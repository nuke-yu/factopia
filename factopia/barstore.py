"""行情 / 汇率的唯一入口：`data/standardized/<market>.parquet` 与 `daily_bar` 之间只走这里。

**模式语义与别的域略有不同**，理由是行情有两类用法：

| 用法 | 例子 | 需要什么 |
|---|---|---|
| 取一段历史做计算 | 回测基准、`sources.py` 的市值加权、模拟盘估值 | 按 `(市场, 日期区间, 股票集合)` 查 |
| 要一份完整文件喂外部工具 | `data.to_qlib` 生成 qlib 二进制 | 一个 parquet 路径 |

所以 `save_*` **始终**落一份 parquet（它是派生缓存，删了能从库重建），
但 `bars()/fx()` 在 `db` 模式下**只读库、不回落 parquet**——
否则页面与计算又会在无人察觉的情况下读到上一次的行情。

    files  parquet 是唯一真相；读文件
    dual   写：parquet + 库；读：库，库空回落 parquet
    db     写：parquet（派生）+ 库；读：只读库

`FACTOPIA_STORAGE_BARS` 可单独灰度；默认跟随全局 `FACTOPIA_STORAGE`。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from factopia import db
from factopia.repo import bars as repo_bars

log = logging.getLogger(__name__)

DOMAIN = "bars"
MARKETS = ("hk", "uk", "fr")

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
DATA = Path(os.environ.get("FACTOPIA_DATA_DIR", str(RUNTIME / "data")))
STANDARDIZED = DATA / "standardized"
RAW = DATA / "raw"

def _is_primary() -> bool:
    return db.is_primary(DOMAIN)


def parquet_path(market: str) -> Path:
    """派生 parquet 的位置（外部工具要文件路径时用；业务读请用 `bars()`）。"""
    return STANDARDIZED / f"{market}.parquet"


# ── 读 ──────────────────────────────────────────────────────────────────────────────

def has_bars(market: str) -> bool:
    if _is_primary():
        if repo_bars.has_bars(market):
            return True
        return db.mode(DOMAIN) == "dual" and parquet_path(market).exists()
    return parquet_path(market).exists()


def bars(market: str, columns: list[str] | None = None, start: str | None = None,
         end: str | None = None, symbols: list[str] | None = None) -> pd.DataFrame:
    """某市场的行情。`columns` 只裁剪返回列（与 parquet 时代的列名一致）。"""
    if _is_primary():
        got = repo_bars.bars(market, columns=columns, start=start, end=end, symbols=symbols)
        if not got.empty or db.mode(DOMAIN) == "db":
            return got
    p = parquet_path(market)
    if not p.exists():
        return pd.DataFrame(columns=columns)
    df = pd.read_parquet(p)
    if columns:
        df = df[[c for c in columns if c in df.columns]]
    if start:
        df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(start)]
    if end:
        df = df[pd.to_datetime(df["date"]) <= pd.Timestamp(end)]
    if symbols:
        df = df[df["symbol"].isin(symbols)]
    return df.reset_index(drop=True)


def fx(pairs: list[str] | None = None) -> pd.DataFrame:
    if _is_primary():
        got = repo_bars.fx(pairs)
        if not got.empty or db.mode(DOMAIN) == "db":
            return got
    p = RAW / "fx.parquet"
    if not p.exists():
        return pd.DataFrame(columns=["pair", "date", "close"])
    df = pd.read_parquet(p)
    if pairs:
        key = "pair" if "pair" in df.columns else "symbol"
        df = df[df[key].isin(pairs)]
    return df.reset_index(drop=True)


def span(market: str) -> dict | None:
    if _is_primary():
        got = repo_bars.span(market)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    p = parquet_path(market)
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["symbol", "date"])
    return {"first": str(pd.to_datetime(df["date"]).min().date()),
            "last": str(pd.to_datetime(df["date"]).max().date()),
            "sessions": int(pd.to_datetime(df["date"]).nunique()),
            "symbols": int(df["symbol"].nunique())}


# ── 写 ──────────────────────────────────────────────────────────────────────────────

def save_bars(market: str, df: pd.DataFrame) -> int:
    """落 parquet（派生缓存，始终写）+ 进库（dual/db）。返回行数。"""
    if df is None or df.empty:
        return 0
    STANDARDIZED.mkdir(parents=True, exist_ok=True)
    tmp = parquet_path(market).with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(parquet_path(market))
    if db.mode(DOMAIN) in ("dual", "db"):
        got = db.write_db(DOMAIN, lambda: repo_bars.save_bars(market, df), f"bars.save:{market}")
        return int(got or len(df))
    return len(df)


def save_fx(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    RAW.mkdir(parents=True, exist_ok=True)
    p = RAW / "fx.parquet"
    tmp = p.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(p)
    if db.mode(DOMAIN) in ("dual", "db"):
        got = db.write_db(DOMAIN, lambda: repo_bars.save_fx(df), "bars.save_fx")
        return int(got or len(df))
    return len(df)


def save_bars_parquet_only(market: str, df: pd.DataFrame) -> None:
    """只落 parquet（demo/seed 这类"造数据"脚本用，不让它们决定库里的行情）。"""
    STANDARDIZED.mkdir(parents=True, exist_ok=True)
    df.to_parquet(parquet_path(market), index=False)
