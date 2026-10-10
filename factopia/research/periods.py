"""交易日历与时间段划分（Spec MINE-09、第 3.6 节）。

时间段按该市场交易日历计算，段与段之间间隔 N 个交易日（N 为预测周期），避免标签跨段重叠。
RD-Agent 使用的数据截至 2023-12-31，2024 年以后对 AI 不可见（MINE-03）。
"""
import os
from pathlib import Path

import pandas as pd

HORIZONS = (1, 5, 20, 60)
TRAIN_START, TRAIN_END, VALID_END = "2015-01-01", "2020-12-31", "2021-12-31"
RDAGENT_HOME = Path(os.environ.get("FACTOPIA_RDAGENT_HOME", "/workspace/runtime/rdagent_markets"))


def check_horizon(value: object) -> int:
    n = int(value)
    if n not in HORIZONS:
        raise ValueError(f"预测周期必须是 {HORIZONS} 之一，收到 {value}")
    return n


def calendar(market: str) -> list[str]:
    path = RDAGENT_HOME / market / "qlib_data/cn_data/calendars/day.txt"
    return [d.strip() for d in path.read_text().splitlines() if d.strip()]


def split_dates(market: str, n: int) -> dict[str, str]:
    """训练 / 验证 / 测试的起止日期（键名与 RD-Agent 的环境变量后缀一致）。"""
    cal = calendar(market)

    def last_on_or_before(day: str) -> int:
        return max(i for i, d in enumerate(cal) if d <= day)

    i_train_end = last_on_or_before(TRAIN_END)
    i_valid_start = i_train_end + n + 1          # 训练集最后一个标签用到的价格之后
    i_valid_end = last_on_or_before(VALID_END)
    i_test_start = i_valid_end + n + 1
    i_test_end = len(cal) - 2                      # 回测最后一步需要读取下一个交易日
    if not (i_valid_start < i_valid_end < i_test_start < i_test_end):
        raise ValueError(f"预测周期 {n} 过长，无法在现有数据上划分出有效的验证期和测试期")
    return {
        "TRAIN_START": TRAIN_START, "TRAIN_END": cal[i_train_end],
        "VALID_START": cal[i_valid_start], "VALID_END": cal[i_valid_end],
        "TEST_START": cal[i_test_start], "TEST_END": cal[i_test_end],
    }


def findings_end(market: str, horizon: int, test_start: str) -> pd.Timestamp:
    """已有发现可使用的最后一个日期：该日标签用到的价格（t+N+1）必须早于回测测试期开始日（MINE-11）。"""
    cal = pd.DatetimeIndex(calendar(market))
    i_test = cal.searchsorted(pd.Timestamp(test_start))
    return cal[i_test - horizon - 2]
