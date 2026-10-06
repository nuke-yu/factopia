"""验证第 1 天：三个市场的数据源和交易日历能否正常获取。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python spikes/01_data_sources.py
"""
import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

STOCKS = {"HK": "0700.HK", "UK": "HSBA.L", "FR": "MC.PA"}
FX = ["GBPHKD=X", "EURHKD=X"]
INDICES = {"HK": "^HSI", "UK": "^FTSE", "FR": "^FCHI"}
CALENDARS = {"HK": "XHKG", "UK": "XLON", "FR": "XPAR"}


def check_prices() -> None:
    print("== 行情（2015-01-01 起，日线，已复权）")
    for market, symbol in {**STOCKS, **{f"{m}-指数": s for m, s in INDICES.items()}}.items():
        df = yf.download(symbol, start="2015-01-01", auto_adjust=True, progress=False, multi_level_index=False)
        currency = yf.Ticker(symbol).fast_info.get("currency")
        print(
            f"{market:8} {symbol:9} 行数={len(df):5}  {df.index.min().date()} ~ {df.index.max().date()}  "
            f"币种={currency}  最新收盘={df['Close'].iloc[-1]:.2f}  缺失值={int(df.isna().sum().sum())}"
        )


def check_fx() -> None:
    print("\n== 汇率")
    for symbol in FX:
        df = yf.download(symbol, start="2015-01-01", progress=False, multi_level_index=False)
        print(f"{symbol:9} 行数={len(df):5}  {df.index.min().date()} ~ {df.index.max().date()}  最新={df['Close'].iloc[-1]:.4f}")


def check_calendars() -> None:
    print("\n== 交易日历（2025 年）")
    sessions = {}
    for market, code in CALENDARS.items():
        cal = xcals.get_calendar(code)
        s = cal.sessions_in_range("2025-01-01", "2025-12-31")
        sessions[market] = set(s)
        print(f"{market} {code}: {len(s)} 个交易日，时区 {cal.tz}")
    common = set.intersection(*sessions.values())
    union = set.union(*sessions.values())
    print(f"三个市场都开市: {len(common)} 天；至少一个市场开市: {len(union)} 天")
    for market, s in sessions.items():
        only_closed_here = sorted(d.date() for d in (union - s))
        print(f"{market} 休市而其他市场开市的日子 {len(only_closed_here)} 天，例如: {only_closed_here[:5]}")


if __name__ == "__main__":
    print(f"pandas {pd.__version__}, yfinance {yf.__version__}, exchange_calendars {xcals.__version__}\n")
    check_prices()
    check_fx()
    check_calendars()
