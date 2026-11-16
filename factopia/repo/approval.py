"""审批留痕与提交材料的数据库实现（对应 `runtime/approvals/<sid>/`）。

这个域是整个迁移里**事务收益最明显**的地方，也是三条哈希链里最严格的一条。

文件时代的 `submit()` 是这样写的：

    mkdir materials/                     ← ① 写目录
    write report / spec / factors / translations / rules   ← ② 逐个文件
    _save(sub)                           ← ③ 原子替换 submission.json
    _append(sid, {...})                  ← ④ 追加 events.jsonl

四步之间任何一步崩溃，都会留下"材料在、提交记录不在"或"提交记录在、留痕缺失"的
脏状态，而审计要求恰好是"提交与留痕必须同时成立"。数据库版把 ③④ 放进同一个事务：

    with db.tx() as conn:
        upsert approval_submission
        delete-insert approval_step
        delete-insert approval_rule_decision
        insert approval_event（哈希链）

① ② 仍然写盘——冻结材料（报告原件、因子代码、规则包、翻译）按方案第 3 节留在文件侧，
数据库只存 materials 元数据与 sha256。理由：它们是**不可变的归档字节**，
进数据库会把 8 GB 配额换成几 MB 的 bytea，却换不到任何查询能力。

哈希链的口径与文件版**完全相同**：

    hash = sha256(json(除 hash 外全部字段, sort_keys=True, ensure_ascii=False, default=str))
    prev = 上一条的 hash（第一条为 64 个 0）

所以同一个事件在文件里和在数据库里算出的 hash 是同一个值 —— 迁移时直接搬运原 hash，
不需要（也不允许）重算，否则历史链会断。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, chain_hash, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "approval"

# 事件列 → 文件里的键名。`actor` 列在文件里叫 `by`，这是唯一的改名，必须映射回去，
# 否则读回的事件 dict 与原文件不一致，哈希校验立刻失败。
EVENT_COLUMNS = ("seq", "at", "action", "role", "actor", "note", "step", "rule",
                 "markets", "thresholds", "labels", "payload", "prev_hash", "hash")

# 这些键有独立列，其余全部进 payload
EVENT_KNOWN = {"action", "role", "by", "note", "step", "rule", "markets", "thresholds", "labels"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 提交主体 ─────────────────────────────────────────────────

def _split_submission(sub: dict) -> dict:
    return {
        "id": str(sub["id"]),
        "research_id": str(sub["research_id"]),
        "version": int(sub["version"]),
        "markets": list(sub.get("markets") or []),
        "approved_markets": sub.get("approved_markets"),
        "status": str(sub["status"]),
        "current_step": int(sub.get("current") or 0),
        "fingerprint": str(sub["fingerprint"]),
        "created_at": as_dt(sub.get("created_at")) or as_dt(_now()),
        "created_by": sub.get("created_by"),
        "materials": Jsonb(json_safe(sub.get("materials") or {})),
        "conditions": Jsonb(json_safe(sub.get("conditions") or [])),
        "lock": Jsonb(json_safe(sub["lock"])) if sub.get("lock") is not None else None,
    }


_UPSERT_SUBMISSION = """
INSERT INTO factopia.approval_submission
    (id, research_id, version, markets, approved_markets, status, current_step,
     fingerprint, created_at, created_by, materials, conditions, lock)
VALUES
    (%(id)s, %(research_id)s, %(version)s, %(markets)s, %(approved_markets)s, %(status)s,
     %(current_step)s, %(fingerprint)s, %(created_at)s, %(created_by)s, %(materials)s,
     %(conditions)s, %(lock)s)
ON CONFLICT (id) DO UPDATE SET
    research_id      = EXCLUDED.research_id,
    version          = EXCLUDED.version,
    markets          = EXCLUDED.markets,
    approved_markets = EXCLUDED.approved_markets,
    status           = EXCLUDED.status,
    current_step     = EXCLUDED.current_step,
    fingerprint      = EXCLUDED.fingerprint,
    materials        = EXCLUDED.materials,
    conditions       = EXCLUDED.conditions,
    lock             = EXCLUDED.lock
RETURNING created_at, updated_at
"""


def _sync_steps(conn, sid: str, steps: list[dict]) -> None:
    """路线是整条重写的：步数是个位数，而且 approve 会推进 current，逐条 diff 得不偿失。"""
    conn.execute("DELETE FROM factopia.approval_step WHERE submission_id = %s", (sid,))
    for i, st in enumerate(steps or []):
        conn.execute(
            """INSERT INTO factopia.approval_step
                   (submission_id, idx, role, market, duty, state, hits, model_exceptions)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (sid, i, str(st.get("role") or ""), st.get("market"), st.get("duty"),
             str(st.get("state") or "pending"), list(st.get("hits") or []),
             list(st.get("model_exceptions") or [])),
        )


