"""股票池成分股的起止日期（BR-18：时间点一致的股票池）。

configs/universe/membership/<市场>.csv：symbol, start, end, source, note
- 同一股票可有多行（多次进出指数）；end 为空表示仍在指数中
- source=current_list：由当前成分股生成（start = 有行情的第一天），不是历史名单，存在幸存者偏差；
  取得数据商的历史成分股后按相同格式替换，source 写数据商名称
Qlib 股票池文件（instruments/<pool>.txt）按此生成（factopia.data.to_qlib），Qlib 原生支持同一股票多个区间，
回测在每个交易日只使用当日属于指数的股票。
"""
import os
from pathlib import Path

import pandas as pd

from factopia import db

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
MDIR = CONFIGS / "universe" / "membership"
COLUMNS = ["symbol", "start", "end", "source", "note"]
DOMAIN = "refdata"


def path(market: str) -> Path:
    return MDIR / f"{market}.csv"


def load(market: str) -> pd.DataFrame | None:
    """成分股名单。db 模式读 `index_membership`，**不回落 CSV**（回落就等于读到旧的）。"""
    if db.is_primary(DOMAIN):
        from factopia.repo import refdata as repo_refdata
        got = repo_refdata.load_membership(market)
        if got is not None:
            return got.fillna("")
        if db.mode(DOMAIN) == "db":
            return None
    p = path(market)
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype=str).fillna("")
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{p} 缺少列：{missing}")
    return df


def init(market: str, force: bool = False) -> pd.DataFrame:
    """由当前成分股生成（只在文件不存在或 force 时写入，避免覆盖数据商的历史名单）。"""
    exists = bool(db.query_one("SELECT 1 FROM factopia.index_membership WHERE index_code = %s",
                               (market,))) if db.is_primary(DOMAIN) else path(market).exists()
    if exists and not force:
        return load(market)
    uni = pd.read_csv(CONFIGS / "universe" / f"{market}.csv")
    from factopia import barstore
    first = (barstore.bars(market, columns=["symbol", "date"]).groupby("symbol")["date"].min()
             if barstore.has_bars(market) else {})
    rows = [{"symbol": s, "start": pd.Timestamp(first[s]).strftime("%Y-%m-%d") if s in first else "",
             "end": "", "source": "current_list", "note": "当前成分股，非历史名单"} for s in uni["symbol"]]
    df = pd.DataFrame(rows, columns=COLUMNS)
    mode = db.mode(DOMAIN)
    if mode in ("files", "dual"):
        MDIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(path(market), index=False)
    if mode in ("dual", "db"):
        from factopia.repo import refdata as repo_refdata
        repo_refdata.save_membership(market, df)
    return df


def is_point_in_time(market: str) -> bool:
    df = load(market)
    return df is not None and len(df) > 0 and not (df["source"] == "current_list").any()


def pool_ranges(market: str, spans: pd.DataFrame, code) -> pd.DataFrame:
    """成分股区间 ∩ 有行情的区间 → Qlib 股票池行（code, start, end）。spans 列：symbol, start, end。"""
    mem = load(market)
    if mem is None:
        return pd.DataFrame({"code": spans["symbol"].map(code), "start": spans["start"], "end": spans["end"]})
    span = spans.set_index("symbol")
    rows = []
    for r in mem.to_dict("records"):
        if r["symbol"] not in span.index:
            continue
        lo = max(r["start"] or span.at[r["symbol"], "start"], span.at[r["symbol"], "start"])
        hi = min(r["end"] or span.at[r["symbol"], "end"], span.at[r["symbol"], "end"])
        if lo <= hi:
            rows.append({"code": code(r["symbol"]), "start": lo, "end": hi})
    return pd.DataFrame(rows, columns=["code", "start", "end"])
