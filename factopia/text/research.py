"""文本信号研究（BR-49、50；Spec 第 14.17 节）：从研究立项发起，一个市场一个任务（kind = "text"），由后台运行器执行。

研究规格 signal_source = "text" 时，"开始运行"不启动 RD-Agent，而是检验情报台已生成的文本信号（风险表述变化，
factopia/text/signal.py：按公告可获取时间生效、无未来数据），输出与 RD-Agent 运行相同格式的结果，
研究结论、诊断、组合、审批与监控因此沿用同一条流程：

- 数据集：香港 demo_hk、英国 demo_uk（模拟数据，仅用于流程演示）、法国 fr（AMF 真实公告）；
  模拟数据集使用自带的模拟行情，基准为股票池等权（factopia/sources.py），并在结果中标注；
- 时间段：与 RD-Agent 相同的划分规则（训练 / 验证 / 测试，测试期截至 2023 年底，2024 年起为未见数据）；
- 第 0 轮（RISK_CHANGE）：打分 = 风险表述变化原值（rank IC 方向与假设的表述一致）；回测按假设方向选股
  （score_direction = −1 表示打分越低越好），持股数与每日换仓数与 RD-Agent 回测相同（股票池 20%、持股数 10%），
  成本来自市场规则包；起点（baseline）为等权持有股票池（不使用文本信号）；
- 是否采纳：只看验证期（不看测试期）——rank IC 方向与假设一致，且扣成本超额高于起点；
- 可信度检查：未见数据期（2024 年起）、安慰剂（每天在有打分的股票之间随机打乱打分，20 次）、
  复现（从复核后的标注重新计算信号，与当时的信号逐值比较）、回测确定性（无随机因素，范围为单值）；
- 测试期每天有打分的股票少于 5 只的天数不足 20 天时，该市场标为"数据不足"，不参与采纳与迁移分类
  （法国真实公告从 2024 年 8 月开始，测试期内没有数据）。
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from factopia import rules, sources
from factopia.export.backtests import RUNS_DIR, quantile_returns
from factopia.research import periods as rperiods
from factopia.research import store, verdict
from factopia.text import signal as tsignal
from factopia.text.common import DATASETS, concept_names, ds_dir, read_json
from factopia.validate import metrics as vm

DATA_CUTOFF = "2023-12-31"        # 与 RD-Agent 数据截止日相同（MINE-03）
TOPK_RATIO, MIN_TOPK, DROP_RATIO, MIN_DROP = 0.20, 8, 0.10, 1   # 与 factopia/mining/topk.py 相同
MIN_STOCKS = 5
MIN_DAYS = 20
PLACEBO_N = 20
FACTOR = "RISK_CHANGE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inst(sym: str) -> str:
    return sym.replace(".", "_")


def split(cal: pd.DatetimeIndex, n: int) -> dict:
    """训练 / 验证 / 测试 / 未见数据期，规则与 factopia/research/periods.py 相同（测试期截至数据截止日）。"""
    days = [d.strftime("%Y-%m-%d") for d in cal]
    seen = [d for d in days if d <= DATA_CUTOFF]

    def last_on_or_before(day):
        return max(i for i, d in enumerate(seen) if d <= day)

    i_tr = last_on_or_before(rperiods.TRAIN_END)
    i_vs, i_ve = i_tr + n + 1, last_on_or_before(rperiods.VALID_END)
    i_ts, i_te = i_ve + n + 1, len(seen) - 2
    return {"train_start": rperiods.TRAIN_START, "train_end": seen[i_tr], "valid_start": seen[i_vs], "valid_end": seen[i_ve],
            "test_start": seen[i_ts], "test_end": seen[i_te], "holdout_start": days[i_te + 1], "holdout_end": days[-2]}


def sizing(n: int) -> tuple[int, int]:
    topk = max(MIN_TOPK, round(n * TOPK_RATIO))
    return topk, max(MIN_DROP, round(topk * DROP_RATIO))


def backtest(score: pd.DataFrame, close: pd.DataFrame, bench: pd.Series, start: str, end: str,
             topk: int, n_drop: int, cost: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """TopkDropout 回测（与 RD-Agent 的 Qlib 策略相同的规则，简化为等权）：
    第 t 日收盘后的打分决定 t+1 日收盘的调仓——卖出打分最差的 n_drop 只持仓（不在当日前 topk 内的），买入未持有的最好股票补足；
    持仓从 t+2 日起计收益。score 已按假设方向调整（越大越好）；没有打分的股票不买入，已持有的继续持有直至被卖出。"""
    ret = close.pct_change(fill_method=None)
    cal = close.loc[start:end].index
    held: list[str] = []
    rows, pos = [], []
    prev_score = None
    for d in cal:
        # 当日收益：昨日收盘的持仓
        r = float(ret.loc[d, held].fillna(0.0).mean()) if held else 0.0
        bought = sold = 0
        if prev_score is not None:
            s = prev_score.dropna()
            if len(s):
                rank = s.sort_values(ascending=False)
                top = list(rank.index[:topk])
                if not held:
                    buy, sell = top, []
                else:
                    out = [h for h in sorted(held, key=lambda x: s.get(x, -np.inf)) if h not in top][:n_drop]
                    keep = [h for h in held if h not in out]
                    buy = [x for x in rank.index if x not in keep][:max(0, topk - len(keep))]
                    sell = out
                held = [h for h in held if h not in sell] + buy
                bought, sold = len(buy), len(sell)
        k = max(len(held), 1)
        turnover = (bought + sold) / max(topk, 1)
        c = (bought * cost["buy"]["total"] + sold * cost["sell"]["total"]) / max(topk, 1)
        rows.append({"date": d, "return": r, "cost": c, "bench": float(bench.get(d, 0.0)), "turnover": turnover})
        pos += [(d, _inst(h), 1.0 / k) for h in held]
        prev_score = score.loc[d] if d in score.index else None
    bt = pd.DataFrame(rows).set_index("date")
    return bt, pd.DataFrame(pos, columns=["date", "instrument", "weight"])


def equal_weight(close: pd.DataFrame, bench: pd.Series, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """起点：每天等权持有股票池中有价格的股票（不使用文本信号）。"""
    ret = close.pct_change(fill_method=None).loc[start:end]
    avail = close.loc[start:end].notna()
    held = close.notna().shift(1, fill_value=False).loc[start:end]      # 前一日收盘有价格的股票
    r = ret.where(held).mean(axis=1).fillna(0.0)
    bt = pd.DataFrame({"return": r, "cost": 0.0, "bench": bench.reindex(r.index).fillna(0.0), "turnover": 0.0})
    w = avail.div(avail.sum(axis=1).replace(0, np.nan), axis=0)
    pos = w.stack().rename("weight").reset_index()
    pos.columns = ["date", "instrument", "weight"]
    pos = pos[pos["weight"] > 0]
    pos["instrument"] = pos["instrument"].map(_inst)
    return bt.rename_axis("date"), pos


def daily_ic(score: pd.DataFrame, label: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for d in score.index:
        s, y = score.loc[d], label.loc[d] if d in label.index else None
        if y is None:
            continue
        ok = s.notna() & y.notna()
        if ok.sum() < MIN_STOCKS:
            continue
        rows.append({"date": d, "ic": float(s[ok].corr(y[ok])), "rank_ic": float(s[ok].rank().corr(y[ok].rank()))})
    return pd.DataFrame(rows, columns=["date", "ic", "rank_ic"])


def metrics(bt: pd.DataFrame, sig: pd.DataFrame) -> dict:
    w, wo = vm.risk(bt["return"] - bt["bench"] - bt["cost"]), vm.risk(bt["return"] - bt["bench"])
    nav = (1 + bt["return"] - bt["cost"]).cumprod()
    dd_abs = float((nav / nav.cummax() - 1).min()) if len(nav) else None
    return {"ic": vm._r(sig["ic"].mean()) if len(sig) else None, "rank_ic": vm._r(sig["rank_ic"].mean()) if len(sig) else None,
            "annual_return_with_cost": w["annual_return"], "annual_return_without_cost": wo["annual_return"],
            "max_drawdown_with_cost": w["max_drawdown"], "information_ratio_with_cost": w["information_ratio"],
            "max_drawdown_abs": vm._r(dd_abs)}     # 绝对回撤（扣成本）：起点为等权持有股票池，超额回撤恒为 0，回撤检查改用绝对回撤


def _save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(vm_clean(data), ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def vm_clean(x):
    if isinstance(x, dict):
        return {k: vm_clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [vm_clean(v) for v in x]
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def _report(bt: pd.DataFrame) -> pd.DataFrame:
    out = bt.copy()
    out["excess_with_cost"] = out["return"] - out["bench"] - out["cost"]
    out["nav_with_cost"] = (1 + out["excess_with_cost"]).cumprod()
    out["nav_bench"] = (1 + out["bench"]).cumprod()
    cum = out["excess_with_cost"].cumsum()
    out["drawdown_with_cost"] = cum - cum.cummax().clip(lower=0.0)
    return out.rename_axis("date").reset_index()


def reproduce(dataset: str, stored: pd.DataFrame) -> dict:
    """从复核后的标注与已对齐的生效日重新计算信号，与当时写出的信号逐值比较。"""
    df = tsignal.scores(dataset, realign=False)
    px = tsignal.prices(dataset)
    again = tsignal.daily_signal(df, px).stack()
    orig = stored["risk_change"]
    both = pd.concat({"a": orig, "b": again.reindex(orig.index)}, axis=1)
    match = (both["a"] - both["b"]).abs() <= 1e-9 * (1 + both["a"].abs())
    return {"name": FACTOR, "n": int(len(both)), "match_share": round(float(match.mean()), 6) if len(both) else None}


def run(job: dict, log=print) -> str:
    """执行一个文本信号研究任务，返回运行编号。"""
    t0 = time.time()
    started = _now()
    spec = store.load_version(job["research_id"], job["version"])
    m, h = job["market"], int(job["horizon"])
    ds = sources.TEXT_DATASETS[m]
    cfg = DATASETS[ds]
    sim = ds if ds in sources.SIMULATED else None          # 模拟数据集使用自带的模拟行情
    rule = verdict.rules(spec)
    direction = rule["ric_sign"]
    run_id = f"{m}_h{h}_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    out = RUNS_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    log(f"{run_id}：文本信号研究 {job['research_id']} v{job['version']}，数据集 {ds}（{cfg['source']}）")

    stored = pd.read_parquet(ds_dir(ds) / "signal.parquet")
    summary = read_json(ds_dir(ds) / "summary.json") or {}
    close = sources.close(m, sim)
    universe = sources.universe(m, sim) or list(close.columns)
    close = close.reindex(columns=[c for c in close.columns if c in set(universe)])
    bench_lvl = sources.index(m, sim)
    bench = bench_lvl.reindex(bench_lvl.index.union(close.index)).ffill().pct_change(fill_method=None).reindex(close.index).fillna(0.0)
    raw = stored["risk_change"].unstack("instrument").reindex(index=close.index, columns=close.columns)
    label = close.shift(-(h + 1)) / close.shift(-1) - 1
    per = split(close.index, h)
    topk, n_drop = sizing(len(close.columns))
    cost = rules.costs(m)
    oriented = raw * direction

    def evaluate(s, e):
        bt, pos = backtest(oriented, close, bench, s, e, topk, n_drop, cost)
        sg = daily_ic(raw.loc[s:e], label)
        return bt, pos, sg

    log(f"{run_id}：时间段 {per}；持股 {topk} 只，每日最多换 {n_drop} 只；方向 {'打分越低越好' if direction < 0 else '打分越高越好'}")
    bt_t, pos_t, sig_t = evaluate(per["test_start"], per["test_end"])
    bl_t, blpos_t = equal_weight(close, bench, per["test_start"], per["test_end"])
    base_m = metrics(bl_t, pd.DataFrame(columns=["ic", "rank_ic"]))
    insufficient = None if len(sig_t) >= MIN_DAYS else (
        f"测试期（{per['test_start']} 至 {per['test_end']}）每天有打分的股票不少于 {MIN_STOCKS} 只的天数只有 {len(sig_t)} 天"
        f"（文本数据从 {stored.index.get_level_values(0).min():%Y-%m-%d} 开始）：数据不足，不判定")

    # 采纳：只看验证期
    bt_v, _, sig_v = evaluate(per["valid_start"], per["valid_end"])
    bl_v, _ = equal_weight(close, bench, per["valid_start"], per["valid_end"])
    mv, bv = metrics(bt_v, sig_v), metrics(bl_v, pd.DataFrame(columns=["ic", "rank_ic"]))
    ok_dir = mv["rank_ic"] is not None and mv["rank_ic"] * direction > 0
    ok_ret = None not in (mv["annual_return_with_cost"], bv["annual_return_with_cost"]) and mv["annual_return_with_cost"] > bv["annual_return_with_cost"]
    decision = bool(not insufficient and ok_dir and ok_ret)
    reason = (insufficient or
              f"验证期（{per['valid_start']} 至 {per['valid_end']}）rank IC {mv['rank_ic'] if mv['rank_ic'] is not None else '无'}"
              f"（假设方向{'为负' if direction < 0 else '为正'}，{'一致' if ok_dir else '不一致'}），"
              f"扣成本年化超额 {mv['annual_return_with_cost']} vs 起点 {bv['annual_return_with_cost']}（{'高于' if ok_ret else '不高于'}起点）"
              f" → {'采纳' if decision else '不采纳'}")
    log(f"{run_id}：{reason}")

    for sub in ("backtest", "positions", "signal", "quantile", "score", "validation"):
        (out / sub).mkdir(exist_ok=True)
    m_t = metrics(bt_t, sig_t) if not insufficient else None
    _save(_report(bl_t), out / "backtest" / "baseline.parquet")
    _save(blpos_t, out / "positions" / "baseline.parquet")
    if not insufficient:
        _save(_report(bt_t), out / "backtest" / "0.parquet")
        _save(pos_t, out / "positions" / "0.parquet")
        _save(sig_t, out / "signal" / "0.parquet")
        sc = raw.loc[per["test_start"]:per["test_end"]].stack().rename("score").reset_index()
        sc.columns = ["date", "instrument", "score"]
        sc["instrument"] = sc["instrument"].map(_inst)
        _save(sc, out / "score" / "0.parquet")
        s_ser = raw.loc[per["test_start"]:per["test_end"]].stack().rename_axis(["datetime", "instrument"])
        l_ser = label.loc[per["test_start"]:per["test_end"]].stack().rename_axis(["datetime", "instrument"])
        q = quantile_returns(s_ser, l_ser.reindex(s_ser.index), h)
        (out / "quantile" / "0.json").write_text(json.dumps(vm_clean(q), ensure_ascii=False), encoding="utf-8")

    names = concept_names()
    factor = {"name": FACTOR, "kind": "text", "dataset": ds, "code": None,
              "description": f"[文本信号] 管理层风险表述变化：每篇公告按概念（{'、'.join(names.values())}）统计净风险表述（每百段），"
                             f"与同一公司上一期比较的变化；按公告可获取时间生效，沿用最长 {tsignal.HOLD_DAYS} 个交易日。",
              "formulation": "\\text{RISK\\_CHANGE}_{i,t} = \\text{Tone}_{i,k} - \\text{Tone}_{i,k-1},\\quad \\text{Tone} = \\frac{100}{N_{para}}\\sum (\\text{direction}\\times\\text{confidence})",
              "variables": {"Tone": "一篇公告的净风险表述（每百段，复核后的概念标签）", "k": "该公司按时间排序的第 k 篇同类公告",
                            "t": "交易日：生效日（可获取时间之后的第一个收盘）起沿用"},
              "source": cfg["source"], "licence": cfg["licence"], "simulated": cfg["simulated"],
              "processing_delay_min": cfg["processing_delay_min"]}
    ended = _now()
    rounds = [{"loop": 0, "status": "insufficient" if insufficient else "done",
               "hypothesis": spec.get("hypothesis"), "reason": "文本信号研究：检验情报台生成的风险表述变化信号（不调用大模型生成代码）",
               "factors": [factor], "coding_attempts": 0, "metrics": m_t, "decision": decision,
               "feedback": {"observations": reason, "decision_basis": "验证期 rank IC 方向与扣成本超额（不使用测试期）",
                            "valid": {"ai": mv, "baseline": bv}},
               "workspace": None, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "steps": {},
               "started_at": started, "ended_at": ended, "sota_before": base_m}]
    run = {"run_id": run_id, "market": m, "horizon": h, "kind": "text",
           "research": {"id": job["research_id"], "version": job["version"], "path": str(store.version_path(job["research_id"], job["version"])),
                        "market": m, "horizon": h, "job_id": job["job_id"]},
           "context_leak": False, "settings": {k: per[k] for k in ("train_start", "train_end", "valid_start", "valid_end", "test_start", "test_end")},
           "baseline": {"metrics": base_m, "workspace": None, "label": "等权持有股票池（不使用文本信号）"},
           "rounds": 1, "adopted": int(decision), "cost_usd": 0.0, "started_at": started, "ended_at": ended,
           "status": "done", "duration_s": round(time.time() - t0, 1),
           "price_source": sources.describe(sim), "score_direction": direction, "insufficient": insufficient,
           "text_dataset": {"dataset": ds, "source": cfg["source"], "licence": cfg["licence"], "simulated": cfg["simulated"],
                            "n_filings": summary.get("n_filings"), "n_companies": summary.get("n_companies"),
                            "signal_start": f"{stored.index.get_level_values(0).min():%Y-%m-%d}",
                            "signal_end": f"{stored.index.get_level_values(0).max():%Y-%m-%d}"},
           "backtest_rule": {"topk": topk, "n_drop": n_drop, "cost": {"buy": cost["buy"]["total"], "sell": cost["sell"]["total"]}}}
    _write_json(out / "rounds.json", rounds)
    _write_json(out / "events.json", [{"t": 0.0, "loop": 0, "step": "text", "kind": "step", "name": "检验文本信号", "phase": "start"},
                                      {"t": run["duration_s"], "loop": 0, "step": "text", "kind": "step", "name": "检验文本信号", "phase": "end"}])
    _write_json(out / "run.json", run)

    if decision:
        validate(run, raw, oriented, close, bench, label, per, topk, n_drop, cost, stored, ds, base_m, m_t, log)
    log(f"{run_id}：完成（{run['duration_s']} 秒）")
    return run_id


def validate(run, raw, oriented, close, bench, label, per, topk, n_drop, cost, stored, ds, base_m, m_t, log) -> None:
    """可信度检查（格式与 factopia/validate/worker.py 相同）。"""
    h = run["horizon"]
    periods = {"test": (per["test_start"], per["test_end"]), "holdout": (per["holdout_start"], per["holdout_end"])}
    start, end = per["test_start"], per["holdout_end"]
    bt, _ = backtest(oriented, close, bench, start, end, topk, n_drop, cost)
    bl, _ = equal_weight(close, bench, start, end)
    sig = daily_ic(raw.loc[start:end], label).set_index("date")["rank_ic"]

    def per_period(report, ric, s_frame=None):
        res = {}
        for k, (a, b) in periods.items():
            q = None
            if s_frame is not None:
                s_ser = s_frame.loc[a:b].stack().rename_axis(["datetime", "instrument"])
                l_ser = label.loc[a:b].stack().rename_axis(["datetime", "instrument"])
                try:
                    q = quantile_returns(s_ser, l_ser.reindex(s_ser.index), h)
                except Exception:
                    q = None
            p = vm.period(report, ric, a, b, h, q)
            for key in ("annual_return_with_cost", "information_ratio_with_cost", "max_drawdown_with_cost", "annual_return_without_cost"):
                p[f"{key}_default"] = p[key]
                p[f"{key}_range"] = [p[key], p[key]] if p[key] is not None else None
                p[f"{key}_all"] = [p[key]] if p[key] is not None else []
            res[k] = p
        return res

    empty = pd.Series(dtype=float)
    ai, base = per_period(bt, sig, raw), per_period(bl, empty)
    log(f"{run['run_id']}：未见数据期 rank IC {ai['holdout']['rank_ic']}（t {ai['holdout']['rank_ic_t']}），"
        f"扣成本年化超额 {ai['holdout']['annual_return_with_cost']} vs 起点 {base['holdout']['annual_return_with_cost']}")
    rep = reproduce(ds, stored)
    log(f"{run['run_id']}：复现 {rep['match_share']}")
    dates = bt.index[::5].append(bt.index[-1:]).unique()
    curve = lambda b: [[round(float(v), 5) for v in (b["return"] - b["bench"] - b["cost"]).cumsum().reindex(dates).ffill()]]
    result = {"run_id": run["run_id"], "loop": 0, "market": run["market"], "horizon": h, "kind": "text",
              "started_at": _now(), "errors": [], "periods": {k: {"start": a, "end": b} for k, (a, b) in periods.items()},
              "factors": [rep], "ai": ai, "baseline": base, "seeds": 0,
              "curve": {"dates": [d.strftime("%Y-%m-%d") for d in dates], "ai": curve(bt), "baseline": curve(bl)},
              "original": {"ai": m_t, "baseline": base_m},
              "note": "文本信号回测没有随机因素（并列按股票代码），多次运行结果相同；安慰剂为每天在有打分的股票之间随机打乱打分"}
    rng_runs = []
    a, b = periods["test"]
    mask = raw.loc[a:b].notna()
    for seed in range(1, PLACEBO_N + 1):
        rng = np.random.default_rng(seed)
        sh = oriented.loc[a:b].copy()
        for d in sh.index:
            ok = mask.loc[d]
            if ok.sum() > 1:
                v = sh.loc[d, ok].to_numpy()
                sh.loc[d, ok] = rng.permutation(v)
        pb, _ = backtest(sh, close, bench, a, b, topk, n_drop, cost)
        mm = vm.risk(pb["return"] - pb["bench"] - pb["cost"])
        rng_runs.append({"seed": seed, "annual_return_with_cost": mm["annual_return"], "max_drawdown_with_cost": mm["max_drawdown"], "rank_ic": None})
    ai_test, bl_test = ai["test"]["annual_return_with_cost"], base["test"]["annual_return_with_cost"]
    rets = [r["annual_return_with_cost"] for r in rng_runs if r["annual_return_with_cost"] is not None]
    result["placebo"] = {"n": len(rets), "runs": rng_runs,
                         "share_ge_ai": round(sum(r >= ai_test for r in rets) / len(rets), 4) if rets and ai_test is not None else None,
                         "share_ge_baseline": round(sum(r >= bl_test for r in rets) / len(rets), 4) if rets and bl_test is not None else None,
                         "median": round(float(np.median(rets)), 6) if rets else None}
    result["ended_at"] = _now()
    _write_json(RUNS_DIR / run["run_id"] / "validation" / "0.json", result | {"status": "done"})
    log(f"{run['run_id']}：安慰剂 {result['placebo']['share_ge_ai']}")
