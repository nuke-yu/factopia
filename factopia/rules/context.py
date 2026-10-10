"""预检输入：把研究规格、模拟授权、股票池、证券主数据、持仓与诊断整理为规则引擎的上下文。

- 立项预检（setup）只需要研究规格与授权；
- 组合预检（portfolio）还需要每个市场采纳轮次的持仓与诊断（研究结论 study 中的 focus 轮次）。
持仓文件的股票代码为 Qlib 格式（0700_HK），这里转换为证券主数据的代码（0700.HK）。
"""
import csv
import json
import os
from pathlib import Path

import pandas as pd
import yaml

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(RUNTIME / "results")))


def _symbol(code: str) -> str:
    head, _, tail = code.rpartition("_")
    return f"{head}.{tail}" if head else code


def mandate(mandate_id: str | None) -> dict:
    for p in sorted((CONFIGS / "mandates").glob("*.yaml")):
        m = yaml.safe_load(p.read_text(encoding="utf-8"))
        if m["id"] == mandate_id:
            return m
    return {"id": mandate_id, "long_only": True, "restricted_securities": []}


def universe(markets: list[str]) -> dict[str, list[str]]:
    out = {}
    for m in markets:
        p = CONFIGS / "universe" / "membership" / f"{m}.csv"
        out[m] = sorted({r["symbol"] for r in csv.DictReader(p.open(encoding="utf-8"))}) if p.exists() else []
    return out


def industries() -> dict[str, str]:
    p = RUNTIME / "refdata" / "securities.parquet"
    if not p.exists():
        return {}
    s = pd.read_parquet(p, columns=["symbol", "industry"])
    return {k: v for k, v in zip(s["symbol"], s["industry"]) if v}


def setup_context(spec: dict, overrides: dict | None = None) -> dict:
    """overrides：演示 / 测试用，覆盖授权条款或追加数据来源（如 {"mandate": {"long_only": False}, "extra_sources": [["hk", "hkexnews"]]}）。"""
    overrides = overrides or {}
    md = mandate(spec.get("mandate")) | (overrides.get("mandate") or {})
    markets = spec.get("markets") or []
    cats = {d.get("category") for d in spec.get("data_needs") or [] if d.get("category")} | {"price_volume", "benchmark"}
    if len(markets) > 1 or md.get("base_currency"):
        cats.add("fx")
    return {"markets": markets, "mandate": md, "data_categories": cats, "universe": universe(markets),
            "industries": industries(), "extra_sources": [tuple(x) for x in overrides.get("extra_sources") or []],
            "pit": None, "portfolio": None}


def portfolio_context(spec: dict, study: dict, overrides: dict | None = None) -> dict:
    """study：研究结论（factopia.api.main.study 的输出）。overrides["positions"]：{市场: [{date, symbol, weight}]} 用于合成违规演示。"""
    ctx = setup_context(spec, overrides)
    pf = {}
    for m, e in (study.get("markets") or {}).items():
        f = e.get("focus")
        if not f:
            continue
        p = RESULTS / "runs" / str(e["run_id"]) / "positions" / f"{f['loop']}.parquet"
        pos = None
        if p.exists():
            pos = pd.read_parquet(p, columns=["date", "instrument", "weight"])
            pos["symbol"] = pos["instrument"].map(_symbol)
        extra = ((overrides or {}).get("positions") or {}).get(m)
        if extra:
            add = pd.DataFrame(extra)
            add["date"] = pd.to_datetime(add["date"])
            pos = add if pos is None else pd.concat([pos, add], ignore_index=True)
        pf[m] = {"run_id": e["run_id"], "loop": f["loop"], "positions": pos, "diagnostics": f.get("diagnostics")}
    ctx["portfolio"] = pf
    pit = RESULTS / "pit_status.json"
    ctx["pit"] = json.loads(pit.read_text(encoding="utf-8")) if pit.exists() else None
    return ctx
