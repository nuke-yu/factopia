"""发给 RD-Agent 的场景信息（Spec MINE-07、第 2.3.1 节、第 16.7 节）。

RD-Agent 场景子类（scenario.py）在运行时调用 build_context()；后端的提示词预览调用同一个函数，
因此页面上看到的内容就是实际发给 AI 的内容（RS-07）。

包括：市场介绍（团队审核）、预测周期、已有发现（只用测试期开始前的数据，MINE-11）、研究规格、数据说明。
"""
import os
from pathlib import Path

import pandas as pd

from factopia.factors.families import family
from factopia.factors.stats import classify, newey_west_t
from factopia.research.periods import findings_end

CONTEXT_DIR = Path(os.environ.get("FACTOPIA_CONTEXT_DIR", "/workspace/configs/rdagent/context"))
RESULTS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "alpha158"
CONTEXT_MARK = "====== Research context"
BENCHMARK_FILE = "daily_benchmark.h5"
FINDINGS_START = "2015-01-02"

MARKETS = {
    "hk": {"name": "Hong Kong (Hang Seng Index constituents)", "currency": "HKD", "example": "0700_HK", "index": "Hang Seng Index"},
    "uk": {"name": "UK (FTSE 100 constituents)", "currency": "GBP", "example": "HSBA_L", "index": "FTSE 100 Index"},
    "fr": {"name": "France (CAC 40 constituents)", "currency": "EUR", "example": "MC_PA", "index": "CAC 40 Index"},
}


def label_en(label: str) -> str:
    """把迁移性分类标签（中文，Spec 第 3.4 节）译为英文。"""
    if label.startswith("地区特定"):
        return "market-specific " + label[len("地区特定"):]
    return {"三市场通用": "universal across all three markets", "需调参": "needs re-parameterisation",
            "扣成本后无效": "not significant after costs", "无效": "not significant"}.get(label, label)


def compute_prior_findings(horizon: int, test_start: str, markets=tuple(MARKETS)) -> tuple[pd.DataFrame, dict[str, pd.Timestamp]]:
    """按回测测试期开始日之前的数据，重新计算 Alpha158 各因子在三个市场的 rank IC、t 值与迁移性分类。

    不读取 transfer.csv：其验证期（2022–2023）与 RD-Agent 的测试期重叠，提供给 AI 会造成数据泄漏。
    test_start 为本市场的测试期开始日；其他市场按各自的交易日历截到同一日期之前。
    """
    folder = RESULTS_DIR / f"h{horizon}"
    stats, ends = {}, {}
    for m in markets:
        ric = pd.read_parquet(folder / f"ic_daily_{m}.parquet")
        ends[m] = findings_end(m, horizon, test_start)
        ric = ric.loc[:ends[m]]
        stats[f"ric_{m}"] = ric.mean()
        stats[f"t_{m}"] = ric.apply(newey_west_t, lags=horizon)
    table = pd.DataFrame(stats)
    table["label"] = table.apply(classify, axis=1)
    return table, ends


def prior_findings_text(market: str, horizon: int, test_start: str, groups: list[str] | None = None) -> str:
    """已有发现摘要。groups 为研究员选择提供给 AI 的因子类型（界面上的中文分组名），只影响"最强因子"列表。"""
    if not (RESULTS_DIR / f"h{horizon}" / f"ic_daily_{market}.parquet").exists():
        return "No prior evaluation results are available for this horizon."
    t, ends = compute_prior_findings(horizon, test_start)
    period = f"{FINDINGS_START} to {ends[market]:%Y-%m-%d}"
    lines = [
        f"Evaluation of the 158 Alpha158 factors on {period}, i.e. only data before the backtest test period "
        f"(rank IC with the next-{horizon}-trading-day return, Newey-West t-statistics, significance threshold |t| > 3):",
        "- Classification across Hong Kong / UK / France: "
        + "; ".join(f"{label_en(k)}: {v}" for k, v in t["label"].value_counts().items()),
    ]
    col_t, col_r = f"t_{market}", f"ric_{market}"
    pool = t if not groups else t[[family(n)[0] in groups for n in t.index]]
    top = pool.reindex(pool[col_t].abs().sort_values(ascending=False).index).head(5)
    lines.append("- Strongest Alpha158 factors in this market over the same period"
                 + (" (restricted to the factor families selected by the researcher):" if groups else ":"))
    for name, row in top.iterrows():
        others = ", ".join(f"{m}: t={row[f't_{m}']:.2f}" for m in MARKETS if m != market)
        lines.append(f"  - {name} ({family(name)[1]}): rank IC {row[col_r]:.3f}, t={row[col_t]:.2f}; other markets {others}")
    return "\n".join(lines)


