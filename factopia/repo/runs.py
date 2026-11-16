"""运行产物（`runtime/results/runs/<run_id>/`）的数据库实现。

## 为什么要有这个模块

`run.json` / `rounds.json` / `events.json` 一直被 API、Agent 观察器、验收脚本各自
拼路径直读。files 模式下这没问题；一旦 `FACTOPIA_STORAGE=runs=db`，同一份数据就有
两个位置，谁先写谁后写没人管得住——页面读到的是"上一次运行留下的旧文件"，
看起来正常，数字是旧的。这是迁移里最危险的一类故障：不报错。

因此 run 产物分成两半，处理方式不同：

**进库**（结构稳定、体积小、页面直接渲染）
    run.json        → run
    rounds.json     → run_round（+ run_factor）
    events.json     → run_event
    validation/*.json → run_validation
    quantile/*.json → run_quantile
    diagnostics/*.json → run_diagnostic
    report.md       → run_report

**留文件**（唯一真相，没有第二份副本，因此不存在"读到旧的"问题）
    backtest/*.parquet、signal/*.parquet、score/*.parquet、positions/*.parquet
    这些是 RD-Agent 逐轮回测的直接产物，列结构随 RD-Agent 版本变化；
    强行拆列入库会做出一份"猜错就丢列"的副本。它们统一由 `factopia.runstore`
    独占访问，调用方不再自己拼路径。

## 形状保真

`run.payload` / `run_round.payload` 兜住 002 之后仍可能新增的键：读回时
「列值 + payload」合并，`research` 再按 `research_id`/`version` 还原成嵌套字典。
因此 `load(run_id)` 的返回值与 `run.json` 的 JSON 逐字等价，页面无需感知迁移。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "runs"

# run 表已拆列的键；其余进 payload
RUN_COLS = ("run_id", "market", "horizon", "status", "rounds", "adopted", "cost_usd",
            "started_at", "ended_at", "duration_s", "log_dir", "context", "context_leak",
            "settings", "baseline", "research_id", "version")

# run_round 表已拆列的键；其余进 payload（factors 单独进 run_factor）
ROUND_COLS = ("loop", "status", "hypothesis", "reason", "coding_attempts", "metrics",
              "sota_before", "decision", "feedback", "steps", "workspace", "cost_usd",
              "prompt_tokens", "completion_tokens", "started_at", "ended_at")

EVENT_COLS = ("t", "loop", "step", "kind")

# 这些列是 jsonb：psycopg 不会自动把 dict 转成 jsonb，必须显式包一层
JSON_COLS = ("settings", "baseline", "metrics", "sota_before", "feedback", "steps")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 读 ───────────────────────────────────────────────────────────────────────────────

def _run_from_row(row, payload: dict | None) -> dict:
    (run_id, market, horizon, research_id, version, status, rounds, adopted, cost_usd,
     started_at, ended_at, duration_s, log_dir, context, context_leak, settings, baseline) = row
    out: dict = {}
    for k, v in (("run_id", run_id), ("market", market), ("horizon", horizon),
                 ("status", status), ("rounds", rounds), ("adopted", adopted),
                 ("cost_usd", None if cost_usd is None else float(cost_usd)),
                 ("started_at", iso(started_at)), ("ended_at", iso(ended_at)),
                 ("duration_s", None if duration_s is None else float(duration_s)),
                 ("log_dir", log_dir), ("context", context), ("context_leak", context_leak),
                 ("settings", settings), ("baseline", baseline)):
        if v is not None:
            out[k] = v
    # 嵌套 research 只在文件里出现过时还原；research_id 为空则不输出该键（与文件一致）
    if research_id:
        out["research"] = {"id": research_id}
        if version is not None:
            out["research"]["version"] = version
    out.update(payload or {})
    return json_safe(out)


_RUN_SELECT = """
    SELECT run_id, market, horizon, research_id, version, status, rounds, adopted, cost_usd,
           started_at, ended_at, duration_s, log_dir, context, context_leak,
           settings, baseline, payload
      FROM factopia.run
