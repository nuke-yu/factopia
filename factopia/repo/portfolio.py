"""授权组合的数据库实现（对应 `runtime/portfolio/<rid>_v<ver>/`）。

拆分原则在这一域体现得最清楚：

* `summary.json` **整体进 `portfolio.summary`（JSONB）**。它是提交审批时冻结的那份材料，
  审计要回答"当时组合的约束、归因、汇率拆分分别是什么"，任何规范化都会破坏这个答案。
  `period` / `metrics` / `attribution` / `fx_split` / `constraints` 五列只是**投影**，
  为的是让看板不必解析整份 JSON 就能排序与筛选。
* `daily.parquet` 拆成 `portfolio_daily`：`port` / `bench` 是每天都要画的曲线，单独成列；
  `{市场}_rl/_bl/_rb/_bb/_w/_cost/_rf` 这组按市场伸缩的字段进 `payload`——
  市场数一变就要加列的表，迟早要改结构。
* `weights.parquet` 拆成 `portfolio_weight`：一行一只股票一天，才能算"某只股票被持有了多久"。
* `feedback.json` 拆成 `portfolio_feedback`：追加表，`bigserial` 天然保留顺序。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, fnum, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "portfolio"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(research_id: str, version: int, summary: dict,
         daily: pd.DataFrame | None = None, weights: pd.DataFrame | None = None) -> None:
    """写入一份组合：summary 整体、逐日曲线、逐股权重，一个事务。"""
    rid, ver = str(research_id), int(version)
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO factopia.portfolio
                   (research_id, version, built_at, base_currency, period, metrics,
                    attribution, fx_split, constraints, summary)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (research_id, version) DO UPDATE SET
                   built_at = EXCLUDED.built_at, base_currency = EXCLUDED.base_currency,
                   period = EXCLUDED.period, metrics = EXCLUDED.metrics,
                   attribution = EXCLUDED.attribution, fx_split = EXCLUDED.fx_split,
                   constraints = EXCLUDED.constraints, summary = EXCLUDED.summary""",
            (
                rid, ver, as_dt(summary.get("built_at")), summary.get("base_currency"),
                Jsonb(json_safe(summary.get("period") or {})),
                Jsonb(json_safe(summary.get("metrics") or {})),
                Jsonb(json_safe(summary.get("attribution") or {})),
                Jsonb(json_safe(summary.get("fx_split") or {})),
                Jsonb(json_safe(summary.get("constraints") or [])),
                Jsonb(json_safe(summary)),
            ),
        )

        if daily is not None and len(daily):
            conn.execute("DELETE FROM factopia.portfolio_daily WHERE research_id=%s AND version=%s",
                         (rid, ver))
            fixed = {"date", "port", "bench"}
            # 逐日曲线可能以 date 为索引（build 里的产物），也可能是普通列（parquet 里 index=False）。
            # 只在索引确实是 date 时展开——否则 reset_index() 会多出一列 "index" 混进 payload。
            frame = daily.reset_index() if daily.index.name == "date" else daily
            for rec in frame.to_dict("records"):
                extra = {k: v for k, v in rec.items() if k not in fixed}
                conn.execute(
                    """INSERT INTO factopia.portfolio_daily
                           (research_id, version, date, port, bench, payload)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (rid, ver, pd.Timestamp(rec["date"]).date(), fnum(rec.get("port")),
                     fnum(rec.get("bench")), Jsonb(json_safe(extra))),
                )

        if weights is not None and len(weights):
            conn.execute("DELETE FROM factopia.portfolio_weight WHERE research_id=%s AND version=%s",
                         (rid, ver))
            for rec in weights.to_dict("records"):
                conn.execute(
                    """INSERT INTO factopia.portfolio_weight
                           (research_id, version, date, market, instrument, weight)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (rid, ver, pd.Timestamp(rec["date"]).date(), str(rec["market"]),
                     str(rec["symbol"]), fnum(rec.get("weight"))),
                )


