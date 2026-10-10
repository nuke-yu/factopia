"""导出 RD-Agent 每轮（含基准）的 Qlib 回测结果：每日收益与持仓、模型打分的每日 IC、分组收益。

在 app 容器中运行（持仓文件是 Qlib 对象），须先运行 rdagent_run.py：
    docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.export.backtests

对每个 /workspace/runtime/results/runs/<运行编号>/ 输出：
    backtest/<轮次>.parquet   日期、组合收益、成本、基准收益、换手率、组合净值与回撤（轮次 baseline 为起点）
    positions/<轮次>.parquet  日期、股票、权重
    signal/<轮次>.parquet     日期、模型打分与未来收益的 IC、RankIC
    quantile/<轮次>.json      按模型打分把股票分成 5 组后，各组的未来收益（因子分析的标准做法）
    score/<轮次>.parquet      日期、股票、模型打分（供诊断：中性化、冗余、稳定性，factopia.diagnostics）
"""
import json
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

RUNS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results")) / "runs"
GROUPS = 5


def _artifacts(workspace: str) -> Path | None:
    found = sorted(Path(workspace).glob("mlruns/*/*/artifacts/portfolio_analysis/report_normal_1day.pkl"))
    return found[-1].parent.parent if found else None


def _to_parquet(df: pd.DataFrame, path: Path) -> None:
    """先写临时文件再替换：运行中导出时，后端可能正在读取同一个文件。"""
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _load(path: Path):
    with path.open("rb") as f:
        return pickle.load(f)


def quantile_returns(score: pd.Series, label: pd.Series, horizon: int) -> dict:
    """每个交易日按模型打分把股票从低到高分成 5 组，统计各组的未来收益（标签：未来 horizon 日收益）。

    打分相同的股票取平均排名、落在同一组，因此各组股票数可能不等（tie_share 记录相同打分的比例）。
    累计收益每隔 horizon 个交易日调仓一次，避免相邻日期的未来收益重叠计算。
    """
    df = pd.concat({"score": score, "label": label}, axis=1).dropna()
    by_day = df.groupby(level="datetime")
    df["group"] = by_day["score"].transform(
        lambda s: np.ceil(s.rank(method="average") / len(s) * GROUPS).clip(1, GROUPS)).astype(int)
    daily = df.groupby([df.index.get_level_values("datetime"), "group"])["label"].mean().unstack().reindex(
        columns=range(1, GROUPS + 1))
    size = df.groupby([df.index.get_level_values("datetime"), "group"]).size().unstack().reindex(
        columns=range(1, GROUPS + 1)).fillna(0)
    universe = by_day["label"].mean()
    tie_share = by_day["score"].agg(lambda s: s.value_counts().iloc[0] / len(s))

    steps = daily.index[::horizon]
    nav = (1 + daily.loc[steps].fillna(0)).cumprod()  # 某组当天没有股票时按持有现金计
    nav_universe = (1 + universe.loc[steps]).cumprod()
    spread = daily[GROUPS] - daily[1]
    r = lambda x: None if pd.isna(x) else round(float(x), 6)
    return {
        "horizon": horizon, "groups": GROUPS, "days": len(daily),
        "mean": [r(daily[g].mean()) for g in range(1, GROUPS + 1)],
        "universe_mean": r(universe.mean()),
        "size": [round(float(size[g].mean()), 1) for g in range(1, GROUPS + 1)],
        "empty_days": [int((size[g] == 0).sum()) for g in range(1, GROUPS + 1)],
        "spread_mean": r(spread.mean()),
        "spread_positive_share": r((spread.dropna() > 0).mean()),
        "tie_share": r(tie_share.median()),
        "dates": [d.strftime("%Y-%m-%d") for d in steps],
        "nav": [[r(v) for v in nav[g]] for g in range(1, GROUPS + 1)],
        "nav_universe": [r(v) for v in nav_universe],
    }


def export_round(art: Path, out: Path, key: str, horizon: int) -> None:
    report = _load(art / "portfolio_analysis/report_normal_1day.pkl")
    bt = report[["return", "cost", "bench", "turnover"]].copy()
    bt["excess_with_cost"] = bt["return"] - bt["bench"] - bt["cost"]
    bt["nav_with_cost"] = (1 + bt["excess_with_cost"]).cumprod()
    bt["nav_bench"] = (1 + bt["bench"]).cumprod()
    # 回撤与 Qlib 的最大回撤指标算法一致：累计超额收益（逐日相加）离此前最高点的距离，起点为 0
    cum = bt["excess_with_cost"].cumsum()
    bt["drawdown_with_cost"] = cum - cum.cummax().clip(lower=0.0)
    _to_parquet(bt.rename_axis("date").reset_index(), out / "backtest" / f"{key}.parquet")

    positions = _load(art / "portfolio_analysis/positions_normal_1day.pkl")
    rows = [(d, inst, w) for d, pos in positions.items()
            for inst, w in pos.get_stock_weight_dict(only_stock=True).items()]
    _to_parquet(pd.DataFrame(rows, columns=["date", "instrument", "weight"]), out / "positions" / f"{key}.parquet")

    sig = pd.DataFrame({"ic": _load(art / "sig_analysis/ic.pkl"), "rank_ic": _load(art / "sig_analysis/ric.pkl")})
    _to_parquet(sig.rename_axis("date").reset_index(), out / "signal" / f"{key}.parquet")

    pred = _load(art / "pred.pkl")["score"]
    _to_parquet(pred.rename("score").rename_axis(["date", "instrument"]).reset_index(), out / "score" / f"{key}.parquet")
    q = quantile_returns(pred, _load(art / "label.pkl").iloc[:, 0], horizon)
    tmp = out / "quantile" / f"{key}.json.tmp"
    tmp.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out / "quantile" / f"{key}.json")


def export_run(run_dir: Path) -> int:
    run = json.loads((run_dir / "run.json").read_text())
    rounds = json.loads((run_dir / "rounds.json").read_text())
    for sub in ("backtest", "positions", "signal", "quantile", "score"):
        (run_dir / sub).mkdir(exist_ok=True)
    targets = [("baseline", run["baseline"]["workspace"] if run.get("baseline") else None)]
    targets += [(str(r["loop"]), r["workspace"]) for r in rounds]
    n = 0
    for key, ws in targets:
        art = _artifacts(ws) if ws else None
        if art is None:
            continue
        export_round(art, run_dir, key, run["horizon"])
        n += 1
    return n


def main() -> None:
    for run_dir in sorted(p for p in RUNS_DIR.glob("*") if (p / "run.json").exists()):
        print(f"{run_dir.name}: 导出 {export_run(run_dir)} 组回测结果")


if __name__ == "__main__":
    main()
