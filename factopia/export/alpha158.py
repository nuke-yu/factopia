"""导出 Alpha158 评估的界面数据（迁移性立方体、因子时间曲面）与三个市场的概况。

在 app 容器中运行，须先运行 factopia.factors.evaluate：
    docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.export.alpha158 [--horizon 20]

输出 /workspace/runtime/results/：
    alpha158/h<N>/cube.json     每个因子在三个时期、三个市场的 RankIC 与 t 值及迁移性分类
    alpha158/h<N>/surface.json  每个市场：因子 × 月份的月均 RankIC 与 6 个月滚动均值
    overview.json               三个市场的股票数、数据区间、币种、基准、汇率
"""
import argparse
import json
import os
from pathlib import Path

import pandas as pd

from factopia.factors.families import GROUP_ORDER, family
from factopia.factors.stats import MARKETS, T_THRESHOLD, classify

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RESULTS = RUNTIME / "results"

# 与 factopia.factors.evaluate.SPLITS 一致；train 期即 AI 可见的已有发现所用区间（MINE-11）
PERIODS = {
    "train": {"label": "2015–2021", "note": "AI 可见（测试期开始前）"},
    "valid": {"label": "2022–2023", "note": "与 RD-Agent 回测测试期重叠，不提供给 AI"},
    "test": {"label": "2024 至今", "note": "AI 从未见过的数据"},
}
MARKET_INFO = {
    "hk": {"name": "香港", "city": "Hong Kong", "index": "恒生指数", "currency": "HKD", "lat": 22.28, "lon": 114.16},
    "uk": {"name": "英国", "city": "London", "index": "富时 100", "currency": "GBP", "lat": 51.51, "lon": -0.13},
    "fr": {"name": "法国", "city": "Paris", "index": "CAC 40", "currency": "EUR", "lat": 48.86, "lon": 2.35},
}
FX_TO_HKD = {"uk": "GBPHKD=X", "fr": "EURHKD=X"}  # 1 单位本币兑港元（组合基础币种，第 8 章 #3）


def _num(x: float, nd: int = 4) -> float | None:
    return None if pd.isna(x) else round(float(x), nd)


def _sorted_factors(names) -> list[str]:
    return sorted(names, key=lambda n: (GROUP_ORDER.index(family(n)[0]) if family(n)[0] in GROUP_ORDER
                                        else len(GROUP_ORDER), n))


def export_cube(folder: Path) -> dict:
    s = pd.read_csv(folder / "summary.csv")
    factors = _sorted_factors(s["factor"].unique())
    out = []
    for name in factors:
        f = s[s["factor"] == name]
        periods = {}
        for p in PERIODS:
            g = f[f["split"] == p].set_index("market")
            row = pd.Series({f"t_{m}": g.loc[m, "rank_ic_t"] for m in MARKETS})
            periods[p] = {
                "t": {m: _num(g.loc[m, "rank_ic_t"], 3) for m in MARKETS},
                "ric": {m: _num(g.loc[m, "rank_ic_mean"]) for m in MARKETS},
                "label": classify(row),
            }
        grp, _, zh = family(name)
        out.append({"name": name, "group": grp, "desc": zh, "periods": periods})
    return {"threshold": T_THRESHOLD, "periods": PERIODS, "groups": GROUP_ORDER, "factors": out}


def export_surface(folder: Path) -> dict:
    result = {}
    for m in MARKETS:
        ric = pd.read_parquet(folder / f"ic_daily_{m}.parquet")
        ric.index = pd.to_datetime(ric.index)
        monthly = ric.resample("ME").mean().dropna(how="all")
        rolling = monthly.rolling(6, min_periods=3).mean()
        cols = _sorted_factors(monthly.columns)
        result[m] = {
            "months": [d.strftime("%Y-%m") for d in monthly.index],
            "factors": cols,
            "monthly": [[_num(v) for v in monthly[c]] for c in cols],
            "rolling6": [[_num(v) for v in rolling[c]] for c in cols],
        }
    return {"test_boundary": "2022-01", "markets": result}


def export_overview() -> dict:
    quality = json.loads((RUNTIME / "data/standardized/quality_report.json").read_text())
    fx = pd.read_parquet(RUNTIME / "data/raw/fx.parquet")
    markets = {}
    for m, info in MARKET_INFO.items():
        q = quality["markets"][m]
        first, last, sessions = q["sessions"]
        entry = {**info, "stocks": q["symbols"], "excluded": q["excluded"], "first": first, "last": last,
                 "sessions": sessions, "mining_cutoff": "2023-12-31"}
        if m in FX_TO_HKD:
            entry["fx_to_hkd"] = _num(fx[fx["symbol"] == FX_TO_HKD[m]].sort_values("date").iloc[-1]["close"])
        markets[m] = entry
    return {"markets": markets, "fx_date": str(fx["date"].max())[:10]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=20)
    horizon = parser.parse_args().horizon
    folder = RESULTS / "alpha158" / f"h{horizon}"
    (folder / "cube.json").write_text(json.dumps({"horizon": horizon, **export_cube(folder)}, ensure_ascii=False))
    (folder / "surface.json").write_text(json.dumps({"horizon": horizon, **export_surface(folder)}, ensure_ascii=False))
    (RESULTS / "overview.json").write_text(json.dumps(export_overview(), ensure_ascii=False, indent=2))
    print(f"已导出 {folder}/cube.json、surface.json 与 {RESULTS}/overview.json")


if __name__ == "__main__":
    main()