def _sync_decisions(conn, sid: str, decisions: dict) -> None:
    """`rule_decisions` 的键是 "角色|规则"，落表时拆成两列——这正是拆表的目的：
    以后可以直接 `GROUP BY rule` 统计"哪条规则最常被例外处理"。"""
    conn.execute("DELETE FROM factopia.approval_rule_decision WHERE submission_id = %s", (sid,))
    for key, d in (decisions or {}).items():
        role = d.get("role") or key.split("|", 1)[0]
        conn.execute(
            """INSERT INTO factopia.approval_rule_decision
                   (submission_id, role, rule, action, actor, note, at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (submission_id, role, rule) DO UPDATE SET
                   action = EXCLUDED.action, actor = EXCLUDED.actor,
                   note = EXCLUDED.note, at = EXCLUDED.at""",
            (sid, role, str(d.get("rule") or key.split("|", 1)[-1]), str(d.get("action") or ""),
             d.get("by"), d.get("note"), as_dt(d.get("at")) or as_dt(_now())),
        )


def _event_params(sid: str, ev: dict) -> dict:
    """一个事件 dict → 列参数。未知字段进 payload，读回时再摊平。"""
    extra = {k: v for k, v in ev.items() if k not in EVENT_KNOWN and k not in ("at", "prev", "hash", "seq")}
    return {
        "submission_id": sid,
        "at": as_dt(ev.get("at")),
        "action": str(ev["action"]),
        "role": str(ev.get("role") or ""),
        "actor": str(ev.get("by") or ""),
        "note": ev.get("note"),
        "step": ev.get("step"),
        "rule": ev.get("rule"),
        "markets": ev.get("markets"),
        "thresholds": Jsonb(json_safe(ev["thresholds"])) if ev.get("thresholds") is not None else None,
        "labels": ev.get("labels"),
        "payload": Jsonb(json_safe(extra)),
        "prev_hash": ev["prev"],
        "hash": ev["hash"],
    }


_INSERT_EVENT = """
INSERT INTO factopia.approval_event
    (submission_id, seq, at, action, role, actor, note, step, rule, markets,
     thresholds, labels, payload, prev_hash, hash)
VALUES
    (%(submission_id)s, %(seq)s, %(at)s, %(action)s, %(role)s, %(actor)s, %(note)s,
     %(step)s, %(rule)s, %(markets)s, %(thresholds)s, %(labels)s, %(payload)s,
     %(prev_hash)s, %(hash)s)
ON CONFLICT DO NOTHING
"""


def append_event(conn, sid: str, ev: dict | None = None, *, exact: dict | None = None,
                 seq: int | None = None) -> dict:
    """追加一条事件。

    `exact`：已经算好 at / prev / hash 的事件（`dual` 模式下由文件实现产出，或迁移回填时
    从 events.jsonl 原样搬运）。给了它就**逐字照抄**，绝不重算 —— 重算会让两条链分叉。
    `ev`：只给业务字段，由这里补 at / prev / hash。

    并发：先锁住提交行，同一提交的事件串行追加，`seq` 不会撞号。
    """
    conn.execute("SELECT 1 FROM factopia.approval_submission WHERE id = %s FOR UPDATE", (sid,))
    if exact is None:
        row = conn.execute(
            "SELECT hash FROM factopia.approval_event WHERE submission_id = %s "
            "ORDER BY seq DESC LIMIT 1", (sid,)
        ).fetchone()
        prev = row[0] if row else "0" * 64
        body = {**ev, "at": _now(), "prev": prev}
        body["hash"] = chain_hash(body)
    else:
        body = exact
    if seq is None:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM factopia.approval_event WHERE submission_id = %s",
            (sid,),
        ).fetchone()[0]
    params = _event_params(sid, body)
    params["seq"] = seq
    conn.execute(_INSERT_EVENT, params)
    return body


def save_full(sub: dict, events: list[dict]) -> dict:
    """迁移专用：整条提交 + 全部历史事件，一个事务。

    `seq` 按文件里的顺序（第 i 条就是 i+1），并用 `ON CONFLICT DO NOTHING` 兜住重跑；
    事件里的 at / prev / hash **原样写入**，绝不重算——重算等于改写历史。
    """
    row = _split_submission(sub)
    with db.tx() as conn:
        conn.execute(_UPSERT_SUBMISSION, row)
        _sync_steps(conn, row["id"], sub.get("steps") or [])
        _sync_decisions(conn, row["id"], sub.get("rule_decisions") or {})
        for i, ev in enumerate(events, start=1):
            append_event(conn, row["id"], exact=ev, seq=i)
    return sub


