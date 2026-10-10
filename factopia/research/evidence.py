"""证据包（Spec 第 16.4 节、RS-05）：每条带出处与计算区间，只用测试期开始前的数据（MINE-11）。

- Alpha158 已有发现：按各市场的测试期开始日截断后重新计算；
- 以往运行：只给假设方向与是否采纳，不给测试期的具体数值（第 16.6 节）；受泄漏影响的运行不作为证据。
"""
import json
import os
from pathlib import Path

from factopia.factors.families import family
from factopia.factors.stats import T_THRESHOLD
from factopia.research.context import FINDINGS_START, RESULTS_DIR, compute_prior_findings
from factopia.research.periods import split_dates

RUNS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "runs"
TOP_N = 8


def alpha158_findings(markets: list[str], horizon: int, groups: list[str] | None = None) -> list[dict]:
    items = []
    if not (RESULTS_DIR / f"h{horizon}").exists():
        return items
    for m in markets:
        test_start = split_dates(m, horizon)["TEST_START"]
        table, ends = compute_prior_findings(horizon, test_start)
        rows = []
        for name, r in table.iterrows():
            group = family(name)[0]
            if groups and group not in groups:
                continue
            if abs(r[f"t_{m}"]) > T_THRESHOLD:
                rows.append({"name": name, "group": group, "desc": family(name)[2], "t": round(float(r[f"t_{m}"]), 2),
                             "ric": round(float(r[f"ric_{m}"]), 4), "label": r["label"]})
        rows.sort(key=lambda x: -abs(x["t"]))
        items.append({
            "id": f"alpha158_pre_test_h{horizon}_{m}", "market": m, "kind": "alpha158",
            "source": f"Alpha158 逐日 rank IC（runtime/results/alpha158/h{horizon}/ic_daily_{m}.parquet）",
            "period": {"start": FINDINGS_START, "end": f"{ends[m]:%Y-%m-%d}"}, "test_start": test_start,
            "significant": len(rows), "factors": rows[:TOP_N],
        })
    return items


def past_runs(markets: list[str], horizon: int) -> list[dict]:
    """以往运行中每一轮的假设与是否采纳（不含测试期数值）；受泄漏影响的运行跳过。"""
    items = []
    for p in sorted(RUNS_DIR.glob("*/run.json")):
        run = json.loads(p.read_text(encoding="utf-8"))
        if run["market"] not in markets or run["horizon"] != horizon or run.get("context_leak"):
            continue
        rounds = json.loads((p.parent / "rounds.json").read_text(encoding="utf-8"))
        for r in rounds:
            if r.get("hypothesis") and r.get("decision") is not None:
                items.append({"run_id": run["run_id"], "market": run["market"], "loop": r["loop"],
                              "hypothesis": r["hypothesis"], "adopted": bool(r["decision"])})
    return items


def past_runs_lines(items: list[dict]) -> list[str]:
    """写入研究规格（past_runs），由 context.research_section 发给 AI。"""
    return [f"[{'adopted' if x['adopted'] else 'rejected'}] ({x['market']}) {x['hypothesis']}" for x in items]
