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
from factopia.research import evidence, mandates, store
from factopia.research.context import build_context
from factopia.research.periods import HORIZONS, split_dates

RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUN_ID = re.compile(r"^[a-z]{2}_h\d{1,2}_\d{14}$")
ROUND_KEY = re.compile(r"^(baseline|\d{1,3})$")
MARKETS = ("hk", "uk", "fr")

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
    return {"run": {**run, "state": _run_state(run, job)}, "rounds": _json(d / "rounds.json"),
            "job": _job_public(job) if job else None, "batch": batch}


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
                   if j["research_id"] == spec["id"] and j.get("run_id") not in exported and j["state"] in ("queued", "running", "failed")]
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
    """锁定研究规格（RS-08）：生成新的版本号，此后不可修改。"""
    spec = store.save_version(_prepare_spec(body, lock=True))
    return spec


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
