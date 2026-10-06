"""下载三个市场（香港 / 英国 / 巴黎）的日线行情、基准指数和汇率，存入命名卷 /workspace/runtime/data/raw/。

对应 Spec DATA-02（行情）、DATA-03（币种核对）、DATA-04（汇率）。原始数据只做落盘，标准化在下一步进行。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.download
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

START = "2015-01-01"
UNIVERSE_DIR = Path("configs/universe")
RAW_DIR = Path(os.environ.get("FACTOPIA_DATA_DIR", "/workspace/runtime/data")) / "raw"

MARKETS = {
    "hk": {"index": "^HSI", "currency": "HKD"},
    "uk": {"index": "^FTSE", "currency": "GBP"},
    "fr": {"index": "^FCHI", "currency": "EUR"},
}
FX_PAIRS = ["GBPHKD=X", "EURHKD=X", "EURGBP=X", "GBPUSD=X", "EURUSD=X", "HKD=X"]  # HKD=X 即 USDHKD


def _download(symbols: list[str]) -> pd.DataFrame:
    """批量下载，返回长表：date, symbol, open, high, low, close, volume。"""
    wide = yf.download(symbols, start=START, auto_adjust=True, progress=False, group_by="ticker", threads=True)
    frames = []
    for s in symbols:
        if s not in wide.columns.get_level_values(0):
            continue
        df = wide[s].dropna(how="all")
        if df.empty:
            continue
        df = df.rename(columns=str.lower).reset_index().rename(columns={"Date": "date"})
        df["symbol"] = s
        frames.append(df[["date", "symbol", "open", "high", "low", "close", "volume"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _quote_currency(symbols: list[str]) -> dict[str, str | None]:
    out = {}
    for s in symbols:
        try:
            out[s] = yf.Ticker(s).fast_info.get("currency")
        except Exception:
            out[s] = None
    return out


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"downloaded_at": datetime.now(timezone.utc).isoformat(), "start": START, "markets": {}}

    for market, cfg in MARKETS.items():
        universe = pd.read_csv(UNIVERSE_DIR / f"{market}.csv")
        symbols = universe["symbol"].tolist()
        out = RAW_DIR / market
        out.mkdir(parents=True, exist_ok=True)

        prices = _download(symbols)
        prices.to_parquet(out / "prices.parquet", index=False)
        got = set(prices["symbol"]) if not prices.empty else set()
        missing = sorted(set(symbols) - got)

        currencies = _quote_currency(sorted(got))
        pd.DataFrame(currencies.items(), columns=["symbol", "quote_currency"]).to_csv(out / "currency.csv", index=False)
        odd = {s: c for s, c in currencies.items() if c not in (cfg["currency"], "GBp")}

        index = _download([cfg["index"]])
        index.to_parquet(out / "index.parquet", index=False)

        manifest["markets"][market] = {
            "universe": len(symbols), "downloaded": len(got), "missing": missing,
            "rows": len(prices), "non_standard_currency": odd,
            "date_range": [str(prices["date"].min().date()), str(prices["date"].max().date())] if len(prices) else None,
        }
        print(f"{market}: 成分股 {len(symbols)}，成功 {len(got)}，失败 {len(missing)} {missing[:10]}，"
              f"{len(prices)} 行，币种例外 {odd}")

    fx = _download(FX_PAIRS)
    fx.to_parquet(RAW_DIR / "fx.parquet", index=False)
    manifest["fx"] = sorted(fx["symbol"].unique().tolist())
    print(f"fx: {manifest['fx']}，{len(fx)} 行")

    (RAW_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"已写入 {RAW_DIR}")


if __name__ == "__main__":
    main()
