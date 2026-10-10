"""Factopia 后端接口。

运行（独立服务，见 docker/api/Dockerfile 与 docker-compose.yml 的 api 服务）：
    uvicorn factopia.api.main:app --host 0.0.0.0 --port 8000

数据来自 /workspace/runtime/results，由 factopia.export 导出；研究规格示例、模拟授权与市场介绍来自 /workspace/configs。
研究规格的起草、版本与运行任务（Spec 第 16 章）：起草调用大模型；锁定的版本写入 /workspace/runtime/research；
发起运行只新建任务文件（/workspace/runtime/jobs），由后台运行器（runner 服务）执行。
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from factopia.research import draft as drafting
from factopia.research import evidence, mandates, store, verdict
from factopia import rules as rulebook
from factopia.research.context import build_context
from factopia.rules import context as rule_ctx
from factopia.research.periods import HORIZONS, split_dates
from factopia.validate import metrics as vmetrics

RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUN_ID = re.compile(r"^[a-z]{2}_h\d{1,2}_\d{14}$")
ROUND_KEY = re.compile(r"^(baseline|\d{1,3})$")
MARKETS = ("hk", "uk", "fr")
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}

app = FastAPI(title="Factopia API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(PermissionError)
def _storage_not_ready(request, exc: PermissionError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={"detail": "研究存储目录尚未初始化：请先启动后台运行器（runner 服务）"})


def _json(path: Path):
    if not path.exists():
        raise HTTPException(404, f"结果文件不存在：{path.name}（先运行 factopia.export）")
    return _cached_json(path, path.stat().st_mtime)


@lru_cache(maxsize=64)
def _cached_json(path: Path, mtime: float):  # 文件更新后 mtime 变化，缓存自动失效
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(run_id: str) -> Path:
    if not RUN_ID.match(run_id) or not (RESULTS / "runs" / run_id / "run.json").exists():
        raise HTTPException(404, f"运行不存在：{run_id}")
    return RESULTS / "runs" / run_id


def _columns(df: pd.DataFrame) -> dict:
    """按列返回，日期转为字符串，缺失值为 null。"""
    out = {}
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            out[c] = s.dt.strftime("%Y-%m-%d").tolist()
        else:
            out[c] = [None if pd.isna(v) else round(float(v), 6) for v in s]
    return out


@app.get("/api/health")
def health():
    return {"status": "ok", "results": RESULTS.exists()}


@app.get("/api/overview")
def overview():
    return _json(RESULTS / "overview.json")


@app.get("/api/alpha158/{horizon}/cube")
def cube(horizon: int):
    return _json(RESULTS / "alpha158" / f"h{horizon}" / "cube.json")


@app.get("/api/alpha158/{horizon}/surface")
def surface(horizon: int, market: str | None = Query(None, pattern="^(hk|uk|fr)$")):
    data = _json(RESULTS / "alpha158" / f"h{horizon}" / "surface.json")
    if market:
        return {**data, "markets": {market: data["markets"][market]}}
    return data


@app.get("/api/runs")
def runs():
    items = []
    for p in sorted((RESULTS / "runs").glob("*/run.json"), reverse=True):
        run = _json(p)
        items.append({k: v for k, v in run.items() if k not in ("context", "log_dir")})
    return items


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    d = _run_dir(run_id)
    run = _json(d / "run.json")
    job = _job_of_run(run_id)
    batch = [_job_public(j) for j in store.list_jobs() if job and j["batch_id"] == job["batch_id"]]
    rounds = _json(d / "rounds.json")
    spec = _run_spec(run)
    rule = verdict.rules(spec) if spec else None
    return {"run": {**run, "state": _run_state(run, job)}, "rounds": rounds,
            "job": _job_public(job) if job else None, "batch": batch,
            "verdicts": verdict.judge_rounds(run, rounds, rule) if rule else None,
            "rule": ({k: rule[k] for k in ("primary", "ric_sign", "sign_from_text")} | {"primary_market": run["market"] == rule["primary"]}) if rule else None}


def _run_spec(run: dict) -> dict | None:
    """运行所用的锁定版本（页面发起的运行才有）。"""
    r = run.get("research") or {}
    rid, v = r.get("id"), r.get("version")
    if not rid or v is None or not drafting.ID_PATTERN.match(rid) or v not in store.versions(rid):
        return None
    return store.load_version(rid, v)


@app.get("/api/runs/{run_id}/backtest")
def backtest(run_id: str):
    d = _run_dir(run_id) / "backtest"
    return {p.stem: _columns(pd.read_parquet(p)) for p in sorted(d.glob("*.parquet"))}


@app.get("/api/runs/{run_id}/signal")
def signal(run_id: str):
    d = _run_dir(run_id) / "signal"
    return {p.stem: _columns(pd.read_parquet(p)) for p in sorted(d.glob("*.parquet"))}


def _compound(s: pd.Series) -> float:
    return float((1 + s).prod() - 1)


@app.get("/api/runs/{run_id}/monthly")
def monthly(run_id: str):
    """各轮的月度与年度收益（扣成本后超额收益、基准指数，按日复利，与净值图一致）。"""
    out = {}
    for p in sorted((_run_dir(run_id) / "backtest").glob("*.parquet")):
        df = pd.read_parquet(p, columns=["date", "excess_with_cost", "bench"])
        idx = pd.to_datetime(df["date"])
        rows = []
        for keys, g in df.groupby([idx.dt.year, idx.dt.month]):
            rows.append({"year": int(keys[0]), "month": int(keys[1]), "days": len(g),
                         "excess": round(_compound(g["excess_with_cost"]), 6), "bench": round(_compound(g["bench"]), 6)})
        years = [{"year": int(y), "days": len(g), "excess": round(_compound(g["excess_with_cost"]), 6),
                  "bench": round(_compound(g["bench"]), 6)} for y, g in df.groupby(idx.dt.year)]
        out[p.stem] = {"months": rows, "years": years}
    return out


@app.get("/api/runs/{run_id}/quantiles")
def quantiles(run_id: str):
    """各轮的分组收益（按模型打分分 5 组）。"""
    d = _run_dir(run_id) / "quantile"
    return {p.stem: _json(p) for p in sorted(d.glob("*.json"))}


@app.get("/api/runs/{run_id}/positions/{key}")
def positions(run_id: str, key: str, step: int = Query(5, ge=1, le=60)):
    """持仓矩阵：每隔 step 个交易日取一次，股票按持有天数排序。"""
    if not ROUND_KEY.match(key):
        raise HTTPException(404, "轮次不存在")
    path = _run_dir(run_id) / "positions" / f"{key}.parquet"
    if not path.exists():
        raise HTTPException(404, "该轮没有持仓记录")
    df = pd.read_parquet(path)
    dates = sorted(df["date"].unique())[::step]
    df = df[df["date"].isin(dates)]
    order = df.groupby("instrument").size().sort_values(ascending=False).index.tolist()
    pivot = df.pivot(index="date", columns="instrument", values="weight").reindex(columns=order).fillna(0.0)
    return {
        "dates": [pd.Timestamp(d).strftime("%Y-%m-%d") for d in pivot.index],
        "instruments": order,
        "weights": [[round(float(v), 5) for v in row] for row in pivot.to_numpy()],
    }


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str):
    """过程事件（供运行中页面显示或回放）。完整提示词较长，按需从 /prompt 接口读取。"""
    events = _json(_run_dir(run_id) / "events.json")
    return [{**{k: v for k, v in e.items() if k not in ("system", "user")}, "index": i, "has_prompt": bool(e.get("user"))}
            for i, e in enumerate(events)]


@app.get("/api/runs/{run_id}/events/{index}/prompt")
def run_event_prompt(run_id: str, index: int):
    events = _json(_run_dir(run_id) / "events.json")
    if not 0 <= index < len(events) or not events[index].get("user"):
        raise HTTPException(404, "该事件没有保存提示词")
    e = events[index]
    return {"system": e["system"], "user": e["user"], "response": e.get("response")}


RUNNING_WINDOW = timedelta(minutes=30)  # 没有任务记录的运行（命令行启动）：最后一次记录后 30 分钟内视为"运行中"
JOB_STATE = {"queued": "排队中", "running": "运行中", "done": "已完成", "failed": "失败", "cancelled": "已停止",
             "budget_stopped": "超出预算停止"}


def _job_of_run(run_id: str) -> dict | None:
    return next((j for j in store.list_jobs() if j.get("run_id") == run_id), None)


def _job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "log_dir"} | {"state_zh": JOB_STATE.get(job["state"], job["state"])}


def _run_state(run: dict, job: dict | None = None) -> str:
    if job:
        return {"running": "运行中", "done": "已完成"}.get(job["state"], "已中止")
    if run.get("status") == "done":
        return "已完成"
    ended = run.get("ended_at")
    if ended and datetime.now(timezone.utc) - datetime.fromisoformat(ended) < RUNNING_WINDOW:
        return "运行中"
    return "已中止"


def _run_summary(run: dict, how: str, jobs_by_run: dict) -> dict:
    job = jobs_by_run.get(run["run_id"])
    return {
        "run_id": run["run_id"], "market": run["market"], "rounds": run["rounds"], "adopted": run["adopted"],
        "cost_usd": run["cost_usd"], "started_at": run["started_at"], "state": _run_state(run, job),
        "context_leak": run.get("context_leak"), "linked": how,
        "version": ((run.get("research") or {}).get("version")),
        "job_id": job["job_id"] if job else None, "job_state": job["state"] if job else None,
        "job_error": job.get("error") if job else None,
        "baseline_return": (((run.get("baseline") or {}).get("metrics")) or {}).get("annual_return_with_cost"),
        "best_return": _best_return(run["run_id"]),
    }


@app.get("/api/projects")
def projects():
    """研究项目：页面上锁定的研究规格（runtime/research，取最新版本）与示例（configs/research），及其运行与任务。

    运行日志中记录了研究规格编号的直接关联；更早的运行按市场与预测周期关联到示例（linked = "推断"）。
    """
    specs = []
    for rid in store.research_ids():
        vs = store.versions(rid)
        specs.append({**store.load_version(rid, vs[-1]), "versions": vs, "example": False})
    known = {s["id"] for s in specs}
    for p in sorted((CONFIGS / "research").glob("*.yaml")):
        ex = yaml.safe_load(p.read_text(encoding="utf-8"))
        if ex["id"] not in known:
            specs.append({**ex, "markets": [ex["market"]], "versions": [], "example": True})
    all_runs = [_json(p) for p in sorted((RESULTS / "runs").glob("*/run.json"), reverse=True)]
    all_jobs = store.list_jobs()
    jobs_by_run = {j["run_id"]: j for j in all_jobs if j.get("run_id")}
    items = []
    for spec in specs:
        linked = []
        for run in all_runs:
            rid = (run.get("research") or {}).get("id")
            if rid == spec["id"]:
                linked.append(_run_summary(run, "记录", jobs_by_run))
            elif spec["example"] and rid is None and run["market"] == spec["market"] and run["horizon"] == spec.get("horizon", 20):
                linked.append(_run_summary(run, "推断", jobs_by_run))
        exported = {r["run_id"] for r in linked}
        pending = [_job_public(j) for j in reversed(all_jobs)
                   if j["research_id"] == spec["id"] and j.get("kind") != "validate" and j.get("run_id") not in exported
                   and j["state"] in ("queued", "running", "failed")]
        states = [r["state"] for r in linked] + [j["state_zh"] for j in pending]
        status = ("运行中" if "运行中" in states else "排队中" if "排队中" in states else "已完成" if "已完成" in states
                  else "已中止" if states else "已锁定" if spec["versions"] else "示例")
        items.append({"id": spec["id"], "title": spec.get("title_zh") or spec.get("direction", ""),
                      "markets": spec.get("markets") or [spec.get("market")], "horizon": spec.get("horizon", 20),
                      "hypothesis": spec.get("hypothesis_zh") or spec.get("hypothesis", ""),
                      "versions": spec["versions"], "example": spec["example"], "status": status,
                      "runs": linked, "jobs": pending})
    return items


def _best_return(run_id: str) -> float | None:
    rounds = _json(RESULTS / "runs" / run_id / "rounds.json")
    adopted = [r["metrics"]["annual_return_with_cost"] for r in rounds if r.get("decision") and r.get("metrics")]
    return adopted[-1] if adopted else None


# ── 研究规格：起草、预览、锁定、运行（Spec 第 16 章）──────────────────────────────

class DraftIn(BaseModel):
    hypothesis: str = Field(min_length=10, max_length=4000)
    direction: str = Field("", max_length=1000)
    markets: list[str] = Field(min_length=1)
    horizon: int = 20
    mandate: str
    groups: list[str] = []
    include_past_runs: bool = True


def _check_markets(markets: list[str]) -> None:
    if not markets or any(m not in MARKETS for m in markets):
        raise HTTPException(422, f"市场必须是 {MARKETS} 中的一个或多个")


@app.get("/api/mandates")
def list_mandates():
    """模拟客户授权（configs/mandates），已解析为条款、挖掘约束与需要 AI 解释的自由文本条款。"""
    return [mandates.parse(m) for m in mandates.load_all()]


@app.post("/api/research/draft")
def research_draft(body: DraftIn):
    """AI 起草研究规格（RS-02）：约 30–60 秒。"""
    _check_markets(body.markets)
    if body.horizon not in HORIZONS:
        raise HTTPException(422, f"预测周期必须是 {HORIZONS} 之一")
    try:
        return drafting.make_draft(body.model_dump())
    except KeyError:
        raise HTTPException(404, f"授权不存在：{body.mandate}")
    except drafting.DraftError as e:
        raise HTTPException(502, str(e))


class PreviewIn(BaseModel):
    spec: dict
    market: str


@app.post("/api/research/preview")
def research_preview(body: PreviewIn):
    """提示词预览（RS-07）：调用 RD-Agent 场景子类运行时使用的同一个函数。"""
    _check_markets([body.market])
    spec = _prepare_spec(body.spec, lock=False)
    dates = split_dates(body.market, spec["horizon"])
    text = build_context(body.market, spec["horizon"], spec, dates["TEST_START"])
    return {"market": body.market, "test_start": dates["TEST_START"], "dates": dates, "text": text, "chars": len(text)}


EDITABLE = ("hypothesis", "rationale", "directions", "exclusions", "falsification")
LOCKED_BY = "研究员（演示，未登录）"


def _prepare_spec(raw: dict, lock: bool) -> dict:
    """整理研究规格：校验必填字段；以往运行只取假设方向与是否采纳（第 16.6 节）。"""
    spec = {k: raw.get(k) for k in (
        "id", "title_zh", "based_on", "mandate", "markets", "horizon", "hypothesis_original", "direction_original",
        "hypothesis", "hypothesis_zh", "rationale", "directions", "exclusions", "falsification", "data_needs",
        "constraints", "evidence", "questions", "run", "ai_draft")}
    spec["horizon"] = int(spec.get("horizon") or 20)
    spec["markets"] = spec.get("markets") or []
    _check_markets(spec["markets"])
    ev = spec.get("evidence") or {}
    spec["past_runs"] = (evidence.past_runs_lines(evidence.past_runs(spec["markets"], spec["horizon"]))
                         if ev.get("include_past_runs") else [])
    if not lock:
        return spec
    problems = []
    if not drafting.ID_PATTERN.match(str(spec.get("id") or "")):
        problems.append("编号须为 3–40 个小写字母、数字或下划线，以字母开头")
    for k, name in (("hypothesis", "规范化假设"), ("falsification", "可证伪测试")):
        if not str(spec.get(k) or "").strip():
            problems.append(f"{name}为必填")
    if not str((spec.get("rationale") or {}).get("note") or "").strip():
        problems.append("经济逻辑为必填")
    if any(not q.get("confirmed") for q in spec.get("questions") or []):
        problems.append("还有待确认事项未处理")
    if problems:
        raise HTTPException(422, "；".join(problems))
    original = (spec.get("ai_draft") or {}).get("original") or {}
    at = store.now()
    spec["edits"] = [{"field": k, "by": LOCKED_BY, "at": at} for k in EDITABLE if k in original and original[k] != spec.get(k)]
    spec["locked_by"] = LOCKED_BY
    return spec


@app.post("/api/research/versions")
def lock_version(body: dict):
    """锁定研究规格（RS-08）：生成新的版本号，此后不可修改。锁定前先做立项预检（BR-21）：命中阻断规则不允许锁定。"""
    spec = _prepare_spec(body, lock=True)
    pre = rulebook.evaluate(rule_ctx.setup_context(spec), ["setup"])
    if pre["blocked"]:
        hits = [f"{h['rule_id']} {h['title']}（{h['reason']}）" for h in pre["hits"] if h["severity"] == "block"]
        raise HTTPException(422, "立项预检命中阻断规则，不能锁定：" + "；".join(hits))
    spec["precheck_setup"] = pre
    return store.save_version(spec)


# ---------------------------------------------------------------- 地区规则包与预检（第 14.12 节）

@app.get("/api/rules/packs")
def rule_packs():
    """规则包（通用 + 各市场）与数据来源登记表。"""
    try:
        packs = rulebook.load_packs()
    except rulebook.RuleError as e:
        raise HTTPException(500, f"规则包格式错误：{e}")
    return {"packs": list(packs.values()), "sources": rulebook.load_sources(), "disclaimer": rulebook.DISCLAIMER,
            "severity_zh": rulebook.SEVERITY_ZH, "stage_zh": rulebook.STAGE_ZH,
            "category_zh": rulebook.CATEGORY_ZH, "status_zh": rulebook.STATUS_ZH}


class PrecheckIn(BaseModel):
    spec: dict
    overrides: dict = {}


@app.post("/api/rules/precheck")
def rules_precheck(body: PrecheckIn):
    """立项预检（锁定前，草稿页调用）：授权、市场、数据来源与许可。overrides 用于演示违规场景。"""
    spec = body.spec
    _check_markets(spec.get("markets") or [])
    return rulebook.evaluate(rule_ctx.setup_context(spec, body.overrides), ["setup"])


class RulesCheckIn(BaseModel):
    overrides: dict = {}


def _version_rules(research_id: str, version: int, overrides: dict) -> dict:
    spec = get_version(research_id, version)
    setup = spec.get("precheck_setup") if not overrides else None
    setup = setup or rulebook.evaluate(rule_ctx.setup_context(spec, overrides), ["setup"])
    st = study(research_id, version)
    has_pf = any((e.get("focus") or {}) for e in st["markets"].values())
    pctx = rule_ctx.portfolio_context(spec, st, overrides) if has_pf else None
    pf = rulebook.evaluate(pctx, ["portfolio"]) if has_pf else None
    return {"research_id": research_id, "version": version, "setup": setup, "portfolio": pf,
            "portfolio_source": {m: v["source"] for m, v in (pctx or {}).get("portfolio", {}).items()},
            "portfolio_note": None if has_pf else "还没有完成的运行：组合生成后才能做组合预检",
            "focus": {m: {"run_id": e.get("run_id"), "loop": (e.get("focus") or {}).get("loop")} for m, e in st["markets"].items()},
            "overrides": overrides}


@app.get("/api/research/{research_id}/versions/{version}/rules")
def version_rules(research_id: str, version: int):
    """一个锁定版本的两次预检：立项预检（锁定时存档）与组合预检（采纳轮次的持仓与诊断）。"""
    return _version_rules(research_id, version, {})


@app.post("/api/research/{research_id}/versions/{version}/rules")
def version_rules_check(research_id: str, version: int, body: RulesCheckIn):
    """带演示覆盖的预检：如把授权改为允许卖空、加入受限证券、追加卖空持仓，验证规则能否拦截（不写入任何文件）。"""
    return _version_rules(research_id, version, body.overrides)


@app.get("/api/research/{research_id}/versions")
def list_versions(research_id: str):
    if not drafting.ID_PATTERN.match(research_id):
        raise HTTPException(404, "研究项目不存在")
    return [store.load_version(research_id, v) for v in store.versions(research_id)]


@app.get("/api/research/{research_id}/versions/{version}")
def get_version(research_id: str, version: int):
    if not drafting.ID_PATTERN.match(research_id) or version not in store.versions(research_id):
        raise HTTPException(404, "版本不存在")
    return store.load_version(research_id, version)


def _study_market(m: str, runs: list[dict], jobs: list[dict], rule: dict) -> dict:
    """一个市场的结论：取最近一次完成的运行（没有则取最近一次运行），按可证伪测试判定各轮。"""
    jobs_by_run = {j["run_id"]: j for j in jobs if j.get("run_id")}
    active = next((j for j in reversed(jobs) if j["market"] == m and j["state"] in ("queued", "running")), None)
    mine = [r for r in runs if r["market"] == m]
    done = [r for r in mine if (jobs_by_run.get(r["run_id"]) or {}).get("state", "done" if r.get("status") == "done" else "") == "done"]
    run = (done or mine or [None])[0]
    entry = {"market": m, "primary": m == rule["primary"], "job": _job_public(active) if active else None,
             "attempts": len(mine)}
    if run is None:
        return entry | {"state": active["state"] if active else "none", "run_id": None, "focus": None, "rounds": []}
    job = jobs_by_run.get(run["run_id"])
    state = job["state"] if job else ("done" if run.get("status") == "done" else "incomplete")
    rounds = _json(RESULTS / "runs" / run["run_id"] / "rounds.json")
    judged = {v["loop"]: v for v in verdict.judge_rounds(run, rounds, rule)}
    qdir = RESULTS / "runs" / run["run_id"] / "quantile"
    rows = []
    for r in rounds:
        mt = r.get("metrics") or {}
        sig = _signal_sig(run["run_id"], r["loop"], run["horizon"])
        rows.append({"loop": r["loop"], "decision": r.get("decision"), "rank_ic": mt.get("rank_ic"),
                     "rank_ic_t": sig.get("t") if sig else None,
                     "return": mt.get("annual_return_with_cost"), "drawdown": mt.get("max_drawdown_with_cost"),
                     "passed": judged[r["loop"]]["passed"], "hypothesis": r.get("hypothesis")})
    focus = verdict.focus_round(rounds)
    fo = None
    if focus:
        q = qdir / f"{focus['loop']}.json"
        qd = _json(q) if q.exists() else None
        fo = {"loop": focus["loop"], "metrics": focus["metrics"], "checks": judged[focus["loop"]]["checks"],
              "passed": judged[focus["loop"]]["passed"], "hypothesis": focus.get("hypothesis"),
              "factors": [f["name"] for f in focus.get("factors") or []],
              "quantile": {k: qd[k] for k in ("spread_mean", "spread_positive_share", "tie_share", "groups")} if qd else None}
        sig = _signal_sig(run["run_id"], focus["loop"], run["horizon"])
        val = _validation(run["run_id"], focus["loop"])
        vjob = _validate_job(run["run_id"], focus["loop"])
        diag = RESULTS / "runs" / run["run_id"] / "diagnostics" / f"{focus['loop']}.json"
        bdiag = RESULTS / "runs" / run["run_id"] / "diagnostics" / "baseline.json"
        fo |= {"diagnostics": _json(diag) if diag.exists() else None,
               "baseline_diagnostics": _json(bdiag) if bdiag.exists() else None}
        fo |= {"significance": sig, "credibility": verdict.credibility(val, sig, rule),
               "validation": _validation_summary(val), "validation_job": _job_public(vjob) if vjob else None}
    return entry | {"state": state, "run_id": run["run_id"], "cost_usd": run["cost_usd"], "duration_s": run.get("duration_s"),
                    "baseline": (run.get("baseline") or {}).get("metrics"), "rounds": rows, "focus": fo}


def _signal_sig(run_id: str, loop, horizon: int) -> dict | None:
    """测试期逐日 rank IC 的显著性（导出的 signal 文件；Newey-West t 值）。"""
    p = RESULTS / "runs" / run_id / "signal" / f"{loop}.parquet"
    if not p.exists():
        return None
    return _cached_sig(p, p.stat().st_mtime, horizon)


@lru_cache(maxsize=256)
def _cached_sig(p: Path, mtime: float, horizon: int) -> dict:
    df = pd.read_parquet(p)
    return vmetrics.significance(df.set_index("date")["rank_ic"], horizon)


def _validation(run_id: str, loop) -> dict | None:
    p = RESULTS / "runs" / run_id / "validation" / f"{loop}.json"
    return _json(p) if p.exists() else None


def _validation_summary(v: dict | None) -> dict | None:
    """研究结论页用的摘要：去掉逐日曲线与每次安慰剂的明细。"""
    if not v:
        return None
    return {k: v.get(k) for k in ("status", "periods", "seeds", "started_at", "ended_at", "errors")} | {
        "ai": v.get("ai"), "baseline": v.get("baseline"),
        "placebo": {k: (v.get("placebo") or {}).get(k) for k in ("n", "share_ge_ai", "share_ge_baseline", "median")} if v.get("placebo") else None}


def _validate_job(run_id: str, loop) -> dict | None:
    jobs = [j for j in store.list_jobs() if j.get("kind") == "validate" and j.get("target_run") == run_id and j.get("loop") == loop]
    return jobs[-1] if jobs else None


@app.get("/api/runs/{run_id}/validation")
def run_validation(run_id: str):
    """可信度检查结果（factopia/validate）：各轮的完整结果（含逐日曲线与安慰剂明细）及相关任务。"""
    d = _run_dir(run_id) / "validation"
    items = {p.stem: _json(p) for p in sorted(d.glob("*.json"))} if d.exists() else {}
    jobs = [_job_public(j) for j in store.list_jobs() if j.get("kind") == "validate" and j.get("target_run") == run_id]
    return {"items": items, "jobs": jobs}


class ValidateIn(BaseModel):
    loop: int | None = None
    placebo_n: int = Field(20, ge=0, le=50)


@app.post("/api/runs/{run_id}/validate")
def start_validation(run_id: str, body: ValidateIn):
    """对某一轮（默认 RD-Agent 最终采纳的轮次）排入可信度检查；已有排队或运行中的同一检查时直接返回该任务。"""
    d = _run_dir(run_id)
    run, rounds = _json(d / "run.json"), _json(d / "rounds.json")
    loop = body.loop
    if loop is None:
        focus = verdict.focus_round(rounds)
        if focus is None:
            raise HTTPException(422, "这次运行没有被采纳的轮次，可在逐轮表中指定轮次")
        loop = focus["loop"]
    if not any(r["loop"] == loop and r.get("metrics") for r in rounds):
        raise HTTPException(422, f"第 {loop} 轮没有回测结果")
    existing = _validate_job(run_id, loop)
    if existing and existing["state"] in ("queued", "running"):
        return _job_public(existing)
    return _job_public(store.create_validate_job(run, loop, body.placebo_n))


def _study_findings(markets: dict, rule: dict) -> list[dict]:
    """平台按数据生成的观察（不是 AI 撰写）：tone 为 bad / warn / good。"""
    out = []
    sign = rule["ric_sign"]
    for m, e in markets.items():
        f, name = e.get("focus"), MARKET_ZH[m]
        if e["state"] != "done" or not f:
            continue
        mt, base = f["metrics"], e.get("baseline") or {}
        ric, r, br = mt.get("rank_ic"), mt.get("annual_return_with_cost"), base.get("annual_return_with_cost")
        ric_ok = ric is not None and ric * sign > 0
        if r is not None and br is not None and r > br and not ric_ok:
            out.append({"tone": "bad", "text": f"{name}：采纳轮次的收益高于起点（{br:.1%} → {r:.1%}），但 rank IC 为 {ric:.3f}，"
                                                f"方向与假设相反——收益提高不是因为模型会给股票排序（第 8 章 #21）。"})
        elif ric_ok:
            out.append({"tone": "good", "text": f"{name}：采纳轮次的 rank IC 为 {ric:.3f}，方向与假设一致"
                                                 + ("，但幅度很小。" if abs(ric) < 0.02 else "。")})
        q = f.get("quantile")
        if q and q.get("spread_mean") is not None and q["spread_mean"] * sign < 0:
            out.append({"tone": "warn", "text": f"{name}：打分最高的一组比最低的一组平均少赚 {abs(q['spread_mean']):.2%}（每 20 个交易日）。"})
        if q and (q.get("tie_share") or 0) > 0.2:
            out.append({"tone": "warn", "text": f"{name}：约 {q['tie_share']:.0%} 的股票每天得到完全相同的打分，排名先后取决于并列处理。"})
    for m, e in markets.items():
        f, name = e.get("focus"), MARKET_ZH[m]
        cred = (f or {}).get("credibility") or {}
        noise = cred.get("noise") or {}
        if noise.get("range"):
            lo, hi = noise["range"]
            rep_ = f"（RD-Agent 当时报告 {noise['reported']:.1%}）" if noise.get("reported") is not None else ""
            out.append({"tone": "warn", "text": f"{name}：同一个模型，只改变打分相同的股票的先后顺序，测试期扣成本年化在 "
                                                 f"{lo:.1%} 至 {hi:.1%} 之间{rep_}——收益对并列顺序很敏感，单次回测的收益不能作为证据。"})
        for c in cred.get("checks", []):
            if c["key"] == "holdout" and c["ok"] is not None:
                v = c["value"]
                t = f"，t = {v['rank_ic_t']:.1f}" if v.get("rank_ic_t") is not None else ""
                out.append({"tone": "good" if c["ok"] else "bad",
                            "text": f"{name}：2024 年以后（AI 从未见过的数据）rank IC 为 {v['rank_ic']:.3f}{t}，扣成本年化中位数 "
                                    f"{v['ai']:.1%}，起点 {v['baseline']:.1%}——{'规律在新数据上仍然成立' if c['ok'] else '规律没有在新数据上延续'}。"})
            if c["key"] == "placebo" and c["ok"] is not None:
                v = c["value"]
                out.append({"tone": "good" if c["ok"] else "bad",
                            "text": f"{name}：{v['n']} 组随机因子中有 {v['share']:.0%} 的收益不低于 AI 因子"
                                    f"{'，AI 因子明显好于随机' if c['ok'] else '，AI 因子与随机因子没有明显区别'}。"})
            if c["key"] == "reproduce" and c["ok"] is False:
                out.append({"tone": "bad", "text": f"{name}：重新计算的因子值与 RD-Agent 当时的只有 {c['value']:.0%} 一致，代码可能用到了未来数据。"})
    for m, e in markets.items():      # 研究结果诊断（第 14.11 节）
        d, name = ((e.get("focus") or {}).get("diagnostics")), MARKET_ZH[m]
        if not d:
            continue
        c, lq, s = d["cost"], d["liquidity"], d.get("signal") or {}
        be = c.get("breakeven_cost_multiple")
        out.append({"tone": "bad" if (c["net_excess"] or 0) <= 0 < (c["gross_excess"] or 0) else "warn" if be is not None and be < 2 else "good",
                    "text": f"{name}：扣成本前年化超额 {c['gross_excess']:.1%}，成本拖累 {c['cost_drag']:.1%}（其中税费约 {c['tax_share']:.0%}），"
                            f"扣成本后 {c['net_excess']:.1%}；年化换手 {c['turnover_annual']:.1f} 倍"
                            + (f"，成本达到现在的 {be:.1f} 倍时超额归零。" if be is not None and be > 0 else "。")
                            + (f"（{d['backtest_note']}）" if d.get("backtest_note") else "")})
        if lq.get("capacity_base"):
            out.append({"tone": "warn" if lq["capacity_base"] < 50e6 else "good",
                        "text": f"{name}：按每笔调仓不超过日均成交额 10% 估算，容量约 {lq['capacity_base'] / 1e6:,.0f} 百万 {d['base_currency']}；"
                                f"{lq['low_liquidity_weight']:.0%} 的持仓在成交额最低的 20% 股票中。"})
        if not d["fx"].get("same_currency") and d["fx"].get("fx_contribution") is not None:
            out.append({"tone": "warn", "text": f"{name}：换算为 {d['fx']['base']} 后，汇率对组合年化收益的贡献为 {d['fx']['fx_contribution']:+.1%}。"})
        if s.get("neutral_rank_ic") is not None and s.get("rank_ic"):
            kept = s.get("neutral_retained")
            ctrl = "、".join({"size": "规模", "momentum": "动量", "volatility": "波动", "reversal": "反转"}.get(c, c)
                            for c in s.get("neutral_controls") or [])
            base = f"{name}：剔除{ctrl}影响后 Rank IC 为 {s['neutral_rank_ic']:.3f}（原始 {s['rank_ic']:.3f}）"
            if s["rank_ic"] * sign <= 0:      # 原始方向已与假设相反：只报数字
                out.append({"tone": "warn", "text": base + "，原始方向与假设相反。"})
            else:
                out.append({"tone": "warn" if kept is not None and kept < 0.5 else "good",
                            "text": base + ("，大部分来自风格暴露。" if kept is not None and kept < 0.5 else "，不是简单的风格暴露。")})
    rows = [(m, x) for m, e in markets.items() for x in e.get("rounds", []) if x.get("rank_ic") is not None]
    if rows:
        ok = sum(1 for _, x in rows if x["rank_ic"] * sign > 0)
        out.append({"tone": "good" if ok > len(rows) / 2 else "warn",
                    "text": f"全部 {len(rows)} 轮中，{ok} 轮的 rank IC 方向与假设一致（假设预期{'为正' if sign > 0 else '为负'}）。"})
    adopted = [x for _, x in rows if x.get("decision")]
    bad = [x for x in adopted if x.get("passed") is False]
    if bad:
        out.append({"tone": "warn", "text": f"RD-Agent 采纳的 {len(adopted)} 轮中，{len(bad)} 轮没有通过可证伪测试："
                                             "RD-Agent 的 AI 评审主要看收益，与事先写好的标准不一致。"})
    return out


def _cross_market(markets: dict) -> dict | None:
    """三市场因子相关结构（BR-28）：各市场采纳轮次的逐月 Rank IC 两两相关（月份对齐，避免交易日历不同）。"""
    monthly = {}
    for m, e in markets.items():
        f = e.get("focus")
        p = RESULTS / "runs" / str(e.get("run_id")) / "signal" / f"{(f or {}).get('loop')}.parquet"
        if f and p.exists():
            sig = pd.read_parquet(p)
            monthly[m] = sig.set_index(pd.to_datetime(sig["date"]))["rank_ic"].resample("ME").mean()
    if len(monthly) < 2:
        return None
    df = pd.DataFrame(monthly).dropna()
    pairs = []
    ms = list(monthly)
    for i in range(len(ms)):
        for j in range(i + 1, len(ms)):
            c = df[ms[i]].corr(df[ms[j]]) if len(df) >= 6 else None
            pairs.append({"a": ms[i], "b": ms[j], "corr": None if c is None or pd.isna(c) else round(float(c), 3)})
    return {"months": len(df), "pairs": pairs,
            "note": "逐月 Rank IC 的相关：越高说明三个市场的信号同涨同跌（同一驱动因素），越低说明各市场由不同因素驱动"}


@app.get("/api/research/{research_id}/versions/{version}/study")
def study(research_id: str, version: int):
    """研究结论（Spec 第 14.5 节）：同一锁定版本在各市场的结果，按可证伪测试判定。"""
    spec = get_version(research_id, version)
    rule = verdict.rules(spec)
    runs = [r for r in (_json(p) for p in sorted((RESULTS / "runs").glob("*/run.json"), reverse=True))
            if (r.get("research") or {}).get("id") == research_id and (r.get("research") or {}).get("version") == version]
    jobs = [j for j in store.list_jobs() if j["research_id"] == research_id and j["version"] == version]
    markets = {m: _study_market(m, runs, jobs, rule) for m in rule["markets"]}
    diags = {m: (e.get("focus") or {}).get("diagnostics") for m, e in markets.items()}
    exp = store.RESEARCH_DIR / research_id / f"explain_v{version}.json"
    return {"research_id": research_id, "version": version, "hypothesis": spec.get("hypothesis"),
            "title": spec.get("title_zh") or spec.get("title"),
            "rule": {k: rule[k] for k in ("primary", "markets", "ric_sign", "sign_from_text", "text")},
            "overall": verdict.overall(markets, rule), "transfer": verdict.transfer(markets, rule, diags),
            "cross_market": _cross_market(markets),
            "markets": markets, "findings": _study_findings(markets, rule),
            "explanation": json.loads(exp.read_text(encoding="utf-8")) if exp.exists() else None}


@app.post("/api/research/{research_id}/versions/{version}/explain")
def explain_study(research_id: str, version: int):
    """AI 解释市场差异（BR-38）：基于平台事实清单，每句必须引用事实编号；存档供研究员确认。"""
    from factopia.research import explain as explaining
    st = study(research_id, version)
    diags = {m: (e.get("focus") or {}).get("diagnostics") for m, e in st["markets"].items()}
    try:
        out = explaining.explain(st, diags)
    except drafting.DraftError as e:
        raise HTTPException(503, str(e))
    path = store.RESEARCH_DIR / research_id / f"explain_v{version}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ---------------------------------------------------------------- 授权组合与报告（第 14.13 节）

def _portfolio_mod():
    from factopia import portfolio
    return portfolio


@app.get("/api/research/{research_id}/versions/{version}/portfolio")
def get_portfolio(research_id: str, version: int):
    """已构建的授权组合（没有则 summary 为 null）与研究员记录的反馈决定。"""
    get_version(research_id, version)
    P = _portfolio_mod()
    return {"summary": P.load(research_id, version), "feedback": P.feedback(research_id, version)}


@app.post("/api/research/{research_id}/versions/{version}/portfolio")
def build_portfolio(research_id: str, version: int):
    """按授权构建组合并回测（约数秒）：多币种、汇率拆分、Brinson 归因、反馈建议。"""
    P = _portfolio_mod()
    spec = get_version(research_id, version)
    try:
        summary = P.build(spec, study(research_id, version))
    except P.PortfolioError as e:
        raise HTTPException(422, str(e))
    return {"summary": summary, "feedback": P.feedback(research_id, version)}


@app.get("/api/research/{research_id}/versions/{version}/portfolio/series")
def portfolio_series(research_id: str, version: int):
    """净值序列（基础币种）与各市场权重，供组合台画图。"""
    get_version(research_id, version)
    p = _portfolio_mod().out_dir(research_id, version) / "daily.parquet"
    if not p.exists():
        raise HTTPException(404, "尚未构建组合")
    d = pd.read_parquet(p)
    ms = [c[:-2] for c in d.columns if c.endswith("_w")]
    return {"dates": d["date"].dt.strftime("%Y-%m-%d").tolist(),
            "port": [round(float(x), 6) for x in (1 + d["port"]).cumprod()],
            "bench": [round(float(x), 6) for x in (1 + d["bench"]).cumprod()],
            "weights": {m: [round(float(x), 4) for x in d[f"{m}_w"]] for m in ms}}


class FeedbackIn(BaseModel):
    market: str
    action: str
    note: str = ""
    by: str = "研究员（演示，未登录）"


@app.post("/api/research/{research_id}/versions/{version}/portfolio/feedback")
def portfolio_feedback(research_id: str, version: int, body: FeedbackIn):
    """记录研究员对反馈建议的决定（BR-32）：保留 / 进一步研究 / 限定地区 / 重新校准 / 停用。"""
    P = _portfolio_mod()
    if P.load(research_id, version) is None:
        raise HTTPException(404, "尚未构建组合")
    if body.action not in P.ACTIONS or body.market not in MARKETS:
        raise HTTPException(422, f"决定须为 {list(P.ACTIONS)} 之一")
    return P.record_feedback(research_id, version, body.model_dump() | {"action_zh": P.ACTIONS[body.action]})


REPORTS = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime")) / "reports"
REPORT_NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}_v\d+_\d{14}\.html$")


@app.post("/api/research/{research_id}/versions/{version}/report")
def make_report(research_id: str, version: int):
    """生成研究报告（BR-40；RPT-01 至 03）：中文主报告 + 英文 / 法文证据附件，单文件 HTML，存档于 runtime/reports。"""
    from factopia.portfolio import report
    P = _portfolio_mod()
    spec = get_version(research_id, version)
    st = study(research_id, version)
    pf = P.load(research_id, version)
    dp = P.out_dir(research_id, version) / "daily.parquet"
    exp = store.RESEARCH_DIR / research_id / f"explain_v{version}.json"
    qp = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime")) / "data" / "standardized" / "quality_report.json"
    pit = RESULTS / "pit_status.json"
    data = {"spec": spec, "study": st, "rules": _version_rules(research_id, version, {}), "portfolio": pf,
            "daily": pd.read_parquet(dp).set_index("date") if dp.exists() and pf else None,
            "pit": _json(pit) if pit.exists() else None, "quality": _json(qp) if qp.exists() else None,
            "explanation": json.loads(exp.read_text(encoding="utf-8")) if exp.exists() else None,
            "feedback": P.feedback(research_id, version)}
    page = report.render(data)
    REPORTS.mkdir(parents=True, exist_ok=True)
    name = f"{research_id}_v{version}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.html"
    (REPORTS / name).write_text(page, encoding="utf-8")
    return {"name": name, "url": f"/api/reports/{name}", "bytes": len(page.encode("utf-8")), "has_portfolio": pf is not None}


@app.get("/api/research/{research_id}/versions/{version}/reports")
def list_reports(research_id: str, version: int):
    return [{"name": p.name, "url": f"/api/reports/{p.name}", "bytes": p.stat().st_size}
            for p in sorted(REPORTS.glob(f"{research_id}_v{version}_*.html"), reverse=True)] if REPORTS.exists() else []


@app.get("/api/reports/{name}")
def get_report(name: str):
    from fastapi.responses import HTMLResponse
    if not REPORT_NAME.match(name) or not (REPORTS / name).exists():
        raise HTTPException(404, "报告不存在")
    return HTMLResponse((REPORTS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 跨地区审批与审计（第 14.14 节）

def _approval_mod():
    from factopia import approval
    return approval


SUBMITTER = "发起研究员（演示，未登录）"


def _materials(research_id: str, version: int) -> dict:
    """提交材料：研究规格、结论、两次预检、授权组合、最新报告、数据质量、AI 解释、待复核翻译数。"""
    P = _portfolio_mod()
    spec = get_version(research_id, version)
    reps = sorted(REPORTS.glob(f"{research_id}_v{version}_*.html"), reverse=True) if REPORTS.exists() else []
    exp = store.RESEARCH_DIR / research_id / f"explain_v{version}.json"
    qp = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime")) / "data" / "standardized" / "quality_report.json"
    uses_text = any(d.get("category") == "text" for d in spec.get("data_needs") or [])
    pending = treview.queue(None, "pending", 1)["counts"]["pending"] if uses_text else 0
    return {"spec": spec, "study": study(research_id, version), "rules": _version_rules(research_id, version, {}),
            "portfolio": P.load(research_id, version), "report": {"name": reps[0].name, "path": reps[0]} if reps else None,
            "quality": _json(qp) if qp.exists() else None, "text_pending": pending,
            "explanation": json.loads(exp.read_text(encoding="utf-8")) if exp.exists() else None,
            "translations": {"uses_text": uses_text, "pending_reviews": pending}}


@app.get("/api/research/{research_id}/versions/{version}/submission-check")
def submission_check(research_id: str, version: int):
    """WB-05：提交审批前的固定顺序检查。"""
    A = _approval_mod()
    m = _materials(research_id, version)
    return {"ready": not A.preconditions(m), "missing": A.preconditions(m),
            "report": m["report"]["name"] if m["report"] else None,
            "open": [s for s in A.list_all(research_id) if s["version"] == version and s["status"] == "in_review"]}


class SubmitIn(BaseModel):
    research_id: str
    version: int
    note: str = ""


@app.get("/api/approvals")
def list_approvals(research_id: str | None = None):
    return {"items": _approval_mod().list_all(research_id), "roles": _approval_mod().roles()}


@app.post("/api/approvals")
def submit_approval(body: SubmitIn):
    A = _approval_mod()
    open_ = [s for s in A.list_all(body.research_id) if s["version"] == body.version and s["status"] == "in_review"]
    if open_:
        raise HTTPException(422, f"该版本已有审批中的提交：{open_[0]['id']}")
    try:
        return A.submit(_materials(body.research_id, body.version), SUBMITTER, body.note)
    except A.ApprovalError as e:
        raise HTTPException(422, str(e))


def _approval_detail(sid: str) -> dict:
    A = _approval_mod()
    try:
        s = A.load(sid)
    except A.ApprovalError as e:
        raise HTTPException(404, str(e))
    cur = A.current_fingerprint(sid, {"portfolio": _portfolio_mod().load(s["research_id"], s["version"])})
    return {"submission": s, "summary": A.summary(s), "events": A.events(sid), "chain": A.verify_chain(sid),
            "materials_current": cur == s["fingerprint"], "materials_note": None if cur == s["fingerprint"] else cur.split(":", 1)[-1] + "在提交后发生变化",
            "action_zh": A.ACTION_ZH}


@app.get("/api/approvals/{sid}")
def get_approval(sid: str):
    return _approval_detail(sid)


class ActIn(BaseModel):
    role: str
    action: str
    note: str = ""
    rule: str | None = None
    markets: list[str] | None = None
    thresholds: dict | None = None


@app.post("/api/approvals/{sid}/actions")
def approval_action(sid: str, body: ActIn):
    """审批操作：当前角色才能处理当前步骤；例外须写理由；运营完成部署核对时校验材料指纹与部署范围。"""
    A = _approval_mod()
    by = f"模拟用户（{body.role}）"
    try:
        s = A.load(sid)
        cur = A.current_fingerprint(sid, {"portfolio": _portfolio_mod().load(s["research_id"], s["version"])})
        A.act(sid, body.role, by, body.action, body.note, body.rule, body.markets, body.thresholds, cur)
    except A.ApprovalError as e:
        raise HTTPException(422, str(e))
    return _approval_detail(sid)


@app.get("/api/approvals/{sid}/evidence")
def approval_evidence(sid: str):
    """审计证据包（zip）：提交材料、全部事件（哈希链）、报告、规则包、因子代码、文件清单与说明。"""
    from fastapi.responses import Response
    A = _approval_mod()
    try:
        name, data = A.evidence_zip(sid)
    except A.ApprovalError as e:
        raise HTTPException(404, str(e))
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


class RunIn(BaseModel):
    markets: list[str] | None = None
    loops: int = Field(5, ge=1, le=20)
    budget_usd: float = Field(2.0, gt=0, le=20)


@app.post("/api/research/{research_id}/versions/{version}/runs")
def start_runs(research_id: str, version: int, body: RunIn):
    """在所选市场分别运行同一个锁定版本（RS-09）；任务按顺序排队，由后台运行器执行。"""
    spec = get_version(research_id, version)
    markets = body.markets or spec["markets"]
    if any(m not in spec["markets"] for m in markets):
        raise HTTPException(422, f"版本 v{version} 只包含市场 {spec['markets']}")
    return store.create_jobs(research_id, version, markets, spec["horizon"], body.loops, body.budget_usd)


@app.get("/api/jobs")
def list_jobs(batch: str | None = None):
    return [_job_public(j) for j in store.list_jobs() if batch is None or j["batch_id"] == batch]


JOB_ID = re.compile(r"^\d{14}_[0-9a-f]{4}_\d+_[a-z]{2}$")


def _load_job(job_id: str) -> dict:
    if not JOB_ID.match(job_id):
        raise HTTPException(404, "任务不存在")
    try:
        return store.load_job(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "任务不存在")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """任务状态与 RD-Agent 控制台输出的最后 40 行（准备数据阶段尚无进度事件时显示）。"""
    job = _load_job(job_id)
    path = store.log_path(job_id)
    tail = path.read_text(errors="replace").splitlines()[-40:] if path.exists() else []
    return {**_job_public(job), "log_tail": tail, "exported": bool(job.get("run_id")) and (RESULTS / "runs" / job["run_id"] / "run.json").exists()}


@app.get("/api/jobs/{job_id}/live")
def job_live(job_id: str):
    """运行中的实时状态（运行器每 3 秒更新）：当前步骤、最近活动、各次回测结果。"""
    _load_job(job_id)
    return store.load_live(job_id) or {}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = _load_job(job_id)
    if job["state"] not in ("failed", "cancelled", "budget_stopped"):
        raise HTTPException(409, "只有失败或已停止的任务可以重新运行")
    return store.retry_job(job)


@app.get("/api/runner")
def runner_status():
    """后台运行器：是否暂停（及原因）、排队与运行中的任务数。"""
    jobs = store.list_jobs()
    return {"paused": store.paused(), "queued": sum(j["state"] == "queued" for j in jobs),
            "running": sum(j["state"] == "running" for j in jobs)}


@app.post("/api/runner/resume")
def runner_resume():
    """处理完暂停原因（如充值）后继续运行排队中的任务。"""
    store.resume()
    return runner_status()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _load_job(job_id)
    if job["state"] in store.FINAL_STATES:
        raise HTTPException(409, "任务已结束")
    store.request_cancel(job_id)
    return {"ok": True}


@app.get("/api/research/examples")
def research_examples():
    """研究规格示例（configs/research），供研究立项与审核页面预填。"""
    items = []
    for p in sorted((CONFIGS / "research").glob("*.yaml")):
        items.append(yaml.safe_load(p.read_text(encoding="utf-8")))
    return items


@app.get("/api/context/{market}")
def market_context(market: str, lang: str = Query("zh", pattern="^(zh|en)$")):
    """团队审核过的市场介绍（configs/rdagent/context）。"""
    if market not in MARKETS:
        raise HTTPException(404, "市场不存在")
    path = CONFIGS / "rdagent" / "context" / f"{market}.{lang}.md"
    return {"market": market, "lang": lang, "markdown": path.read_text(encoding="utf-8")}


# ---------------------------------------------------------------- 情报台：多语言公告与文本信号（Spec 第 11 章）
from factopia.text import common as tcommon            # noqa: E402
from factopia.text import review as treview            # noqa: E402
from factopia.text import search as tsearch            # noqa: E402


def _text_ds(dataset: str) -> Path:
    if dataset not in tcommon.DATASETS:
        raise HTTPException(404, f"数据集不存在：{dataset}")
    return tcommon.ds_dir(dataset)


@app.get("/api/text/overview")
def text_overview():
    """各数据集（法国真实 / 香港、英国模拟）的规模、来源、许可、检验结果与三市场对比。"""
    out = []
    for ds, cfg in tcommon.DATASETS.items():
        d = tcommon.ds_dir(ds)
        s = tcommon.read_json(d / "summary.json") or {}
        out.append({"dataset": ds, "market": cfg["market"], "market_zh": MARKET_ZH[cfg["market"]],
                    "simulated": cfg["simulated"], "source": cfg["source"], "licence": cfg["licence"],
                    "processing_delay_min": cfg["processing_delay_min"], "ready": bool(s),
                    "n_filings": s.get("n_filings", 0), "n_companies": s.get("n_companies", 0),
                    "n_changes": s.get("n_changes", 0), "hold_days": s.get("hold_days"),
                    "evaluation": s.get("evaluation"), "truth": tcommon.read_json(d / "truth.json")})
    g = tcommon.glossary()
    concepts = [{"concept": c, "zh": v["zh_name"], "en": v["en_name"], "fr": v["fr_name"], "terms": v["terms"]}
                for c, v in g["concepts"].items()]
    return {"datasets": out, "compare": tcommon.read_json(tcommon.TEXT / "compare.json"),
            "glossary_version": g["version"], "concepts": concepts,
            "review_counts": treview.queue(limit=0)["counts"]}


@app.get("/api/text/{dataset}/filings")
def text_filings(dataset: str, instrument: str | None = None, limit: int = Query(200, le=2000)):
    s = tcommon.read_json(_text_ds(dataset) / "summary.json")
    if not s:
        raise HTTPException(404, "该数据集尚未生成文本信号（先运行 python -m factopia.text all）")
    items = [f for f in s["filings"] if not instrument or f["instrument"] == instrument]
    return {"dataset": dataset, "simulated": s["simulated"], "source": s["source"], "licence": s["licence"],
            "total": len(items), "items": items[:limit]}


@app.get("/api/text/{dataset}/filings/{doc_id}")
def text_filing(dataset: str, doc_id: str):
    """一篇公告：原文段落（不改动）+ 各段的概念标签、译文、置信度、复核状态。"""
    d = _text_ds(dataset)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,80}", doc_id):
        raise HTTPException(404, "公告不存在")
    doc = tcommon.read_json(d / "docs" / f"{doc_id}.json")
    if not doc:
        raise HTTPException(404, "公告不存在")
    lab = tcommon.read_json(d / "labels" / f"{doc_id}.json") or {"labels": []}
    s = tcommon.read_json(d / "summary.json") or {"filings": []}
    meta = next((f for f in s["filings"] if f["id"] == doc_id), None)
    dec = treview.decisions()
    names = tcommon.concept_names()
    by_i = {x["i"]: treview.apply(x, dec.get(treview.key(dataset, doc_id, x["i"]))) for x in lab["labels"]}
    paras = []
    for p in doc["paragraphs"]:
        x = by_i.get(p["i"])
        paras.append({"i": p["i"], "text": p["text"],
                      "concepts": [{**c, "name": names.get(c["concept"], c["concept"])} for c in x["concepts"]] if x else [],
                      "translation": (x or {}).get("translation") or {}, "translation_confidence": (x or {}).get("translation_confidence"),
                      "flags": [{"code": f, "label": treview.FLAG_ZH.get(f, f)} for f in (x or {}).get("flags", [])],
                      "review_status": (x or {}).get("review_status"), "method": (x or {}).get("method")})
    return {"dataset": dataset, "simulated": tcommon.DATASETS[dataset]["simulated"], "meta": meta,
            "model": lab.get("model"), "prompt_version": lab.get("prompt_version"),
            "glossary_version": lab.get("glossary_version"), "labelled_at": lab.get("labelled_at"),
            "pages_total": doc.get("pages_total"), "pages_read": doc.get("pages_read"), "paragraphs": paras}


@app.get("/api/text/search")
def text_search(q: str = "", dataset: list[str] | None = Query(None), concept: list[str] | None = Query(None),
                limit: int = Query(50, le=200)):
    for ds in dataset or []:
        _text_ds(ds)
    return tsearch.search(q, dataset, concept, limit)


@app.get("/api/text/review")
def text_review_queue(status: str = Query("pending", pattern="^(pending|done|all)$"), dataset: str | None = None,
                      limit: int = Query(100, le=500)):
    if dataset:
        _text_ds(dataset)
    return treview.queue(dataset, status, limit)


class SummaryIn(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    dataset: list[str] | None = None
    concept: list[str] | None = None


@app.post("/api/text/summary")
def text_summary(body: SummaryIn):
    """对检索结果做 AI 摘要；每句结论带引用编号，无引用的句子被丢弃（BR-10）。"""
    for ds in body.dataset or []:
        _text_ds(ds)
    hits = tsearch.search(body.q, body.dataset, body.concept, 12)["results"]
    try:
        return tsearch.summarize(body.q, hits)
    except drafting.DraftError as e:
        raise HTTPException(503, str(e))


class ReviewIn(BaseModel):
    dataset: str
    doc_id: str
    i: int
    action: str
    reviewer: str
    note: str = ""
    concepts: list[dict] | None = None
    translation: dict | None = None


@app.post("/api/text/review")
def text_review_decide(body: ReviewIn):
    _text_ds(body.dataset)
    try:
        return treview.decide(body.dataset, body.doc_id, body.i, body.action, body.reviewer, body.note,
                              body.concepts, body.translation)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---------------------------------------------------------------- 数据台：证券主数据、公司行动、数据字典、时间点一致性（Spec 第 14.10 节）
REFDATA = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime")) / "refdata"


def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _ref_parquet(name: str) -> pd.DataFrame:
    p = REFDATA / f"{name}.parquet"
    if not p.exists():
        raise HTTPException(404, "参考数据尚未生成（先运行 python -m factopia.refdata all）")
    return pd.read_parquet(p)


@app.get("/api/pit")
def pit_status():
    """各数据域的时间点一致性状态；所有结果页面据此标注（BR-18）。"""
    return _json(RESULTS / "pit_status.json")


@app.get("/api/refdata/summary")
def refdata_summary():
    def rd(name):
        p = REFDATA / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    pit = RESULTS / "pit_status.json"
    return {"master": rd("master_summary.json"), "actions": rd("actions_summary.json"),
            "pit": json.loads(pit.read_text(encoding="utf-8")) if pit.exists() else None}


@app.get("/api/refdata/securities")
def refdata_securities(market: str | None = Query(None, pattern="^(hk|uk|fr)$")):
    sec = _ref_parquet("securities")
    rel = _ref_parquet("relations")
    if market:
        sec = sec[sec["market"] == market]
    multi = sec.groupby("issuer_id")["market"].nunique()
    sec = sec.assign(multi_market=sec["issuer_id"].map(multi).fillna(1) > 1)
    rel = rel[rel["issuer_id"].isin(sec["issuer_id"])]
    return {"securities": _records(sec), "relations": _records(rel)}


@app.get("/api/refdata/actions")
def refdata_actions(symbol: str | None = None, type: str | None = None, limit: int = Query(500, le=5000)):
    df = _ref_parquet("corporate_actions")
    if symbol:
        df = df[df["symbol"] == symbol]
    if type:
        df = df[df["type"] == type]
    counts = df["type"].value_counts().to_dict()
    return {"total": len(df), "counts": counts, "items": _records(df.sort_values("date", ascending=False).head(limit))}


@app.get("/api/refdata/dictionary")
def refdata_dictionary():
    f = yaml.safe_load((CONFIGS / "refdata" / "fields.yaml").read_text(encoding="utf-8"))
    m = yaml.safe_load((CONFIGS / "refdata" / "markets.yaml").read_text(encoding="utf-8"))
    mem = {}
    for mk in MARKETS:
        p = CONFIGS / "universe" / "membership" / f"{mk}.csv"
        if p.exists():
            d = pd.read_csv(p, dtype=str).fillna("")
            mem[mk] = {"rows": len(d), "sources": d["source"].value_counts().to_dict(),
                       "point_in_time": bool(len(d)) and not (d["source"] == "current_list").any()}
    return {"fields": f, "markets": m, "membership": mem}
