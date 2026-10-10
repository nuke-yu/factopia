"""把原始行情整理成统一口径的标准化表（Spec DATA-03、05、06、07、09）。

- 币种：每个市场统一为本地币种（HK=HKD、UK=GBP、FR=EUR）。英股便士（GBp）÷100；
  报价为其他币种的股票（如 CPG.L、IHG.L 为 USD，MTLN.L 为 EUR）按当日汇率换算。
- 交易日：只保留交易所日历（XHKG / XLON / XPAR）中的交易日，统计每只股票缺失的交易日。
- 时间：保留交易所本地日期（业务日期），记录收盘时刻（UTC，正式发布）与实际可获取时间（收盘 + 日终数据延迟）。
- 质量检查：缺失值、零成交量、单日涨跌超过 50%、非正价格；超过阈值的股票标记为排除。
- 输出 /workspace/runtime/data/standardized/{market}.parquet 和 quality_report.json（含数据指纹）。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.standardize
"""
import hashlib
import json
import os
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

DATA_DIR = Path(os.environ.get("FACTOPIA_DATA_DIR", "/workspace/runtime/data"))
RAW_DIR = DATA_DIR / "raw"
OUT_DIR = DATA_DIR / "standardized"

MARKETS = {
    "hk": {"calendar": "XHKG", "currency": "HKD"},
    "uk": {"calendar": "XLON", "currency": "GBP"},
    "fr": {"calendar": "XPAR", "currency": "EUR"},
}

def _eod_delay() -> dict:
    import yaml

    p = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "configs")) / "refdata" / "markets.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {"markets": {}}
    return {m: int(cfg["markets"].get(m, {}).get("eod_delay_min", 60)) for m in MARKETS}


EOD_DELAY = _eod_delay()

# 质量阈值：缺失交易日占比、异常涨跌天数超过以下值的股票标记为排除
MAX_MISSING_RATIO = 0.05
MAX_JUMP_DAYS = 5
JUMP_THRESHOLD = 0.5

# 报价币种 → 本地币种 的换算：(汇率代码, 运算)。GBPUSD=X 表示 1 GBP = x USD
FX_RULES = {
    ("USD", "GBP"): ("GBPUSD=X", "div"),
    ("EUR", "GBP"): ("EURGBP=X", "mul"),
    ("GBP", "EUR"): ("EURGBP=X", "div"),
    ("USD", "EUR"): ("EURUSD=X", "div"),
    ("USD", "HKD"): ("HKD=X", "mul"),
}


def _fx_series(fx: pd.DataFrame, pair: str) -> pd.Series:
    s = fx[fx["symbol"] == pair].set_index("date")["close"].sort_index()
    return s[~s.index.duplicated()]


def _to_local_currency(prices: pd.DataFrame, currency: pd.DataFrame, local: str, fx: pd.DataFrame) -> pd.DataFrame:
    df = prices.merge(currency, on="symbol", how="left")
    df["quote_currency"] = df["quote_currency"].fillna(local)
    price_cols = ["open", "high", "low", "close"]

    pence = df["quote_currency"] == "GBp"
    df.loc[pence, price_cols] = df.loc[pence, price_cols] / 100.0

    for quote in sorted(set(df["quote_currency"]) - {local, "GBp"}):
        pair, op = FX_RULES[(quote, local)]
        rate = _fx_series(fx, pair)
        mask = df["quote_currency"] == quote
        r = rate.reindex(rate.index.union(df.loc[mask, "date"].unique())).ffill().reindex(df.loc[mask, "date"]).to_numpy()
        factor = (1.0 / r) if op == "div" else r
        df.loc[mask, price_cols] = df.loc[mask, price_cols].to_numpy() * factor[:, None]

    df["currency"] = local
    return df


