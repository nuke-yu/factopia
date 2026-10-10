"""行情来源（文本信号研究，BR-49；Spec 第 14.17 节）。

研究运行默认使用真实市场的标准化行情（runtime/data/standardized/<市场>.parquet）与基准指数。
文本信号研究在香港、英国使用模拟文本数据集（虚构公司，"模拟数据，仅用于流程演示"），其行情也是模拟的、
股票池是虚构公司，没有成交量与指数：基准改为股票池等权，成交额相关的诊断（规模、流动性、容量）留空。

运行结果在 run.json 的 price_source 中记录来源（{"dataset": "demo_hk", "simulated": true, ...}）；
诊断、组合、监控按运行读取同一来源，保证模拟数据与真实数据不会混用。
"""
import os
from pathlib import Path

import pandas as pd

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
SIMULATED_LABEL = "模拟数据，仅用于流程演示"
# 文本信号研究每个市场使用的文本数据集（与 factopia/text/common.py 的 DATASETS 一致）
TEXT_DATASETS = {"hk": "demo_hk", "uk": "demo_uk", "fr": "fr"}
SIMULATED = {"demo_hk", "demo_uk"}


def of_run(run: dict | None) -> str | None:
    """运行使用的模拟行情数据集；真实行情返回 None。"""
    ps = (run or {}).get("price_source") or {}
    return ps.get("dataset") if ps.get("simulated") else None


def describe(dataset: str | None) -> dict:
    if dataset in SIMULATED:
        return {"dataset": dataset, "simulated": True, "label": SIMULATED_LABEL,
                "benchmark": "股票池等权（模拟数据没有指数）", "volume": False}
    return {"dataset": None, "simulated": False, "label": "真实行情（标准化数据）", "benchmark": "基准指数", "volume": True}


def _demo(dataset: str) -> pd.DataFrame:
    df = pd.read_parquet(RUNTIME / "text" / dataset / "prices.parquet", columns=["date", "symbol", "close"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    return df


def close(market: str, dataset: str | None = None) -> pd.DataFrame:
    """日期 × 股票 收盘价（复权）。"""
    if dataset in SIMULATED:
        df = _demo(dataset)
    else:
        df = pd.read_parquet(RUNTIME / "data" / "standardized" / f"{market}.parquet", columns=["symbol", "date", "close"])
        df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="symbol", values="close").sort_index()


def volume(market: str, dataset: str | None = None) -> pd.DataFrame | None:
    """日期 × 股票 成交量；模拟数据没有成交量，返回 None。"""
    if dataset in SIMULATED:
        return None
    df = pd.read_parquet(RUNTIME / "data" / "standardized" / f"{market}.parquet", columns=["symbol", "date", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="symbol", values="volume").sort_index()


def index(market: str, dataset: str | None = None) -> pd.Series:
    """基准点位：真实市场为指数收盘；模拟数据为股票池等权（逐日平均收益连乘）。"""
    if dataset in SIMULATED:
        r = close(market, dataset).pct_change(fill_method=None).mean(axis=1).fillna(0.0)
        return (1 + r).cumprod().rename("close")
    ix = pd.read_parquet(RUNTIME / "data" / "raw" / market / "index.parquet", columns=["date", "close"])
    ix["date"] = pd.to_datetime(ix["date"]).dt.tz_localize(None).dt.normalize()
    return ix.set_index("date")["close"].sort_index()


def universe(market: str, dataset: str | None = None) -> list[str] | None:
    """模拟数据集的股票池（全部虚构公司）；真实市场返回 None（由调用方读取成分股名单）。"""
    if dataset in SIMULATED:
        return sorted(_demo(dataset)["symbol"].unique())
    return None