"""


def load(run_id: str) -> dict | None:
    row = db.query_one(_RUN_SELECT + " WHERE run_id = %s", (str(run_id),))
    if row is None:
        return None
    return _run_from_row(row[:17], row[17])


def exists(run_id: str) -> bool:
    return db.query_one("SELECT 1 FROM factopia.run WHERE run_id = %s", (str(run_id),)) is not None


def all_runs(limit: int | None = None) -> list[dict]:
    """全部运行，按开始时间倒序（与原 `sorted(glob, reverse=True)` 的语义一致）。

    排序键用「真实开始时间」而不是目录名：香港/英国/法国的 run_id 前缀不同，
    字典序会把旧 run 排到最前，导致「04 运行中」默认显示旧批次。
    """
    sql = _RUN_SELECT + " ORDER BY COALESCE(started_at, created_at) DESC, run_id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [_run_from_row(r[:17], r[17]) for r in db.query(sql)]


def ids(market: str | None = None) -> list[str]:
    if market:
        return [r[0] for r in db.query(
            "SELECT run_id FROM factopia.run WHERE market = %s ORDER BY COALESCE(started_at, created_at) DESC",
            (market,))]
    return [r[0] for r in db.query(
        "SELECT run_id FROM factopia.run ORDER BY COALESCE(started_at, created_at) DESC")]


def latest(done_only: bool = True) -> dict | None:
    """最近一次运行（首页与总览用）。`done_only` 时只看已完成的。"""
    sql = _RUN_SELECT + (" WHERE status = 'done'" if done_only else "") + \
        " ORDER BY COALESCE(started_at, created_at) DESC LIMIT 1"
    row = db.query_one(sql)
    return _run_from_row(row[:17], row[17]) if row else None


# ── 写 ───────────────────────────────────────────────────────────────────────────────

def _split_run(run: dict) -> tuple[dict, dict]:
    r = json_safe(dict(run))
    research = r.pop("research", None) or {}
    cols = {k: r.pop(k) for k in list(r) if k in RUN_COLS and k not in ("research_id", "version")}
    cols["research_id"] = research.get("id")
    cols["version"] = research.get("version")
    for k in ("started_at", "ended_at"):
        if cols.get(k) is not None:
            cols[k] = as_dt(cols[k])
    for k in JSON_COLS:                      # settings / baseline 是 jsonb，不能直接塞 dict
        if k in cols:
            cols[k] = None if cols[k] is None else Jsonb(cols[k])
    return cols, r


def save_run(run: dict, *, rounds: list[dict] | None = None, events: list[dict] | None = None,
             replace_children: bool = True) -> dict:
    """写入一次运行（可连带轮次与事件）。

    一个事务：run 主行 UPSERT → 轮次逐条 UPSERT（`replace_children` 时先删掉本次
    不在列表里的旧轮次）→ 事件整体替换。事件没有稳定的自然键（同一步骤可能重复
    出现 `t` 相同），所以按 `seq` 位置写入，写前先清空该 run 的事件——否则重跑
    会在时间线上留下重复条目。
    """
    cols, payload = _split_run(run)
    rid = cols["run_id"]
    order = list(cols)
    sets = [f"{c} = EXCLUDED.{c}" for c in order if c != "run_id"] + ["payload = EXCLUDED.payload"]
    with db.tx() as conn:
        conn.execute(
            f"""INSERT INTO factopia.run ({', '.join(order)}, payload)
                VALUES ({', '.join(['%s'] * len(order))}, %s)
                ON CONFLICT (run_id) DO UPDATE SET {', '.join(sets)}""",
            tuple(cols[c] for c in order) + (Jsonb(payload),),
        )
        if rounds is not None:
            _write_rounds(conn, rid, rounds, replace=replace_children)
        if events is not None:
            _write_events(conn, rid, events)
    return run


def _write_rounds(conn, run_id: str, rounds: list[dict], replace: bool = True) -> None:
    loops = [int(r["loop"]) for r in rounds]
    if replace:
        if loops:
            conn.execute(
                "DELETE FROM factopia.run_round WHERE run_id = %s AND loop <> ALL(%s)",
                (run_id, loops))
        else:
            conn.execute("DELETE FROM factopia.run_round WHERE run_id = %s", (run_id,))
    for r in rounds:
        rec = json_safe(dict(r))
        factors = rec.pop("factors", None)
        loop = int(rec.pop("loop"))
        cols = {k: rec.pop(k) for k in list(rec) if k in ROUND_COLS}
        cols["loop"] = loop
        for k in ("started_at", "ended_at"):
            if cols.get(k) is not None:
                cols[k] = as_dt(cols[k])
        for k in JSON_COLS:
            if k in cols:
                cols[k] = None if cols[k] is None else Jsonb(cols[k])
        order = list(cols)
        sets = [f"{c} = EXCLUDED.{c}" for c in order if c != "loop"] + ["payload = EXCLUDED.payload"]
        conn.execute(
            f"""INSERT INTO factopia.run_round (run_id, {', '.join(order)}, payload)
                VALUES (%s, {', '.join(['%s'] * len(order))}, %s)
                ON CONFLICT (run_id, loop) DO UPDATE SET {', '.join(sets)}""",
            (run_id, *(cols[c] for c in order), Jsonb(rec)),
        )
        if factors is not None:
            _write_factors(conn, run_id, loop, factors)


def _write_factors(conn, run_id: str, loop: int, factors: list[dict]) -> None:
    conn.execute("DELETE FROM factopia.run_factor WHERE run_id = %s AND loop = %s", (run_id, loop))
    for i, f in enumerate(factors):
        f = json_safe(dict(f))
        conn.execute(
            """INSERT INTO factopia.run_factor
                   (run_id, loop, idx, name, description, formulation, variables, code,
                    kind, dataset, source, simulated)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (run_id, loop, i, str(f.get("name") or ""), f.get("description"),
             f.get("formulation"), Jsonb(f.get("variables") or {}), f.get("code"),
             f.get("kind"), f.get("dataset"), f.get("source"), f.get("simulated")),
        )


