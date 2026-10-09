"""Factopia 后端接口。

运行（独立服务，见 docker/api/Dockerfile 与 docker-compose.yml 的 api 服务）：
    uvicorn factopia.api.main:app --host 0.0.0.0 --port 8000

数据来自 /workspace/runtime/results（只读挂载），由 factopia.export 导出；研究规格示例与市场介绍来自 /workspace/configs。
"""
import json
import os
import re
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