def load(research_id: str, version: int) -> dict | None:
    row = db.query_one(
        "SELECT summary FROM factopia.portfolio WHERE research_id = %s AND version = %s",
        (str(research_id), int(version)))
    return row[0] if row else None


def list_summaries() -> list[dict]:
    """全部已构建组合的 summary（首页体检与看板用），新的在前。"""
    rows = db.query("SELECT summary FROM factopia.portfolio ORDER BY built_at DESC NULLS LAST")
    return [r[0] for r in rows]


def exists(research_id: str, version: int) -> bool:
    return db.query_one(
        "SELECT 1 FROM factopia.portfolio WHERE research_id = %s AND version = %s",
        (str(research_id), int(version))) is not None


def daily(research_id: str, version: int) -> pd.DataFrame:
    """逐日曲线。列与 `daily.parquet` 完全一致（date/port/bench/各市场字段）。"""
    rows = db.query(
        """SELECT date, port, bench, payload FROM factopia.portfolio_daily
           WHERE research_id = %s AND version = %s ORDER BY date""",
        (str(research_id), int(version)))
    if not rows:
        return pd.DataFrame()
    recs = []
    for d, port, bench, payload in rows:
        rec = {"date": pd.Timestamp(d), "port": fnum(port), "bench": fnum(bench)}
        rec.update(payload or {})
        recs.append(rec)
    return pd.DataFrame(recs)


def weights(research_id: str, version: int) -> pd.DataFrame:
    """逐股权重。列与 `weights.parquet` 一致：date/market/symbol/weight。（weight_in_market 由
    调用方按市场内归一，数据库只存占总组合权重这一列，避免存两份可推导的数。）"""
    rows = db.query(
        """SELECT date, market, instrument, weight FROM factopia.portfolio_weight
           WHERE research_id = %s AND version = %s ORDER BY date, market, instrument""",
        (str(research_id), int(version)))
    if not rows:
        return pd.DataFrame(columns=["date", "market", "symbol", "weight"])
    df = pd.DataFrame(rows, columns=["date", "market", "symbol", "weight"])
    df["date"] = pd.to_datetime(df["date"])
    df["weight"] = df["weight"].astype(float)
    # weight_in_market：市场内归一，供"市场内权重"视图直接用
    tot = df.groupby(["date", "market"])["weight"].transform("sum")
    df["weight_in_market"] = (df["weight"] / tot).fillna(0.0)
    return df[["date", "market", "symbol", "weight_in_market", "weight"]]


# ── 反馈闭环 ─────────────────────────────────────────────────

def record_feedback(research_id: str, version: int, entry: dict) -> list[dict]:
    """记一条反馈。同一行重复写入被跳过——迁移回填可反复执行。"""
    rid, ver = str(research_id), int(version)
    payload = {k: v for k, v in entry.items() if k not in ("at", "by", "note")}
    at = as_dt(entry.get("at")) or as_dt(_now())
    body = Jsonb(json_safe(payload))
    db.execute(
        """INSERT INTO factopia.portfolio_feedback
               (research_id, version, at, actor, note, payload)
           SELECT %s, %s, %s, %s, %s, %s
           WHERE NOT EXISTS (
               SELECT 1 FROM factopia.portfolio_feedback
               WHERE research_id = %s AND version = %s AND at = %s AND payload = %s)""",
        (rid, ver, at, entry.get("by"), entry.get("note"), body,
         rid, ver, at, body),
    )
    return feedback(rid, ver)


def feedback(research_id: str, version: int) -> list[dict]:
    rows = db.query(
        """SELECT at, actor, note, payload FROM factopia.portfolio_feedback
           WHERE research_id = %s AND version = %s ORDER BY id""",
        (str(research_id), int(version)))
    out = []
    for at, actor, note, payload in rows:
        rec = {"at": iso(at)}
        rec.update(payload or {})
        if actor is not None:
            rec["by"] = actor
        if note is not None:
            rec["note"] = note
        out.append(rec)
    return out
