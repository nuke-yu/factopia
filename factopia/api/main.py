"""Factopia 后端接口。

运行（独立服务，见 docker/api/Dockerfile 与 docker-compose.yml 的 api 服务）：
    uvicorn factopia.api.main:app --host 0.0.0.0 --port 8000

数据来自 /workspace/runtime/results（只读挂载），由 factopia.export 导出；研究规格示例与市场介绍来自 /workspace/configs。
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

RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUN_ID = re.compile(r"^[a-z]{2}_h\d{1,2}_\d{14}$")
ROUND_KEY = re.compile(r"^(baseline|\d{1,3})$")
MARKETS = ("hk", "uk", "fr")

app = FastAPI(title="Factopia API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


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
    return {"run": _json(d / "run.json"), "rounds": _json(d / "rounds.json")}


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


RUNNING_WINDOW = timedelta(minutes=30)  # 未完成的运行在最后一次记录后 30 分钟内视为"运行中"


def _run_state(run: dict) -> str:
    if run.get("status") == "done":
        return "已完成"
    ended = run.get("ended_at")
    if ended and datetime.now(timezone.utc) - datetime.fromisoformat(ended) < RUNNING_WINDOW:
        return "运行中"
    return "已中止"


@app.get("/api/projects")
def projects():
    """研究项目：每份研究规格（configs/research）及其关联的运行。

    运行日志中记录了研究规格编号的直接关联；更早的运行按市场与预测周期推断（linked = "推断"）。
    """
    specs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((CONFIGS / "research").glob("*.yaml"))]
    all_runs = [_json(p) for p in sorted((RESULTS / "runs").glob("*/run.json"), reverse=True)]
    items = []
    for spec in specs:
        linked = []
        for run in all_runs:
            rid = (run.get("research") or {}).get("id")
            if rid == spec["id"]:
                how = "记录"
            elif rid is None and run["market"] == spec["market"] and run["horizon"] == spec.get("horizon", 20):
                how = "推断"
            else:
                continue
            linked.append({
                "run_id": run["run_id"], "market": run["market"], "rounds": run["rounds"], "adopted": run["adopted"],
                "cost_usd": run["cost_usd"], "started_at": run["started_at"], "state": _run_state(run),
                "context_leak": run.get("context_leak"), "linked": how,
                "baseline_return": (((run.get("baseline") or {}).get("metrics")) or {}).get("annual_return_with_cost"),
                "best_return": _best_return(run["run_id"]),
            })
        states = [r["state"] for r in linked]
        status = "运行中" if "运行中" in states else "已完成" if "已完成" in states else "已中止" if linked else "草稿"
        items.append({"id": spec["id"], "market": spec["market"], "horizon": spec.get("horizon", 20),
                      "direction": spec.get("direction", ""), "hypothesis": spec.get("hypothesis", ""),
                      "status": status, "runs": linked})
    return items


def _best_return(run_id: str) -> float | None:
    rounds = _json(RESULTS / "runs" / run_id / "rounds.json")
    adopted = [r["metrics"]["annual_return_with_cost"] for r in rounds if r.get("decision") and r.get("metrics")]
    return adopted[-1] if adopted else None


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
