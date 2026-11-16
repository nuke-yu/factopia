"""研究迭代 Agent 的数据库实现（对应 `runtime/agents/<agent_id>/`）。

文件时代一个 Agent 是"一个目录 + 一堆小文件"：

    agent.json                状态、根版本、策略、费用
    rounds/<n>/<name>.json    一轮 5 个 JSON（还有 draft_action / plan_critique 等中间件）
    memory.json               尝试记录与排除方向
    trace.jsonl               每次 LLM / 工具调用
    report.md                 迭代报告

改为表之后有三点收益，都不是"换个地方存"而已：

* **一轮一行**。`agent_round` 把同一轮的 facts / diagnosis / plan / action / evaluation
  放在同一行：代码里永远是整轮一起读写，拆成 5 张表只增加 join。
* **trace 可查询**。jsonl 只能顺序扫；成表后"这个 Agent 最近 20 次工具调用失败在哪"
  是一次索引扫描，而且与 `agent` 同一事务，不会出现"状态改了、trace 没写"。
* **`root` 嵌套拆平**。文件里是 `{"root": {"research_id","version","markets","horizon"}}`，
  表里是四个列——这样"哪些 Agent 在跑某研究版本"可以直接建索引查。

读回时仍然还原成嵌套的 `root`，调用方（loop / approve / api）一行都不用改。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, fnum, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "agents"

# 有独立列的字段；其余（costed_jobs 等）进 payload
_AGENT_COLUMNS = (
    "agent_id", "kind", "state", "research_id", "root_version", "markets", "horizon",
    "policy", "current_round", "current_version", "parent_version", "child_version",
    "round_limit", "dry_run", "web_search", "deep_think", "auto_approve", "approved",
    "approved_run", "cost_usd", "usage", "child_jobs", "last_action", "stop_reason",
    "last_error", "created_at",
)
_KNOWN = set(_AGENT_COLUMNS) | {"root", "rounds", "updated_at", "report"}

# agent_round 的五个正列；其余轮次文件（draft_action / plan_critique / external /
# next_version_draft / approval）进该行的 payload
ROUND_KINDS = ("facts", "diagnosis", "plan", "action", "evaluation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _split(agent: dict) -> dict:
    root = agent.get("root") or {}
    payload = {k: v for k, v in agent.items() if k not in _KNOWN}
    return {
        "agent_id": str(agent["agent_id"]),
        "kind": str(agent.get("kind") or "iteration"),
        "state": str(agent["state"]),
        "research_id": str(root.get("research_id") or agent.get("research_id") or ""),
        "root_version": int(root.get("version") or agent.get("root_version") or 0),
        "markets": list(root.get("markets") or agent.get("markets") or []),
        "horizon": root.get("horizon") if root.get("horizon") is not None else agent.get("horizon"),
        "policy": Jsonb(json_safe(agent.get("policy") or {})),
        "current_round": int(agent.get("current_round") or 0),
        "current_version": agent.get("current_version"),
        "parent_version": agent.get("parent_version"),
        "child_version": agent.get("child_version"),
        "round_limit": int(agent.get("round_limit") or 5),
        "dry_run": bool(agent.get("dry_run")),
        "web_search": bool(agent.get("web_search")),
        "deep_think": bool(agent.get("deep_think")),
        "auto_approve": bool(agent.get("auto_approve")),
        "approved": Jsonb(json_safe(agent["approved"])) if agent.get("approved") is not None else None,
        "approved_run": bool(agent.get("approved_run", True)),
        "cost_usd": fnum(agent.get("cost_usd")) or 0,
        "usage": Jsonb(json_safe(agent.get("usage") or {})),
        "child_jobs": list(agent.get("child_jobs") or []),
        "last_action": agent.get("last_action"),
        "stop_reason": agent.get("stop_reason"),
        "last_error": agent.get("last_error"),
        "created_at": as_dt(agent.get("created_at")) or as_dt(_now()),
        "payload": Jsonb(json_safe(payload)),
    }


_UPSERT = """
INSERT INTO factopia.agent
    (agent_id, kind, state, research_id, root_version, markets, horizon, policy,
     current_round, current_version, parent_version, child_version, round_limit,
     dry_run, web_search, deep_think, auto_approve, approved, approved_run, cost_usd,
     usage, child_jobs, last_action, stop_reason, last_error, created_at, payload)
