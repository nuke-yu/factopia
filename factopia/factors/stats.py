"""因子评估的统计函数与迁移性分类（Spec 第 3.4 节）。

不依赖 Qlib，供 evaluate.py（app 容器）与 RD-Agent 场景子类（rdagent 容器）共用。
"""
import numpy as np
import pandas as pd

MARKETS = ("hk", "uk", "fr")
T_THRESHOLD = 3.0


def newey_west_t(x: pd.Series, lags: int) -> float:
    """均值的 Newey-West t 值：相邻标签有重叠时，逐日 IC 自相关，普通 t 值会被高估。"""
    x = x.dropna().to_numpy()
    n = len(x)
    if n <= lags + 1:
        return np.nan
    d = x - x.mean()
    lrv = d @ d / n
    for k in range(1, lags + 1):
        lrv += 2 * (1 - k / (lags + 1)) * (d[k:] @ d[:-k]) / n
    return x.mean() / np.sqrt(lrv / n) if lrv > 0 else np.nan


def classify(row: pd.Series) -> str:
    """按三个市场的 t 值（列 t_hk / t_uk / t_fr）分类：三市场通用 / 需调参 / 地区特定 / 无效。"""
    t = row[[f"t_{m}" for m in MARKETS]]
    passed = [m for m in MARKETS if abs(row[f"t_{m}"]) > T_THRESHOLD]
    same_sign = len(set(np.sign(t.dropna()))) == 1
    if len(passed) == len(MARKETS) and same_sign:
        return "三市场通用"
    if same_sign and passed and all(abs(row[f"t_{m}"]) > T_THRESHOLD / 2 for m in MARKETS):
        return "需调参"
    if passed:
        return "地区特定(" + "/".join(m.upper() for m in passed) + ")"
    return "无效"
