"""按时间段计算回测与打分指标（只依赖 pandas，可在 Qlib 镜像与后端中使用）。

与 Qlib risk_analysis（mode="sum"，freq="day"）口径一致，RD-Agent 显示的数值即按此计算：
年化 = 日均 × 238，信息比率 = 日均 / 日标准差 × √238，最大回撤 = 累计超额（逐日相加）离此前最高点的最大距离。
"""
import numpy as np
import pandas as pd

from factopia.factors.stats import newey_west_t

N = 238   # Qlib 按 A 股每年 238 个交易日年化（qlib.contrib.evaluate.risk_analysis）
T_SIGNIFICANT = 2.0


def _r(x, nd: int = 6):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def risk(excess: pd.Series) -> dict:
    excess = excess.dropna()
    if len(excess) < 2:
        return {"annual_return": None, "information_ratio": None, "max_drawdown": None}
    cum = excess.cumsum()
    std = excess.std()
    return {"annual_return": _r(excess.mean() * N),
            "information_ratio": _r(excess.mean() / std * np.sqrt(N)) if std > 0 else None,
            "max_drawdown": _r((cum - cum.cummax()).min())}


def significance(daily_ic: pd.Series, horizon: int) -> dict:
    """逐日 rank IC 的均值、Newey-West t 值（滞后 = 预测周期）与 95% 区间。"""
    x = daily_ic.dropna()
    if len(x) <= horizon + 1:
        return {"mean": None, "t": None, "n": int(len(x)), "low": None, "high": None}
    t = newey_west_t(x, horizon)
    se = abs(x.mean() / t) if t and not pd.isna(t) and t != 0 else None
    return {"mean": _r(x.mean()), "t": _r(t, 3), "n": int(len(x)),
            "low": _r(x.mean() - 1.96 * se) if se else None, "high": _r(x.mean() + 1.96 * se) if se else None}


def period(report: pd.DataFrame, ric: pd.Series, start, end, horizon: int, quantiles: dict | None = None) -> dict:
    """一个时间段内：扣成本 / 扣成本前年化超额、信息比率、最大回撤，rank IC 及显著性，分组收益摘要。"""
    rep = report.loc[start:end]
    with_cost = risk(rep["return"] - rep["bench"] - rep["cost"])
    without = risk(rep["return"] - rep["bench"])
    sig = significance(ric.loc[start:end], horizon)
    out = {"start": str(pd.Timestamp(rep.index.min()).date()) if len(rep) else None,
           "end": str(pd.Timestamp(rep.index.max()).date()) if len(rep) else None, "days": int(len(rep)),
           "annual_return_with_cost": with_cost["annual_return"], "information_ratio_with_cost": with_cost["information_ratio"],
           "max_drawdown_with_cost": with_cost["max_drawdown"], "annual_return_without_cost": without["annual_return"],
           "rank_ic": sig["mean"], "rank_ic_t": sig["t"], "rank_ic_low": sig["low"], "rank_ic_high": sig["high"], "rank_ic_days": sig["n"]}
    if quantiles:
        out |= {k: quantiles.get(k) for k in ("spread_mean", "spread_positive_share", "tie_share")}
    return out
