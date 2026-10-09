"""导出 RD-Agent 每轮（含基准）的 Qlib 回测结果：每日收益与持仓、模型打分的每日 IC。

在 app 容器中运行（持仓文件是 Qlib 对象），须先运行 rdagent_run.py：
    docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.export.backtests

对每个 /workspace/runtime/results/runs/<运行编号>/ 输出：
    backtest/<轮次>.parquet   日期、组合收益、成本、基准收益、换手率、组合净值（轮次 baseline 为起点）
    positions/<轮次>.parquet  日期、股票、权重
    signal/<轮次>.parquet     日期、模型打分与未来收益的 IC、RankIC
"""
import json
import os
import pickle
from pathlib import Path

import pandas as pd

RUNS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "runs"


def _artifacts(workspace: str) -> Path | None:
    found = sorted(Path(workspace).glob("mlruns/*/*/artifacts/portfolio_analysis/report_normal_1day.pkl"))
    return found[-1].parent.parent if found else None


def _load(path: Path):
    with path.open("rb") as f:
        return pickle.load(f)


def export_round(art: Path, out: Path, key: str) -> None:
    report = _load(art / "portfolio_analysis/report_normal_1day.pkl")
    bt = report[["return", "cost", "bench", "turnover"]].copy()
    bt["excess_with_cost"] = bt["return"] - bt["bench"] - bt["cost"]
    bt["nav_with_cost"] = (1 + bt["excess_with_cost"]).cumprod()
    bt["nav_bench"] = (1 + bt["bench"]).cumprod()
    bt.rename_axis("date").reset_index().to_parquet(out / "backtest" / f"{key}.parquet", index=False)

    positions = _load(art / "portfolio_analysis/positions_normal_1day.pkl")
    rows = [(d, inst, w) for d, pos in positions.items()
            for inst, w in pos.get_stock_weight_dict(only_stock=True).items()]
    pd.DataFrame(rows, columns=["date", "instrument", "weight"]).to_parquet(
        out / "positions" / f"{key}.parquet", index=False)

    sig = pd.DataFrame({"ic": _load(art / "sig_analysis/ic.pkl"), "rank_ic": _load(art / "sig_analysis/ric.pkl")})
    sig.rename_axis("date").reset_index().to_parquet(out / "signal" / f"{key}.parquet", index=False)


def export_run(run_dir: Path) -> int:
    run = json.loads((run_dir / "run.json").read_text())
    rounds = json.loads((run_dir / "rounds.json").read_text())
    for sub in ("backtest", "positions", "signal"):
        (run_dir / sub).mkdir(exist_ok=True)
    targets = [("baseline", run["baseline"]["workspace"] if run.get("baseline") else None)]
    targets += [(str(r["loop"]), r["workspace"]) for r in rounds]
    n = 0
    for key, ws in targets:
        art = _artifacts(ws) if ws else None
        if art is None:
            continue
        export_round(art, run_dir, key)
        n += 1
    return n


def main() -> None:
    for run_dir in sorted(p for p in RUNS_DIR.glob("*") if (p / "run.json").exists()):
        print(f"{run_dir.name}: 导出 {export_run(run_dir)} 组回测结果")


if __name__ == "__main__":
    main()