def _standardize_market(market: str, cfg: dict, fx: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    raw = RAW_DIR / market
    prices = pd.read_parquet(raw / "prices.parquet")
    currency = pd.read_csv(raw / "currency.csv")
    prices["date"] = pd.to_datetime(prices["date"]).dt.tz_localize(None).dt.normalize()

    df = _to_local_currency(prices, currency, cfg["currency"], fx)

    cal = xcals.get_calendar(cfg["calendar"])
    sessions = cal.sessions_in_range(df["date"].min(), df["date"].max())
    off_calendar = int((~df["date"].isin(sessions)).sum())
    df = df[df["date"].isin(sessions)].copy()
    closes = cal.closes.reindex(sessions)
    df["ts_utc"] = df["date"].map(dict(zip(sessions, closes.dt.tz_convert("UTC"))))
    df["market"] = market.upper()
    # 实际可获取时间 = 收盘时刻 + 日终数据延迟（configs/refdata/markets.yaml，BR-18）
    df["available_at"] = df["ts_utc"] + pd.Timedelta(minutes=EOD_DELAY[market])

    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["ret"] = df.groupby("symbol")["close"].pct_change()

    per_symbol = {}
    flags = pd.Series("", index=df.index)
    for symbol, g in df.groupby("symbol"):
        expected = sessions[(sessions >= g["date"].min()) & (sessions <= g["date"].max())]
        missing = len(expected) - len(g)
        jumps = int((g["ret"].abs() > JUMP_THRESHOLD).sum())
        nonpositive = int((g[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
        zero_vol = int((g["volume"] == 0).sum())
        nan_rows = int(g[["open", "high", "low", "close"]].isna().any(axis=1).sum())
        excluded = (missing / max(len(expected), 1) > MAX_MISSING_RATIO) or jumps > MAX_JUMP_DAYS or nonpositive > 0
        per_symbol[symbol] = {
            "first": str(g["date"].min().date()), "last": str(g["date"].max().date()), "rows": len(g),
            "missing_sessions": missing, "jumps_over_50pct": jumps, "nonpositive_price_rows": nonpositive,
            "zero_volume_rows": zero_vol, "nan_rows": nan_rows, "excluded": bool(excluded),
        }
        if excluded:
            flags[g.index] = "excluded"
    df["dq_flag"] = flags

    out = df[["symbol", "market", "date", "ts_utc", "available_at", "open", "high", "low", "close", "volume",
              "currency", "quote_currency", "dq_flag"]]
    report = {
        "symbols": len(per_symbol),
        "excluded": sorted(s for s, v in per_symbol.items() if v["excluded"]),
        "rows": len(out),
        "rows_dropped_off_calendar": off_calendar,
        "sessions": [str(sessions.min().date()), str(sessions.max().date()), len(sessions)],
        "currency_converted": sorted(set(currency.loc[~currency["quote_currency"].isin([cfg["currency"]]), "quote_currency"].dropna())),
        "per_symbol": per_symbol,
    }
    return out, report


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fx = pd.read_parquet(RAW_DIR / "fx.parquet")
    fx["date"] = pd.to_datetime(fx["date"]).dt.tz_localize(None).dt.normalize()

    report = {"thresholds": {"max_missing_ratio": MAX_MISSING_RATIO, "max_jump_days": MAX_JUMP_DAYS,
                             "jump_threshold": JUMP_THRESHOLD}, "markets": {}}
    for market, cfg in MARKETS.items():
        df, rep = _standardize_market(market, cfg, fx)
        path = OUT_DIR / f"{market}.parquet"
        df.to_parquet(path, index=False)
        rep["sha256"] = _sha256(path)
        report["markets"][market] = rep
        print(f"{market}: {rep['symbols']} 只，{rep['rows']} 行，交易日 {rep['sessions'][2]} 天 "
              f"({rep['sessions'][0]}~{rep['sessions'][1]})，非交易日行剔除 {rep['rows_dropped_off_calendar']}，"
              f"排除 {len(rep['excluded'])} 只 {rep['excluded']}")

    (OUT_DIR / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"已写入 {OUT_DIR}")


if __name__ == "__main__":
    main()
