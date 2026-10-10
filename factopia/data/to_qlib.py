"""把标准化表转换为 Qlib 数据集，每个市场一套：/workspace/runtime/qlib_data/{hk,uk,fr}/（Spec DATA-08）。

Qlib 日线数据格式：
- calendars/day.txt：交易日列表（使用交易所日历，与标准化表一致）
- instruments/all.txt：股票代码、首个交易日、最后交易日（制表符分隔）；另生成按指数命名的股票池文件，
  基准指数单独列在 benchmark.txt（不放入 all，避免参与因子横截面排序）
- features/<代码小写>/<字段>.day.bin：little-endian float32，第一个数是起始日在日历中的下标，其后为逐日数值

代码命名：把 Yahoo 代码中的 "." 换成 "_"（如 0700.HK → 0700_HK），基准指数加 "IDX_" 前缀（如 IDX_HSI）。
价格已在下载时复权，因此复权因子 factor 恒为 1。被质量检查排除的股票不写入。
股票池文件按成分股起止日期生成（configs/universe/membership；目前为当前成分股，非时间点一致，Spec 第 14.10 节）。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.to_qlib
RD-Agent 用数据集（截止 2023-12-31，2024 年以后的数据对 AI 完全不可见，Spec MINE-03）：
     ... python -m factopia.data.to_qlib --rdagent
"""
import argparse
import json
import os
import shutil
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from factopia.refdata import membership

DATA_DIR = Path(os.environ.get("FACTOPIA_DATA_DIR", "/workspace/runtime/data"))
QLIB_DIR = Path(os.environ.get("FACTOPIA_QLIB_DIR", "/workspace/runtime/qlib_data"))
FIELDS = ["open", "high", "low", "close", "volume", "factor"]

MARKETS = {
    "hk": {"calendar": "XHKG", "pool": "hsi", "index": "^HSI"},
    "uk": {"calendar": "XLON", "pool": "ftse100", "index": "^FTSE"},
    "fr": {"calendar": "XPAR", "pool": "cac40", "index": "^FCHI"},
}


def qlib_code(symbol: str) -> str:
    return "IDX_" + symbol.lstrip("^") if symbol.startswith("^") else symbol.replace(".", "_")


def _write_instrument(features_dir: Path, code: str, df: pd.DataFrame, calendar: pd.DatetimeIndex) -> tuple[str, str]:
    df = df.set_index("date").sort_index()
    span = calendar[(calendar >= df.index.min()) & (calendar <= df.index.max())]
    df = df.reindex(span)  # 停牌等缺失日填 NaN，保持与日历逐日对齐
    start_index = int(calendar.get_loc(span[0]))
    out = features_dir / code.lower()
    out.mkdir(parents=True, exist_ok=True)
    for field in FIELDS:
        values = df[field].astype("float32").to_numpy()
        np.hstack([np.float32(start_index), values]).astype("<f4").tofile(out / f"{field}.day.bin")
    return str(span[0].date()), str(span[-1].date())


def convert_market(market: str, cfg: dict, root: Path, end: str | None = None) -> dict:
    std = pd.read_parquet(DATA_DIR / "standardized" / f"{market}.parquet")
    std = std[std["dq_flag"] != "excluded"].copy()
    std["date"] = pd.to_datetime(std["date"])
    if end:
        std = std[std["date"] <= end]
    std["factor"] = 1.0

    index = pd.read_parquet(DATA_DIR / "raw" / market / "index.parquet")
    index["date"] = pd.to_datetime(index["date"]).dt.tz_localize(None).dt.normalize()
    index["factor"] = 1.0

    cal = xcals.get_calendar(cfg["calendar"])
    calendar = cal.sessions_in_range(std["date"].min(), std["date"].max())
    index = index[index["date"].isin(calendar)]

    if root.exists():
        shutil.rmtree(root)
    (root / "calendars").mkdir(parents=True)
    (root / "instruments").mkdir()
    features = root / "features"

    pd.Series(calendar.strftime("%Y-%m-%d")).to_csv(root / "calendars" / "day.txt", index=False, header=False)

    rows = []
    for symbol, g in std.groupby("symbol"):
        rows.append((symbol, qlib_code(symbol), *_write_instrument(features, qlib_code(symbol), g, calendar)))
    spans = pd.DataFrame(rows, columns=["symbol", "code", "start", "end"])
    stocks = spans[["code", "start", "end"]]
    # 股票池按成分股起止日期生成（configs/universe/membership，BR-18）；all.txt 仍为全部有行情的区间
    pool = membership.pool_ranges(market, spans[["symbol", "start", "end"]], qlib_code)
    idx_code = qlib_code(cfg["index"])
    idx_start, idx_end = _write_instrument(features, idx_code, index, calendar)

    # 股票池与 all 只包含股票；基准指数单独列在 benchmark.txt。
    # RD-Agent 用 D.instruments()（即 all）生成因子数据，指数若在 all 中会参与因子的横截面排序。
    # 回测读取基准时直接按代码取数，不依赖股票池文件。
    pool.to_csv(root / "instruments" / f"{cfg['pool']}.txt", sep="\t", index=False, header=False)
    stocks.to_csv(root / "instruments" / "all.txt", sep="\t", index=False, header=False)
    pd.DataFrame([(idx_code, idx_start, idx_end)]).to_csv(
        root / "instruments" / "benchmark.txt", sep="\t", index=False, header=False)

    return {"provider_uri": str(root), "calendar_days": len(calendar), "stocks": len(stocks),
            "point_in_time_pool": membership.is_point_in_time(market),
            "pool": cfg["pool"], "benchmark": idx_code,
            "range": [str(calendar.min().date()), str(calendar.max().date())]}


# RD-Agent 写死从 ~/.qlib/qlib_data/cn_data 读取数据（回测容器内为 /root/.qlib），
# 因此每个市场准备一个"qlib home"，把该市场数据放在 qlib_data/cn_data 位置
RDAGENT_HOME = Path(os.environ.get("FACTOPIA_RDAGENT_HOME", "/workspace/runtime/rdagent_markets"))
RDAGENT_END = "2023-12-31"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rdagent", action="store_true", help="生成 RD-Agent 用数据集（截止 2023-12-31）")
    args = parser.parse_args()

    summary = {}
    for market, cfg in MARKETS.items():
        if args.rdagent:
            root = RDAGENT_HOME / market / "qlib_data" / "cn_data"
            summary[market] = convert_market(market, cfg, root, end=RDAGENT_END)
        else:
            summary[market] = convert_market(market, cfg, QLIB_DIR / market)
        s = summary[market]
        print(f"{market}: {s['stocks']} 只股票 + 基准 {s['benchmark']}，股票池 {s['pool']}，"
              f"交易日 {s['calendar_days']} 天 ({s['range'][0]}~{s['range'][1]}) → {s['provider_uri']}")
    manifest_dir = RDAGENT_HOME if args.rdagent else QLIB_DIR
    (manifest_dir / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