def _write_events(conn, run_id: str, events: list[dict]) -> None:
    conn.execute("DELETE FROM factopia.run_event WHERE run_id = %s", (run_id,))
    rows = []
    for seq, ev in enumerate(events):
        ev = json_safe(dict(ev))
        t = ev.pop("t", 0) or 0
        payload = {k: v for k, v in ev.items() if k not in EVENT_COLS}
        rows.append((run_id, seq, float(t), ev.get("loop"), ev.get("step"),
                     str(ev.get("kind") or "step"), Jsonb(payload)))
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO factopia.run_event (run_id, seq, t, loop, step, kind, payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""", rows)


def save_events(run_id: str, events: list[dict]) -> None:
    with db.tx() as conn:
        _write_events(conn, run_id, events)


# ── 轮次 / 事件 ──────────────────────────────────────────────────────────────────────

def _factors(run_id: str, loop: int) -> list[dict]:
    out = []
    for (idx, name, desc, form, variables, code, kind, dataset, source, simulated) in db.query(
            """SELECT idx, name, description, formulation, variables, code, kind, dataset,
                      source, simulated
                 FROM factopia.run_factor WHERE run_id = %s AND loop = %s ORDER BY idx""",
            (run_id, loop)):
        f = {"name": name}
        for k, v in (("description", desc), ("formulation", form), ("variables", variables),
                     ("code", code), ("kind", kind), ("dataset", dataset),
                     ("source", source), ("simulated", simulated)):
            if v is not None:
                f[k] = v
        out.append(f)
    return out


def rounds(run_id: str) -> list[dict]:
    rows = db.query(
        """SELECT loop, status, hypothesis, reason, coding_attempts, metrics, sota_before,
                  decision, feedback, steps, workspace, cost_usd, prompt_tokens,
                  completion_tokens, started_at, ended_at, payload
             FROM factopia.run_round WHERE run_id = %s ORDER BY loop""", (str(run_id),))
    out = []
    for row in rows:
        *vals, payload = row
        r: dict = {}
        for k, v in zip(ROUND_COLS, vals):
            if k == "loop":
                r[k] = int(v)
                continue
            if v is None:
                continue
            if k in ("cost_usd",):
                v = float(v)
            elif k in ("started_at", "ended_at"):
                v = iso(v)
            r[k] = v
        r.update(payload or {})
        r["factors"] = _factors(run_id, r["loop"])
        out.append(json_safe(r))
    return out


def round(run_id: str, loop: int) -> dict | None:
    return next((r for r in rounds(run_id) if r["loop"] == int(loop)), None)


def events(run_id: str) -> list[dict]:
    out = []
    for seq, t, loop, step, kind, payload in db.query(
            """SELECT seq, t, loop, step, kind, payload FROM factopia.run_event
                WHERE run_id = %s ORDER BY seq""", (str(run_id),)):
        ev = {"t": float(t)}
        if loop is not None:
            ev["loop"] = loop
        if step is not None:
            ev["step"] = step
        ev["kind"] = kind
        ev.update(payload or {})
        out.append(json_safe(ev))
    return out


def event(run_id: str, seq: int) -> dict | None:
    row = db.query_one(
        "SELECT t, loop, step, kind, payload FROM factopia.run_event WHERE run_id = %s AND seq = %s",
        (str(run_id), int(seq)))
    if row is None:
        return None
    t, loop, step, kind, payload = row
    ev = {"t": float(t), "kind": kind}
    if loop is not None:
        ev["loop"] = loop
    if step is not None:
        ev["step"] = step
    ev.update(payload or {})
    return json_safe(ev)


# ── 可信度检查 / 分组 / 诊断 ────────────────────────────────────────────────────────

def save_validation(run_id: str, loop: int, obj: dict) -> None:
    db.execute(
        """INSERT INTO factopia.run_validation (run_id, loop, state, payload, credibility, computed_at)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (run_id, loop) DO UPDATE SET
               state = EXCLUDED.state, payload = EXCLUDED.payload,
               credibility = EXCLUDED.credibility, computed_at = EXCLUDED.computed_at""",
        (str(run_id), int(loop), obj.get("state"), Jsonb(json_safe(obj)),
         obj.get("credibility"), as_dt(obj.get("computed_at")) or as_dt(_now())))


def validation(run_id: str, loop: int) -> dict | None:
    row = db.query_one(
        "SELECT payload FROM factopia.run_validation WHERE run_id = %s AND loop = %s",
        (str(run_id), int(loop)))
    return json_safe(row[0]) if row else None


def validation_loops(run_id: str) -> list[int]:
    return [r[0] for r in db.query(
        "SELECT loop FROM factopia.run_validation WHERE run_id = %s ORDER BY loop", (str(run_id),))]


def save_quantile(run_id: str, key: str, obj: dict) -> None:
    o = json_safe(obj)
    db.execute(
        """INSERT INTO factopia.run_quantile
               (run_id, key, horizon, groups, days, mean, universe_mean, size, empty_days,
                spread_mean, spread_positive_share, tie_share, dates, nav, nav_universe)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (run_id, key) DO UPDATE SET
               horizon = EXCLUDED.horizon, groups = EXCLUDED.groups, days = EXCLUDED.days,
               mean = EXCLUDED.mean, universe_mean = EXCLUDED.universe_mean, size = EXCLUDED.size,
               empty_days = EXCLUDED.empty_days, spread_mean = EXCLUDED.spread_mean,
               spread_positive_share = EXCLUDED.spread_positive_share,
               tie_share = EXCLUDED.tie_share, dates = EXCLUDED.dates, nav = EXCLUDED.nav,
               nav_universe = EXCLUDED.nav_universe""",
        (str(run_id), str(key), o.get("horizon"), o.get("groups"), o.get("days"),
         Jsonb(o.get("mean")), o.get("universe_mean"), Jsonb(o.get("size")),
         Jsonb(o.get("empty_days")), o.get("spread_mean"), o.get("spread_positive_share"),
         o.get("tie_share"), Jsonb(o.get("dates")), Jsonb(o.get("nav")), Jsonb(o.get("nav_universe"))))


_QUANTILE_COLS = ("horizon", "groups", "days", "mean", "universe_mean", "size", "empty_days",
                  "spread_mean", "spread_positive_share", "tie_share", "dates", "nav", "nav_universe")


def quantile(run_id: str, key: str) -> dict | None:
    row = db.query_one(
        f"SELECT {', '.join(_QUANTILE_COLS)} FROM factopia.run_quantile WHERE run_id = %s AND key = %s",
        (str(run_id), str(key)))
    if row is None:
        return None
    out = {k: v for k, v in zip(_QUANTILE_COLS, row) if v is not None}
    return json_safe(out)


def quantiles(run_id: str) -> dict[str, dict]:
    """`{key: obj}`，等价于打开 quantile 目录读全部 json。"""
    out = {}
    for row in db.query(
            f"""SELECT key, {', '.join(_QUANTILE_COLS)} FROM factopia.run_quantile
                 WHERE run_id = %s ORDER BY key""", (str(run_id),)):
        out[row[0]] = json_safe({k: v for k, v in zip(_QUANTILE_COLS, row[1:]) if v is not None})
    return out


def save_diagnostic(run_id: str, key, obj: dict) -> None:
    db.execute(
        """INSERT INTO factopia.run_diagnostic (run_id, key, payload) VALUES (%s, %s, %s)
           ON CONFLICT (run_id, key) DO UPDATE SET payload = EXCLUDED.payload""",
        (str(run_id), str(key), Jsonb(json_safe(obj))))


def diagnostic(run_id: str, key) -> dict | None:
    row = db.query_one(
        "SELECT payload FROM factopia.run_diagnostic WHERE run_id = %s AND key = %s",
        (str(run_id), str(key)))
    return json_safe(row[0]) if row else None


def diagnostics(run_id: str) -> dict[str, dict]:
    return {k: json_safe(v) for k, v in db.query(
        "SELECT key, payload FROM factopia.run_diagnostic WHERE run_id = %s ORDER BY key",
        (str(run_id),))}


# ── 运行报告 ────────────────────────────────────────────────────────────────────────

def save_report(run_id: str, markdown: str) -> None:
    db.execute(
        """INSERT INTO factopia.run_report (run_id, markdown, bytes) VALUES (%s, %s, %s)
           ON CONFLICT (run_id) DO UPDATE SET
               markdown = EXCLUDED.markdown, bytes = EXCLUDED.bytes, created_at = now()""",
        (str(run_id), markdown, len(markdown.encode("utf-8"))))


def report(run_id: str) -> str | None:
    row = db.query_one("SELECT markdown FROM factopia.run_report WHERE run_id = %s", (str(run_id),))
    return row[0] if row else None
