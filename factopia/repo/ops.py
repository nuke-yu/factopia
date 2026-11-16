"""运维留痕的数据库实现：越权访问（`runtime/access/events.jsonl`）与大模型费用（`runtime/llm_costs.jsonl`）。

两者都是**只追加的流水**，进数据库的收益是"能查"而不是"能改"：

* 访问留痕成表后，"哪个地区角色最近被拒了 20 次、拒的是什么"是一次索引扫描；
  文件时代要读整份 jsonl 再过滤。
* 费用流水成表后，`llm_cost(at DESC)` 与 `llm_cost_ref_idx` 让按日 / 按用途 / 按任务
  汇总不必再全量解析。原来的 `summary()` 是把整个 jsonl 读进内存再 groupby。

访问留痕仍是哈希链（与审批同一套算法），所以 `verify_chain()` 可用。
`allowed` 列留了 true 的位置：当前只在拒绝时记录，将来记录放行时不用改表。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from factopia import db
from factopia.repo import as_dt, chain_hash, fnum, iso

log = logging.getLogger(__name__)

ACCESS_DOMAIN = "access"
COST_DOMAIN = "llm_cost"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 越权访问留痕 ─────────────────────────────────────────────

def append_access(ev: dict, *, exact: dict | None = None) -> dict:
    """追加一条访问留痕。

    列名与文件字段不是一一对应，映射见下表（读回时反向映射，保证哈希可复算）：

        role      → view          role_name → actor
        denied    → resource      market    → ref_id
    """
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('factopia.access_event'))")
        if exact is not None:
            body = exact
        else:
            row = conn.execute(
                "SELECT hash FROM factopia.access_event ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else "0" * 64
            body = {**ev, "at": _now(), "prev": prev}
            body["hash"] = chain_hash(body)
        conn.execute(
            """INSERT INTO factopia.access_event
                   (at, view, resource, ref_id, allowed, actor, prev_hash, hash)
               SELECT %s, %s, %s, %s, %s, %s, %s, %s
               WHERE NOT EXISTS (SELECT 1 FROM factopia.access_event WHERE hash = %s)""",
            (as_dt(body.get("at")), body.get("role"), body.get("denied"), body.get("market"),
             bool(body.get("allowed", False)), body.get("role_name"), body["prev"], body["hash"],
             body["hash"]),
        )
    return body


def access_events(limit: int | None = None) -> list[dict]:
    sql = ("SELECT at, view, resource, ref_id, actor, prev_hash, hash "
           "FROM factopia.access_event ORDER BY seq")
    if limit:
        # 取最近 N 条但仍按链序返回（与 `events()[-20:]` 一致）
        rows = db.query(sql + " DESC LIMIT %s", (limit,))[::-1]
    else:
        rows = db.query(sql)
    return [{"role": view, "role_name": actor, "denied": resource, "market": ref_id,
             "at": iso(at), "prev": prev_hash, "hash": digest}
            for at, view, resource, ref_id, actor, prev_hash, digest in rows]


def verify_access_chain() -> dict:
    prev = "0" * 64
    evs = access_events()
    for i, ev in enumerate(evs):
        if ev["prev"] != prev or chain_hash(ev) != ev["hash"]:
            return {"ok": False, "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(evs), "head": prev}


# ── 大模型费用 ───────────────────────────────────────────────

def record_cost(task: str, model: str, prompt_tokens: int, completion_tokens: int,
                usd: float, ref_table: str | None = None, ref_id: str | None = None,
                at: str | None = None, *, dedupe: bool = False) -> None:
    """记一次调用费用。`dedupe=True` 时同 (时间, 用途, 模型, token) 的行重复写入被跳过，
    供迁移回填反复执行；运行时写入不开启——同一秒内两次同长度调用是两笔钱。"""
    pt, ct = int(prompt_tokens), int(completion_tokens)
    row = (as_dt(at) or as_dt(_now()), model, task, pt, ct, round(float(usd), 6),
           ref_table, ref_id)
    if not dedupe:
        db.execute(
            """INSERT INTO factopia.llm_cost
                   (at, model, purpose, prompt_tokens, completion_tokens, usd, ref_table, ref_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            row,
        )
        return
    db.execute(
        """INSERT INTO factopia.llm_cost
               (at, model, purpose, prompt_tokens, completion_tokens, usd, ref_table, ref_id)
           SELECT %s, %s, %s, %s, %s, %s, %s, %s
           WHERE NOT EXISTS (
               SELECT 1 FROM factopia.llm_cost
               WHERE at = %s AND purpose = %s AND model = %s
                 AND prompt_tokens = %s AND completion_tokens = %s)""",
        row + (row[0], task, model, pt, ct),
    )


def cost_summary(days: int = 30) -> dict:
    """按 (日期, 用途) 汇总。与文件版返回同一形状，前端表格不用改。"""
    rows = db.query(
        """SELECT to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS d, purpose,
                  count(*), COALESCE(sum(prompt_tokens), 0),
                  COALESCE(sum(completion_tokens), 0), COALESCE(sum(usd), 0)
           FROM factopia.llm_cost GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC""")
    out = [{"date": d, "task": purpose, "calls": int(calls),
            "prompt_tokens": int(pt), "completion_tokens": int(ct), "usd": round(fnum(u) or 0, 4)}
           for d, purpose, calls, pt, ct, u in rows]
    dates = sorted({r["date"] for r in out}, reverse=True)[:days]
    return {"rows": [r for r in out if r["date"] in dates]}
