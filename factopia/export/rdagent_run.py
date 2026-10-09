"""导出 RD-Agent 运行日志：每轮的假设、因子定义与代码、回测指标、AI 评语、费用与耗时。

在 rdagent 容器中运行（日志是 RD-Agent 的序列化对象）：
    docker compose -f docker-compose.yml -f compose.local.yml run --rm --entrypoint "" rdagent \
        /opt/conda/envs/rdagent/bin/python -m factopia.export.rdagent_run [--log-dir <日志目录>]
不指定 --log-dir 时导出 /workspace/runtime/rdagent/<市场>/h<周期>/log/ 下的全部运行。

输出 /workspace/runtime/results/runs/<运行编号>/：
    run.json     运行信息、发给 AI 的场景信息、是否受数据泄漏影响
    rounds.json  每轮详情；回测目录路径供 backtests.py 读取每日收益与持仓
"""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from rdagent.log.storage import FileStorage

RDAGENT_ROOT = Path(os.environ.get("FACTOPIA_RDAGENT_ROOT", "/workspace/runtime/rdagent"))
RUNS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "runs"

METRICS = {
    "ic": "IC",
    "rank_ic": "Rank IC",
    "annual_return_with_cost": "1day.excess_return_with_cost.annualized_return",
    "annual_return_without_cost": "1day.excess_return_without_cost.annualized_return",
    "max_drawdown_with_cost": "1day.excess_return_with_cost.max_drawdown",
    "information_ratio_with_cost": "1day.excess_return_with_cost.information_ratio",
}
CONTEXT_MARK = "====== Research context"
# MINE-11 修复前，已有发现与市场介绍使用了 2022–2023（回测测试期）的评估结论
LEAK_MARKS = ("validation period 2022", "validation period 2022–2023")


def _metrics(result) -> dict | None:
    if result is None:
        return None
    return {k: (float(result[v]) if v in result.index else None) for k, v in METRICS.items()}


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _run_id(log_dir: Path) -> tuple[str, str, int]:
    market, horizon = log_dir.parts[-4], int(log_dir.parts[-3].lstrip("h"))
    stamp = re.sub(r"[-_]", "", log_dir.name)[:14]  # 2026-10-08_15-58-37-… → 20261008155837
    return f"{market}_h{horizon}_{stamp}", market, horizon


def export(log_dir: Path) -> Path:
    run_id, market, horizon = _run_id(log_dir)
    rounds: dict[int, dict] = {}
    run = {"run_id": run_id, "market": market, "horizon": horizon, "log_dir": str(log_dir),
           "context": None, "context_leak": None, "settings": {}, "baseline": None}

    def rnd(i: int) -> dict:
        return rounds.setdefault(i, {
            "loop": i, "status": "running", "hypothesis": None, "reason": None, "factors": [],
            "coding_attempts": 0, "metrics": None, "decision": None, "feedback": None,
            "workspace": None, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            "steps": {}, "started_at": None, "ended_at": None,
        })

    for msg in FileStorage(log_dir).iter_msg():
        tag, c = msg.tag, msg.content
        if tag == "scenario":
            bg = c.background
            run["context"] = bg[bg.index(CONTEXT_MARK):] if CONTEXT_MARK in bg else None
            run["context_leak"] = any(mark in bg for mark in LEAK_MARKS)
            continue
        if tag == "RDLOOP_SETTINGS":
            run["settings"] = {k: str(v) for k, v in c.items()
                               if k.startswith(("train_", "valid_", "test_"))}
            continue
        m = re.match(r"Loop_(\d+)\.(\w+)\.(.+)", tag)
        if not m:
            continue
        r, step, name = rnd(int(m.group(1))), m.group(2), m.group(3)
        if name == "hypothesis generation":
            r["hypothesis"], r["reason"] = c.hypothesis, c.reason
        elif name == "experiment generation":
            r["factors"] = [{"name": t.factor_name, "description": t.factor_description,
                             "formulation": t.factor_formulation, "variables": t.variables, "code": None}
                            for t in c]
        elif name.endswith("evolving feedback"):
            r["coding_attempts"] += 1
        elif name == "coder result":
            codes = {ws.target_task.factor_name: ws.file_dict.get("factor.py") for ws in c if ws is not None}
            for f in r["factors"]:
                f["code"] = codes.get(f["name"])
        elif name == "runner result":
            r["metrics"] = _metrics(c.result)
            r["workspace"] = str(c.experiment_workspace.workspace_path)
            if run["baseline"] is None and c.based_experiments:
                base = c.based_experiments[0]
                run["baseline"] = {"metrics": _metrics(base.result),
                                   "workspace": str(base.experiment_workspace.workspace_path)}
        elif name == "feedback":
            r["decision"] = bool(c.decision)
            r["feedback"] = {"observations": c.observations, "evaluation": c.hypothesis_evaluation,
                             "new_hypothesis": c.new_hypothesis, "reason": c.reason}
        elif name.endswith("token_cost"):
            r["cost_usd"] += c.get("cost", 0.0)
            r["prompt_tokens"] += c.get("prompt_tokens", 0)
            r["completion_tokens"] += c.get("completion_tokens", 0)
        elif name == "time_info":
            r["steps"][step] = (c["end_time"] - c["start_time"]).total_seconds()
            r["started_at"] = r["started_at"] or _iso(c["start_time"])
            r["ended_at"] = _iso(c["end_time"])
            if step == "record":
                r["status"] = "done"

    ordered = [rounds[i] for i in sorted(rounds)]
    if not ordered:
        raise ValueError("没有完成任何一轮（如启动后立即中止或仅为测试）")
    # 每轮开始时的"历史最好"：起点为基准，被采纳的轮次成为新的最好
    sota = run["baseline"]["metrics"] if run["baseline"] else None
    for r in ordered:
        r["sota_before"] = sota
        if r["decision"]:
            sota = r["metrics"]
    run.update({
        "rounds": len(ordered), "adopted": sum(bool(r["decision"]) for r in ordered),
        "cost_usd": round(sum(r["cost_usd"] for r in ordered), 4),
        "started_at": ordered[0]["started_at"] if ordered else None,
        "ended_at": ordered[-1]["ended_at"] if ordered else None,
        "status": "done" if ordered and all(r["status"] == "done" for r in ordered) else "incomplete",
    })

    out = RUNS_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
    (out / "rounds.json").write_text(json.dumps(ordered, ensure_ascii=False, indent=2))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", type=Path, help="单次运行的日志目录；不指定则导出全部")
    args = parser.parse_args()
    dirs = [args.log_dir] if args.log_dir else sorted(p for p in RDAGENT_ROOT.glob("*/h*/log/*") if p.is_dir())
    for d in dirs:
        try:
            out = export(d)
        except Exception as e:  # 中止或损坏的运行不影响其他运行的导出
            print(f"[跳过] {d}：{type(e).__name__}: {e}")
            continue
        run = json.loads((out / "run.json").read_text())
        print(f"{run['run_id']}: {run['rounds']} 轮，采纳 {run['adopted']}，费用 ${run['cost_usd']}，"
              f"{'受泄漏影响' if run['context_leak'] else '无泄漏标记'} → {out}")


if __name__ == "__main__":
    main()
