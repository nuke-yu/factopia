"""信号留痕与监控结果的数据库实现（对应 `runtime/signals/events.jsonl` 与 `signals/<sid>/monitor.json`）。

两个要点：

1. **链是全局的，不是按信号分的**。`events.jsonl` 只有一份，`prev` 指向上一条全局事件，
   所以 `seq` 必须是 `bigserial`（全局自增），而不是每个 signal 各自从 1 开始。
   写成 `(signal_id, seq)` 主键就错了——那样两条链会各算各的 prev，校验必然失败。
2. **信号目录不建表**。目录项是"审批提交 × 市场 × 事件 × 最新监控"的派生视图，
   落表就要维护一致性；方案 3.3 因此只建 `signal_event` 与 `signal_monitor`，
   目录仍由 `signals.catalog()` 现算。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, chain_hash, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "signals"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _merge_event(row) -> dict:
    seq, signal_id, market, action, role, actor, note, at, prev_hash, digest = row
    ev = {"signal_id": signal_id, "action": action, "role": role, "by": actor,
          "note": note, "at": iso(at), "prev": prev_hash, "hash": digest}
    if market is not None:
        ev["market"] = market
    return ev


def events(limit: int | None = None) -> list[dict]:
    """全部信号事件，按链序（全局 seq）返回。"""
    sql = ("SELECT seq, signal_id, market, action, role, actor, note, at, prev_hash, hash "
           "FROM factopia.signal_event ORDER BY seq")
    if limit:
        sql += " LIMIT %s"
        return [_merge_event(r) for r in db.query(sql, (limit,))]
    return [_merge_event(r) for r in db.query(sql)]


def append(ev: dict, *, exact: dict | None = None) -> dict:
    """追加一条事件。`exact` 时逐字照抄（dual 模式 / 迁移回填），否则补 at/prev/hash。

    并发：`pg_advisory_xact_lock` 锁住整条链的"链头"。链只有一个头，
    所以这里只能串行——但信号状态变更本来就是低频人工操作，串行没有代价。
    """
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('factopia.signal_event'))")
        if exact is not None:
            body = exact
        else:
            row = conn.execute(
                "SELECT hash FROM factopia.signal_event ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else "0" * 64
            body = {**ev, "at": _now(), "prev": prev}
            body["hash"] = chain_hash(body)
        conn.execute(
            """INSERT INTO factopia.signal_event
                   (signal_id, market, action, role, actor, note, at, prev_hash, hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (hash) DO NOTHING""",
            (str(body["signal_id"]), body.get("market"), str(body["action"]),
             str(body.get("role") or ""), str(body.get("by") or ""), body.get("note"),
             as_dt(body.get("at")), body["prev"], body["hash"]),
        )
    return body


def verify_chain() -> dict:
    prev = "0" * 64
    evs = events()
    for i, ev in enumerate(evs):
        if ev["prev"] != prev or chain_hash(ev) != ev["hash"]:
            return {"ok": False, "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(evs), "head": prev}


# ── 监控结果 ─────────────────────────────────────────────────

def save_monitor(result: dict) -> None:
    """每次监控计算一行。`alerts` / `metrics` 拆成列只为让"最近 7 天有预警的信号"
    这类查询能用索引，正文仍以 `payload` 为准（报告口径会随规则包变化，留快照才答得出"当时怎么说"）。"""
    sid = str(result["signal_id"])
    db.execute(
        """INSERT INTO factopia.signal_monitor
               (signal_id, computed_at, suggestion, suggestion_zh, alerts, metrics, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (signal_id, computed_at) DO UPDATE SET
               suggestion = EXCLUDED.suggestion, suggestion_zh = EXCLUDED.suggestion_zh,
               alerts = EXCLUDED.alerts, metrics = EXCLUDED.metrics, payload = EXCLUDED.payload""",
        (
            sid,
            as_dt(result.get("computed_at")) or as_dt(_now()),
            result.get("suggestion"),
            result.get("suggestion_zh"),
            Jsonb(json_safe(result.get("alerts") or [])),
            Jsonb(json_safe({k: result.get(k) for k in ("data_drift", "decay", "factors", "currency")})),
            Jsonb(json_safe(result)),
        ),
    )


def monitor_result(signal_id: str) -> dict | None:
    row = db.query_one(
        "SELECT payload FROM factopia.signal_monitor WHERE signal_id = %s "
        "ORDER BY computed_at DESC LIMIT 1", (signal_id,))
    return row[0] if row else None


def latest_monitors() -> dict[str, dict]:
    """一次查询取全部信号的最新监控：目录页原来是"每个 signal 读一个 json"，现在是一条 DISTINCT ON。"""
    rows = db.query("SELECT signal_id, payload FROM factopia.v_signal_monitor_latest")
    return {sid: payload for sid, payload in rows}