VALUES
    (%(agent_id)s, %(kind)s, %(state)s, %(research_id)s, %(root_version)s, %(markets)s,
     %(horizon)s, %(policy)s, %(current_round)s, %(current_version)s, %(parent_version)s,
     %(child_version)s, %(round_limit)s, %(dry_run)s, %(web_search)s, %(deep_think)s,
     %(auto_approve)s, %(approved)s, %(approved_run)s, %(cost_usd)s, %(usage)s,
     %(child_jobs)s, %(last_action)s, %(stop_reason)s, %(last_error)s, %(created_at)s,
     %(payload)s)
ON CONFLICT (agent_id) DO UPDATE SET
    kind = EXCLUDED.kind, state = EXCLUDED.state, research_id = EXCLUDED.research_id,
    root_version = EXCLUDED.root_version, markets = EXCLUDED.markets,
    horizon = EXCLUDED.horizon, policy = EXCLUDED.policy,
    current_round = EXCLUDED.current_round, current_version = EXCLUDED.current_version,
    parent_version = EXCLUDED.parent_version, child_version = EXCLUDED.child_version,
    round_limit = EXCLUDED.round_limit, dry_run = EXCLUDED.dry_run,
    web_search = EXCLUDED.web_search, deep_think = EXCLUDED.deep_think,
    auto_approve = EXCLUDED.auto_approve, approved = EXCLUDED.approved,
    approved_run = EXCLUDED.approved_run, cost_usd = EXCLUDED.cost_usd,
    usage = EXCLUDED.usage, child_jobs = EXCLUDED.child_jobs,
    last_action = EXCLUDED.last_action, stop_reason = EXCLUDED.stop_reason,
    last_error = EXCLUDED.last_error, payload = EXCLUDED.payload
