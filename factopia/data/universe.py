"""从维基百科抓取三个市场指数的当前成分股，生成 configs/universe/{hk,uk,fr}.csv。

POC 使用当前成分股（存在幸存者偏差，见 Spec 第 8 节）。生成的 CSV 是配置文件，可人工审阅和修改。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.universe
"""
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

OUT_DIR = Path("configs/universe")
HEADERS = {"User-Agent": "Mozilla/5.0 (factopia-poc; research use)"}

SOURCES = {
    "hk": {"url": "https://en.wikipedia.org/wiki/Hang_Seng_Index", "index": "^HSI"},
    "uk": {"url": "https://en.wikipedia.org/wiki/FTSE_100_Index", "index": "^FTSE"},
    "fr": {"url": "https://en.wikipedia.org/wiki/CAC_40", "index": "^FCHI"},
}


def _tables(url: str) -> list[pd.DataFrame]:
    html = requests.get(url, headers=HEADERS, timeout=30).text
    return pd.read_html(StringIO(html))


def _find(tables: list[pd.DataFrame], must_have: list[str]) -> pd.DataFrame:
    for t in tables:
        cols = [str(c).lower() for c in t.columns]
        if all(any(k in c for c in cols) for k in must_have):
            return t
    raise ValueError(f"未找到包含列 {must_have} 的表格")


def _col(t: pd.DataFrame, key: str) -> str:
    return next(c for c in t.columns if key in str(c).lower())


def hk() -> pd.DataFrame:
    t = _find(_tables(SOURCES["hk"]["url"]), ["ticker", "name"])
    codes = t[_col(t, "ticker")].astype(str).str.extract(r"(\d+)")[0]
    return pd.DataFrame({"symbol": codes.str.zfill(4) + ".HK", "name": t[_col(t, "name")]})


def uk() -> pd.DataFrame:
    t = _find(_tables(SOURCES["uk"]["url"]), ["ticker", "company"])
    tickers = t[_col(t, "ticker")].astype(str).str.strip().str.rstrip(".").str.replace(".", "-", regex=False)
    return pd.DataFrame({"symbol": tickers + ".L", "name": t[_col(t, "company")]})


def fr() -> pd.DataFrame:
    t = _find(_tables(SOURCES["fr"]["url"]), ["ticker", "company"])
    tickers = t[_col(t, "ticker")].astype(str).str.strip()
    symbols = tickers.where(tickers.str.contains(r"\."), tickers + ".PA")
    return pd.DataFrame({"symbol": symbols, "name": t[_col(t, "company")]})


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for market, fn in {"hk": hk, "uk": uk, "fr": fr}.items():
        df = fn().dropna().drop_duplicates("symbol").sort_values("symbol")
        df.to_csv(OUT_DIR / f"{market}.csv", index=False)
        print(f"{market}: {len(df)} 只  例如 {df['symbol'].head(5).tolist()}")


if __name__ == "__main__":
    main()