def _bullets(items) -> list[str]:
    return [f"- {x}" for x in (items or []) if str(x).strip()]


def research_section(research: dict) -> list[str]:
    """研究规格部分。锁定的版本（第 16.4 节）使用英文规范化假设；早期的手写示例只有方向与原文假设。"""
    if not research:
        return []
    if "rationale" not in research:  # 早期手写示例（configs/research）
        parts = ["## Research direction (written by the researcher; may be in Chinese, English or French)",
                 f"Direction: {str(research.get('direction', '')).strip()}",
                 f"Hypothesis: {str(research.get('hypothesis', '')).strip()}"]
        if research.get("constraints"):
            parts += ["", "## Constraints", *_bullets(research["constraints"])]
        return parts
    rationale = research.get("rationale") or {}
    parts = ["## Research specification (reviewed and locked by the research team)",
             f"Hypothesis: {str(research.get('hypothesis', '')).strip()}",
             f"Economic rationale ({rationale.get('category', 'other')}): {str(rationale.get('note', '')).strip()}"]
    if research.get("directions"):
        parts += ["", "Candidate feature directions to explore first:", *_bullets(research["directions"])]
    if research.get("exclusions"):
        parts += ["", "Directions to avoid (known not to work or already tested):", *_bullets(research["exclusions"])]
    if research.get("falsification"):
        parts += ["", f"Falsification test: {str(research['falsification']).strip()}"]
    if research.get("constraints"):
        parts += ["", "## Constraints", *_bullets(research["constraints"])]
    if research.get("past_runs"):
        parts += ["", "## Hypotheses tried in earlier runs (direction and whether it was adopted; no test-period numbers)",
                  *_bullets(research["past_runs"])]
    return parts


def build_context(market: str, horizon: int, research: dict | None, test_start: str) -> str:
    m = MARKETS[market]
    parts = [
        f"{CONTEXT_MARK} (Factopia cross-border research platform) ======",
        f"You are researching factors for {m['name']}. The same research is run separately in Hong Kong, "
        "the UK and France, and factors are compared across markets; signals that also work in other markets are preferred.",
        "",
        (CONTEXT_DIR / f"{market}.en.md").read_text(encoding="utf-8").strip(),
        "",
        "## Prediction horizon",
        f"The model predicts the return over the next {horizon} trading days "
        f"(label: Ref($close, -{horizon + 1})/Ref($close, -1) - 1). "
        f"In the backtest each stock is held for at least {horizon} trading days, so factors should "
        "carry information that persists over this horizon rather than one-day noise.",
        "",
        "## Prior findings",
        prior_findings_text(market, horizon, test_start, ((research or {}).get("evidence") or {}).get("groups")),
    ]
    section = research_section(research or {})
    if section:
        parts += ["", *section]
    parts += ["", "## Data notes",
              f"- daily_pv.h5: adjusted daily prices and volume of the constituent stocks, in {m['currency']}; "
              f"instrument codes look like {m['example']}; only trading days of this exchange are included.",
              f"- {BENCHMARK_FILE}: daily bars of the benchmark ({m['index']}), indexed by datetime only. "
              "Use it for market-relative factors such as relative strength or beta. "
              "The benchmark is NOT in daily_pv.h5 and is not a tradable instrument; "
              "index volume may be missing.",
              "- The output factor must contain values for the stocks in daily_pv.h5 only."]
    return "\n".join(parts)