"""

_COLS = ("agent_id, kind, state, research_id, root_version, markets, horizon, policy, "
         "current_round, current_version, parent_version, child_version, round_limit, "
         "dry_run, web_search, deep_think, auto_approve, approved, approved_run, cost_usd, "
         "usage, child_jobs, last_action, stop_reason, last_error, created_at, updated_at, payload")


def _merge(row) -> dict:
    (agent_id, kind, state, research_id, root_version, markets, horizon, policy,
     current_round, current_version, parent_version, child_version, round_limit,
     dry_run, web_search, deep_think, auto_approve, approved, approved_run, cost_usd,
     usage, child_jobs, last_action, stop_reason, last_error, created_at,
     updated_at, payload) = row
    agent = {
        "agent_id": agent_id, "kind": kind, "state": state,
        "created_at": iso(created_at),
        "root": {"research_id": research_id, "version": root_version,
                 "markets": list(markets or []), "horizon": horizon},
        "policy": policy or {},
        "current_round": current_round, "current_version": current_version,
        "parent_version": parent_version, "child_version": child_version,
        "round_limit": round_limit, "rounds": [],
        "dry_run": bool(dry_run), "web_search": bool(web_search),
        "deep_think": bool(deep_think), "auto_approve": bool(auto_approve),
        "approved": approved, "approved_run": bool(approved_run),
        "cost_usd": fnum(cost_usd) or 0.0,
        "usage": usage or {},
        "child_jobs": list(child_jobs or []),
        "last_action": last_action, "stop_reason": stop_reason, "last_error": last_error,
    }
    agent.update(payload or {})
    # 文件时代的 agent.json 有 updated_at（每次 save 都写），保留
    agent["updated_at"] = iso(updated_at)
    return agent


def save(agent: dict) -> None:
    db.execute(_UPSERT, _split(agent))


def load(agent_id: str) -> dict:
    row = db.query_one(f"SELECT {_COLS} FROM factopia.agent WHERE agent_id = %s", (agent_id,))
    if row is None:
        raise FileNotFoundError(agent_id)
    return _merge(row)


def list_agents() -> list[dict]:
    rows = db.query(f"SELECT {_COLS} FROM factopia.agent ORDER BY created_at DESC, agent_id DESC")
    return [_merge(r) for r in rows]


# ── 每轮文件 ─────────────────────────────────────────────────

def write_round(agent_id: str, n: int, name: str, data) -> None:
    """写一轮的一个文件。五个正列之外的轮次文件（draft_action / plan_critique /
    external / next_version_draft / approval）进该行的 payload，同一行不同列。"""
    n = int(n)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO factopia.agent_round (agent_id, n) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING", (agent_id, n))
        if name in ROUND_KINDS:
            conn.execute(
                f"UPDATE factopia.agent_round SET {name} = %s WHERE agent_id = %s AND n = %s",
                (Jsonb(json_safe(data)), agent_id, n))
        else:
            conn.execute(
                "UPDATE factopia.agent_round SET payload = payload || %s "
                "WHERE agent_id = %s AND n = %s",
                (Jsonb({name: json_safe(data)}), agent_id, n))


def read_round(agent_id: str, n: int, name: str):
    if name in ROUND_KINDS:
        row = db.query_one(
            f"SELECT {name} FROM factopia.agent_round WHERE agent_id = %s AND n = %s",
            (agent_id, int(n)))
        return row[0] if row else None
    row = db.query_one(
        "SELECT payload -> %s FROM factopia.agent_round WHERE agent_id = %s AND n = %s",
        (name, agent_id, int(n)))
    return row[0] if row else None


def rounds(agent_id: str) -> list[dict]:
    """某 Agent 的全部轮次（一次查询），供报告与 API 用。"""
    rows = db.query(
        """SELECT n, facts, diagnosis, plan, action, evaluation, payload
           FROM factopia.agent_round WHERE agent_id = %s ORDER BY n""", (agent_id,))
    out = []
    for n, facts, diagnosis, plan, action, evaluation, payload in rows:
        rec = {"n": n}
        for key, val in (("facts", facts), ("diagnosis", diagnosis), ("plan", plan),
                         ("action", action), ("evaluation", evaluation)):
            if val is not None:
                rec[key] = val
        rec.update(payload or {})
        out.append(rec)
    return out


# ── trace ────────────────────────────────────────────────────

def append_trace(agent_id: str, entry: dict) -> None:
    """调用留痕。同一行重复写入会被跳过（迁移回填可反复执行）。"""
    payload = {k: v for k, v in entry.items() if k not in ("kind", "at")}
    at = as_dt(entry.get("at")) or as_dt(_now())
    body = Jsonb(json_safe(payload))
    db.execute(
        """INSERT INTO factopia.agent_trace (agent_id, at, kind, payload)
           SELECT %s, %s, %s, %s
           WHERE NOT EXISTS (
               SELECT 1 FROM factopia.agent_trace
               WHERE agent_id = %s AND at = %s
                 AND kind IS NOT DISTINCT FROM %s AND payload = %s)""",
        (agent_id, at, entry.get("kind"), body,
         agent_id, at, entry.get("kind"), body),
    )


def trace(agent_id: str, limit: int | None = None) -> list[dict]:
    sql = ("SELECT at, kind, payload FROM factopia.agent_trace WHERE agent_id = %s "
           "ORDER BY seq")
    if limit:
        sql += " DESC LIMIT %s"
        rows = db.query(sql, (agent_id, limit))[::-1]
    else:
        rows = db.query(sql, (agent_id,))
    out = []
    for at, kind, payload in rows:
        rec = {"at": iso(at)}
        if kind is not None:
            rec["kind"] = kind
        rec.update(payload or {})
        out.append(rec)
    return out


# ── 记忆与报告 ───────────────────────────────────────────────

def memory(agent_id: str) -> dict:
    row = db.query_one(
        "SELECT attempts, exclusions, updated_at FROM factopia.agent_memory WHERE agent_id = %s",
        (agent_id,))
    if row is None:
        return {"attempts": [], "exclusions": [], "updated_at": None}
    return {"attempts": row[0] or [], "exclusions": row[1] or [], "updated_at": iso(row[2])}


def save_memory(agent_id: str, data: dict) -> None:
    db.execute(
        """INSERT INTO factopia.agent_memory (agent_id, attempts, exclusions, updated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (agent_id) DO UPDATE SET
               attempts = EXCLUDED.attempts, exclusions = EXCLUDED.exclusions,
               updated_at = now()""",
        (agent_id, Jsonb(json_safe(data.get("attempts") or [])),
         Jsonb(json_safe(data.get("exclusions") or []))),
    )


def report(agent_id: str) -> str | None:
    row = db.query_one("SELECT report FROM factopia.agent WHERE agent_id = %s", (agent_id,))
    if row is None:
        raise FileNotFoundError(agent_id)
    return row[0]


def save_report(agent_id: str, text: str) -> None:
    db.execute("UPDATE factopia.agent SET report = %s WHERE agent_id = %s", (text, agent_id))
