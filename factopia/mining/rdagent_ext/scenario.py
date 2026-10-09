"""RD-Agent 场景子类：向 AI 提供场景信息（Spec MINE-07、第 2.3.1 节）。

在 RD-Agent 原有的通用背景之后追加：
- 市场介绍：configs/rdagent/context/<市场>.en.md（团队编写、人工审核）；
- 研究方向、投资假设与约束：研究场景文件（环境变量 FACTOPIA_RESEARCH，可选）；
- 预测周期说明；
- 已有发现：用回测测试期开始之前的数据，从 Alpha158 逐日 IC 重新计算的摘要（MINE-11，防止数据泄漏）。
并把"实验设置"中写死的 CSI300 改为实际市场、时间段与预测周期。

通过环境变量 QLIB_FACTOR_SCEN 启用（由 prepare_run.py 设置），不修改 RD-Agent 源码。
"""
import os
import shutil
from pathlib import Path

import pandas as pd
import yaml
from rdagent.app.qlib_rd_loop.conf import FACTOR_PROP_SETTING
from rdagent.components.coder.factor_coder.config import FACTOR_COSTEER_SETTINGS
from rdagent.scenarios.qlib.experiment import utils as qlib_exp_utils
from rdagent.scenarios.qlib.experiment.factor_experiment import QlibFactorScenario

from factopia.factors.families import family
from factopia.factors.stats import classify, newey_west_t

CONTEXT_DIR = Path(os.environ.get("FACTOPIA_CONTEXT_DIR", "/workspace/configs/rdagent/context"))
RESULTS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "alpha158"
RDAGENT_HOME = Path(os.environ.get("FACTOPIA_RDAGENT_HOME", "/workspace/runtime/rdagent_markets"))

MARKETS = {
    "hk": {"name": "Hong Kong (Hang Seng Index constituents)", "currency": "HKD", "example": "0700_HK", "index": "Hang Seng Index"},
    "uk": {"name": "UK (FTSE 100 constituents)", "currency": "GBP", "example": "HSBA_L", "index": "FTSE 100 Index"},
    "fr": {"name": "France (CAC 40 constituents)", "currency": "EUR", "example": "MC_PA", "index": "CAC 40 Index"},
}
BENCHMARK_FILE = "daily_benchmark.h5"


def _family(name: str) -> str:
    return family(name)[1]


def _label_en(label: str) -> str:
    """把迁移性分类标签（中文，Spec 第 3.4 节）译为英文。"""
    if label.startswith("地区特定"):
        return "market-specific " + label[len("地区特定"):]
    return {"三市场通用": "universal across all three markets", "需调参": "needs re-parameterisation",
            "扣成本后无效": "not significant after costs", "无效": "not significant"}.get(label, label)


def _calendar(market: str) -> pd.DatetimeIndex:
    days = (RDAGENT_HOME / market / "qlib_data/cn_data/calendars/day.txt").read_text().split()
    return pd.DatetimeIndex(days)


def _findings_window(market: str, horizon: int, test_start: str) -> pd.Timestamp:
    """已有发现可使用的最后一个日期：该日标签用到的价格（t+N+1）必须早于回测测试期开始日（MINE-11）。"""
    cal = _calendar(market)
    i_test = cal.searchsorted(pd.Timestamp(test_start))
    return cal[i_test - horizon - 2]


def compute_prior_findings(horizon: int, test_start: str) -> tuple[pd.DataFrame, dict[str, pd.Timestamp]]:
    """按回测测试期开始日之前的数据，重新计算 Alpha158 各因子在三个市场的 rank IC、t 值与迁移性分类。

    不读取 transfer.csv：其验证期（2022–2023）与 RD-Agent 的测试期重叠，提供给 AI 会造成数据泄漏。
    """
    folder = RESULTS_DIR / f"h{horizon}"
    stats, ends = {}, {}
    for m in MARKETS:
        ric = pd.read_parquet(folder / f"ic_daily_{m}.parquet")
        ends[m] = _findings_window(m, horizon, test_start)
        ric = ric.loc[:ends[m]]
        stats[f"ric_{m}"] = ric.mean()
        stats[f"t_{m}"] = ric.apply(newey_west_t, lags=horizon)
    table = pd.DataFrame(stats)
    table["label"] = table.apply(classify, axis=1)
    return table, ends


