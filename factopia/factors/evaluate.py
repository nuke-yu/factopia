"""Alpha158 分市场评估与迁移性分类（Spec FAC-01 至 04、第 3.4 节）。

- 预测目标：未来 N 个交易日收益（N 为预测周期，默认 20，Spec 第 3.6 节），
  按 Qlib 惯例为 Ref($close, -(N+1)) / Ref($close, -1) - 1（次日起持有 N 日）
- 每个交易日在横截面上计算 IC（Pearson）和 RankIC（Spearman）
- 数据切分：训练 2015–2021、验证 2022–2023、测试 2024 至今；迁移性分类只使用验证集
  （验证期与 RD-Agent 回测测试期重叠，因此 transfer.csv 不提供给 AI；AI 看到的已有发现由
  rdagent_ext/scenario.py 用测试期开始之前的逐日 IC 重新计算，Spec MINE-11）
- 显著性：相邻标签有 N-1 天重叠，逐日 IC 高度自相关，直接用逐日样本会高估 t 值；
  因此 t 值使用 Newey-West 修正（滞后 N 期），既使用全部逐日数据，又考虑重叠带来的自相关
- 迁移性分类（门槛 T=3）：三市场通用 / 地区特定 / 需调参 / 无效（"扣成本后无效"需组合回测，后续补充）

输出 /workspace/runtime/results/alpha158/h<N>/：ic_daily_{market}.parquet、summary.csv、transfer.csv
运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.factors.evaluate [--horizon 20]
"""
import argparse
import os
from pathlib import Path

import pandas as pd
import qlib
from qlib.contrib.data.loader import Alpha158DL
from qlib.data import D

from factopia.factors.stats import classify, newey_west_t

QLIB_DIR = Path(os.environ.get("FACTOPIA_QLIB_DIR", "/workspace/runtime/qlib_data"))
RESULTS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "alpha158"

MARKETS = {"hk": "hsi", "uk": "ftse100", "fr": "cac40"}
SPLITS = {"train": ("2015-01-01", "2021-12-31"), "valid": ("2022-01-01", "2023-12-31"), "test": ("2024-01-01", None)}
HORIZONS = (1, 5, 20, 60)
MIN_STOCKS = 10  # 当日有效股票少于此数时不计算 IC


def _daily_ic(features: pd.DataFrame, label: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = features.join(label.rename("label"))
    ic, ric = {}, {}
    for date, g in df.groupby(level="datetime"):
        g = g.dropna(subset=["label"])
        if len(g) < MIN_STOCKS:
            continue
        x = g.drop(columns="label")
        ic[date] = x.corrwith(g["label"])
        ric[date] = x.rank().corrwith(g["label"].rank())
    return pd.DataFrame(ic).T, pd.DataFrame(ric).T


def _summarize(ic: pd.DataFrame, ric: pd.DataFrame, start: str, end: str | None, horizon: int) -> pd.DataFrame:
    sl = slice(start, end)
    ic, ric = ic.loc[sl], ric.loc[sl]
    return pd.DataFrame({
        "ic_mean": ic.mean(), "rank_ic_mean": ric.mean(),
        "icir": ic.mean() / ic.std(), "rank_icir": ric.mean() / ric.std(),
        "rank_ic_t": ric.apply(newey_west_t, lags=horizon), "n_days": ric.notna().sum(),
        "positive_ratio": (ric > 0).mean(),
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=20, choices=HORIZONS, help="预测周期（交易日）")
    horizon = parser.parse_args().horizon
    label_expr = f"Ref($close, -{horizon + 1}) / Ref($close, -1) - 1"
    OUT_DIR = RESULTS_DIR / f"h{horizon}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exprs, names = Alpha158DL.get_feature_config()
    summaries = []
    for market, pool in MARKETS.items():
        qlib.init(provider_uri=str(QLIB_DIR / market), region="us", kernels=1)
        inst = D.instruments(pool)
        feats = D.features(inst, exprs, start_time="2015-01-01")
        feats.columns = names
        feats = feats.swaplevel().sort_index()
        label = D.features(inst, [label_expr], start_time="2015-01-01").iloc[:, 0].swaplevel().sort_index()

        ic, ric = _daily_ic(feats, label)
        ric.to_parquet(OUT_DIR / f"ic_daily_{market}.parquet")
        for split, (start, end) in SPLITS.items():
            s = _summarize(ic, ric, start, end, horizon)
            s["market"], s["split"], s["horizon"] = market, split, horizon
            summaries.append(s.rename_axis("factor").reset_index())
        print(f"{market}: {feats.shape[1]} 个因子，{ric.shape[0]} 个交易日的 IC")

    summary = pd.concat(summaries, ignore_index=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False)

    valid = summary[summary["split"] == "valid"].pivot(index="factor", columns="market", values=["rank_ic_mean", "rank_ic_t"])
    transfer = pd.DataFrame(index=valid.index)
    for m in MARKETS:
        transfer[f"ric_{m}"] = valid[("rank_ic_mean", m)]
        transfer[f"t_{m}"] = valid[("rank_ic_t", m)]
    transfer["label"] = transfer.apply(classify, axis=1)
    transfer.sort_values("label").to_csv(OUT_DIR / "transfer.csv")

    print(f"\n迁移性分类（预测周期 {horizon} 个交易日，验证集 2022–2023，门槛 |t|>3）：")
    print(transfer["label"].value_counts().to_string())
    top = transfer[transfer["label"] != "无效"].copy()
    top["min_abs_t"] = top[[f"t_{m}" for m in MARKETS]].abs().min(axis=1)
    print("\n有效因子示例：")
    print(top.sort_values("min_abs_t", ascending=False).head(12).round(3).to_string())


if __name__ == "__main__":
    main()