def save(sub: dict, event: dict | None = None, *, exact_event: dict | None = None) -> dict:
    """整条写入提交；`event` 给定时在同一事务里追加留痕。

    这是"提交与留痕必须同时成立"的实现点。文件版做不到，因为它没有事务。
    """
    row = _split_submission(sub)
    with db.tx() as conn:
        created_at, updated_at = conn.execute(_UPSERT_SUBMISSION, row).fetchone()
        _sync_steps(conn, row["id"], sub.get("steps") or [])
        _sync_decisions(conn, row["id"], sub.get("rule_decisions") or {})
        if event is not None or exact_event is not None:
            append_event(conn, row["id"], event, exact=exact_event)
    log.debug("%s: 提交 %s 已写入（含留痕）", DOMAIN, row["id"])
    return sub


# ── 读取 ─────────────────────────────────────────────────────

def _read_steps(conn, sid: str) -> list[dict]:
    rows = conn.execute(
        """SELECT role, market, duty, state, hits, model_exceptions
           FROM factopia.approval_step WHERE submission_id = %s ORDER BY idx""",
        (sid,),
    ).fetchall()
    out = []
    for role, market, duty, state, hits, exc in rows:
        st = {"role": role, "duty": duty, "state": state,
              "hits": list(hits or []), "model_exceptions": list(exc or [])}
        if market is not None:          # 数据负责人 / 模型风险 等步骤没有市场，文件里也没有这个键
            st["market"] = market
        out.append(st)
    return out


def _read_decisions(conn, sid: str) -> dict:
    rows = conn.execute(
        """SELECT role, rule, action, actor, note, at FROM factopia.approval_rule_decision
           WHERE submission_id = %s""",
        (sid,),
    ).fetchall()
    return {f"{role}|{rule}": {"rule": rule, "action": action, "by": actor, "role": role,
                               "note": note, "at": iso(at)}
            for role, rule, action, actor, note, at in rows}


def _merge_event(row) -> dict:
    (seq, at, action, role, actor, note, step, rule, markets, thresholds, labels,
     payload, prev_hash, digest) = row
    ev = {"action": action, "role": role, "by": actor, "at": iso(at), "prev": prev_hash, "hash": digest}
    for key, value in (("note", note), ("step", step), ("rule", rule), ("markets", markets),
                       ("thresholds", thresholds), ("labels", labels)):
        if value is not None:
            ev[key] = list(value) if key == "markets" else value
    ev.update(payload or {})
    return ev


def load(sid: str) -> dict:
    with db.conn() as conn:
        row = conn.execute(
            """SELECT id, research_id, version, markets, approved_markets, status,
                      current_step, fingerprint, created_at, created_by, materials,
                      conditions, lock, updated_at
               FROM factopia.approval_submission WHERE id = %s""",
            (sid,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(sid)
        steps = _read_steps(conn, sid)
        decisions = _read_decisions(conn, sid)
    (sid_, rid, version, markets, approved, status, current, fingerprint,
     created_at, created_by, materials, conditions, lock, updated_at) = row
    sub = {
        "id": sid_, "research_id": rid, "version": version, "markets": list(markets or []),
        "status": status, "current": current, "steps": steps,
        "created_at": iso(created_at), "created_by": created_by, "fingerprint": fingerprint,
        "materials": materials, "rule_decisions": decisions,
        "approved_markets": list(approved) if approved is not None else None,
        "conditions": conditions if conditions is not None else [],
        "lock": lock,
    }
    # 文件时代的 submission.json 在第一次 act() 之前**没有** updated_at 键
    # （summary() 用 `s.get("updated_at") or s["created_at"]` 兜底）。
    # 这里保持同一语义，否则迁移对账会在每一份新提交上误报差异。
    if iso(updated_at) != iso(created_at):
        sub["updated_at"] = iso(updated_at)
    return sub


def exists(sid: str) -> bool:
    return db.query_one(
        "SELECT 1 FROM factopia.approval_submission WHERE id = %s", (sid,)) is not None


def events(sid: str) -> list[dict]:
    rows = db.query(
        f"""SELECT {", ".join(EVENT_COLUMNS)} FROM factopia.approval_event
            WHERE submission_id = %s ORDER BY seq""",
        (sid,),
    )
    return [_merge_event(r) for r in rows]


def verify_chain(sid: str) -> dict:
    """与文件版同一个算法：逐条比对 prev 与重算 hash。"""
    prev = "0" * 64
    evs = events(sid)
    for i, ev in enumerate(evs):
        if ev["prev"] != prev or chain_hash(ev) != ev["hash"]:
            return {"ok": False, "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(evs), "head": prev}


def ids(research_id: str | None = None) -> list[str]:
    """提交编号，新的在前（文件版按目录名倒序，编号里带时间戳，等价）。"""
    if research_id:
        rows = db.query(
            "SELECT id FROM factopia.approval_submission WHERE research_id = %s ORDER BY id DESC",
            (research_id,))
    else:
        rows = db.query("SELECT id FROM factopia.approval_submission ORDER BY id DESC")
    return [r[0] for r in rows]