def _prior_findings(market: str, horizon: int, test_start: str) -> str:
    if not (RESULTS_DIR / f"h{horizon}" / f"ic_daily_{market}.parquet").exists():
        return "No prior evaluation results are available for this horizon."
    t, ends = compute_prior_findings(horizon, test_start)
    period = f"2015-01-02 to {ends[market]:%Y-%m-%d}"
    lines = [
        f"Evaluation of the 158 Alpha158 factors on {period}, i.e. only data before the backtest test period "
        f"(rank IC with the next-{horizon}-trading-day return, Newey-West t-statistics, significance threshold |t| > 3):",
        "- Classification across Hong Kong / UK / France: "
        + "; ".join(f"{_label_en(k)}: {v}" for k, v in t["label"].value_counts().items()),
    ]
    col_t, col_r = f"t_{market}", f"ric_{market}"
    top = t.reindex(t[col_t].abs().sort_values(ascending=False).index).head(5)
    lines.append("- Strongest Alpha158 factors in this market over the same period:")
    for name, row in top.iterrows():
        others = ", ".join(f"{m}: t={row[f't_{m}']:.2f}" for m in MARKETS if m != market)
        lines.append(f"  - {name} ({_family(name)}): rank IC {row[col_r]:.3f}, t={row[col_t]:.2f}; other markets {others}")
    return "\n".join(lines)


class FactopiaFactorScenario(QlibFactorScenario):
    def __init__(self) -> None:
        super().__init__()  # 首次运行时由 RD-Agent 生成数据目录（含模板目录中的 daily_benchmark.h5）
        self._install_benchmark()
        self.market = os.environ["FACTOPIA_MARKET"]
        self.horizon = int(os.environ.get("FACTOPIA_HORIZON", 20))
        research_path = os.environ.get("FACTOPIA_RESEARCH")
        self.research = yaml.safe_load(Path(research_path).read_text(encoding="utf-8")) if research_path else {}
        self._background = self._background + "\n\n" + self._context()
        self._experiment_setting = self._setting_table()

    def _install_benchmark(self) -> None:
        """把基准指数文件放入正式与调试数据目录，并刷新提供给 AI 的数据说明。"""
        src = Path(qlib_exp_utils.__file__).parent / "factor_data_template" / BENCHMARK_FILE
        for folder in (FACTOR_COSTEER_SETTINGS.data_folder, FACTOR_COSTEER_SETTINGS.data_folder_debug):
            target = Path(folder) / BENCHMARK_FILE
            if src.exists():
                shutil.copy(src, target)
            if not target.exists():
                raise FileNotFoundError(f"基准指数文件缺失：{target}（检查 generate.py 与 benchmark.txt）")
        self._source_data = qlib_exp_utils.get_data_folder_intro()

    def _context(self) -> str:
        m = MARKETS[self.market]
        parts = [
            "====== Research context (Factopia cross-border research platform) ======",
            f"You are researching factors for {m['name']}. The same research is run separately in Hong Kong, "
            "the UK and France, and factors are compared across markets; signals that also work in other markets are preferred.",
            "",
            (CONTEXT_DIR / f"{self.market}.en.md").read_text(encoding="utf-8").strip(),
            "",
            "## Prediction horizon",
            f"The model predicts the return over the next {self.horizon} trading days "
            f"(label: Ref($close, -{self.horizon + 1})/Ref($close, -1) - 1). "
            f"In the backtest each stock is held for at least {self.horizon} trading days, so factors should "
            "carry information that persists over this horizon rather than one-day noise.",
            "",
            "## Prior findings",
            _prior_findings(self.market, self.horizon, str(FACTOR_PROP_SETTING.test_start)),
        ]
        if self.research:
            parts += ["", "## Research direction (written by the researcher; may be in Chinese, English or French)",
                      f"Direction: {self.research.get('direction', '').strip()}",
                      f"Hypothesis: {self.research.get('hypothesis', '').strip()}"]
            if self.research.get("constraints"):
                parts += ["", "## Constraints"] + [f"- {c}" for c in self.research["constraints"]]
        parts += ["", "## Data notes",
                  f"- daily_pv.h5: adjusted daily prices and volume of the constituent stocks, in {m['currency']}; "
                  f"instrument codes look like {m['example']}; only trading days of this exchange are included.",
                  f"- {BENCHMARK_FILE}: daily bars of the benchmark ({m['index']}), indexed by datetime only. "
                  "Use it for market-relative factors such as relative strength or beta. "
                  "The benchmark is NOT in daily_pv.h5 and is not a tradable instrument; "
                  "index volume may be missing.",
                  "- The output factor must contain values for the stocks in daily_pv.h5 only."]
        return "\n".join(parts)

    def _setting_table(self) -> str:
        s = FACTOR_PROP_SETTING
        return (
            "| Dataset | Model | Factors | Horizon | Data Split |\n"
            "|---|---|---|---|---|\n"
            f"| {MARKETS[self.market]['name']} | LGBModel | Alpha158 Plus | {self.horizon} trading days | "
            f"Train: {s.train_start} to {s.train_end} <br> Valid: {s.valid_start} to {s.valid_end} <br> "
            f"Test: {s.test_start} to {s.test_end} |"
        )
