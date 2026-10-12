"""可信度检查的执行部分：在 dind 中的 Qlib 回测镜像里运行（无网络，见 jobs.py）。

    python -m factopia.validate.worker <任务文件.json>

步骤：
1. 用完整数据（/root/.qlib/qlib_data/cn_data，即 runtime/qlib_data/<市场>，含 2024 年以后）生成 daily_pv.h5 与
   daily_benchmark.h5，格式与 RD-Agent 的数据生成脚本一致（configs/rdagent/common/generate.py）；
2. 逐个运行该轮因子的 AI 代码（与 RD-Agent 相同：读取当前目录的 daily_pv.h5，写出 result.h5），
   并与 RD-Agent 当时的因子值逐日核对 2023 年底之前的部分（复现检查：不一致说明代码用到了未来数据或不可复现）；
3. 按该轮的 Qlib 配置（模型、参数、持股规则、交易成本不变）训练并回测到最新数据：AI 因子 + 20 个基础因子、
   只有 20 个基础因子（起点）各若干次；指标按测试期（2022–2023）与未见数据期（2024 年以后）分别计算；
4. 安慰剂：用同样数量的随机数代替 AI 的因子，在原测试期重复训练回测若干次。

并列敏感性（第 8 章 #21）：模型打分大量并列时，在并列股票中买哪几只会明显影响收益。回测镜像已让并列股票按股票代码
确定排序（docker/qlib/sitecustomize.py），结果可复现；这里另用 K 个"并列排序种子"（FACTOPIA_TIE_SEED = 1…K）
对并列顺序做固定的随机排列，衡量收益对并列顺序有多敏感。收益类指标报告默认排序的结果、全部结果的中位数与范围；
rank IC 与分组收益只取决于打分，不受影响。训练固定随机种子并使用确定性模式（deterministic），因此各次之间的差异只来自
并列排序；同时记录持股分数线上的并列股票数（ties）。
结果写入 results/runs/<运行编号>/validation/<轮次>.json。
"""
import copy
import datetime as dt
import json
import os
import pickle
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from factopia.export.backtests import quantile_returns
from factopia.validate import metrics

QLIB_DATA = "/root/.qlib/qlib_data/cn_data"
FIELDS = ["$open", "$close", "$high", "$low", "$volume", "$factor"]
FACTOR_TIMEOUT_S, QRUN_TIMEOUT_S = 900, 1800


def log(msg: str) -> None:
    print(f"[validate {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load(path: Path):
    with path.open("rb") as f:
        return pickle.load(f)


# ── 1. 完整数据 ──────────────────────────────────────────────

def ensure_data(cache: Path) -> tuple[Path, list[str]]:
    """生成（或复用）完整数据的 daily_pv.h5；按日历最后一天判断是否需要重新生成。"""
    cal = Path(QLIB_DATA, "calendars/day.txt").read_text().split()
    stamp = cache / "STAMP"
    if stamp.exists() and stamp.read_text() == cal[-1]:
        return cache, cal
    log(f"生成完整数据（截至 {cal[-1]}）")
    import qlib
    qlib.init(provider_uri=QLIB_DATA, region="us")
    from qlib.data import D
    tmp = cache.with_name(f"{cache.name}.tmp{os.urandom(4).hex()}")   # 并行的检查各用自己的临时目录
    tmp.mkdir(parents=True, exist_ok=True)
    data = D.features(D.instruments(), FIELDS, freq="day").swaplevel().sort_index().loc["2008-12-29":].sort_index()
    data.to_hdf(tmp / "daily_pv.h5", key="data")
    bench_list = Path(QLIB_DATA, "instruments/benchmark.txt")
    if bench_list.exists():
        code = bench_list.read_text().split()[0]
        D.features([code], FIELDS, freq="day").droplevel(0).sort_index().loc["2008-12-29":].to_hdf(tmp / "daily_benchmark.h5", key="data")
    (tmp / "STAMP").write_text(cal[-1])
    if stamp.exists() and stamp.read_text() == cal[-1]:     # 另一个检查已经生成
        shutil.rmtree(tmp, ignore_errors=True)
        return cache, cal
    old = cache.with_name(f"{cache.name}.old{os.urandom(4).hex()}")
    if cache.exists():
        cache.rename(old)
    tmp.rename(cache)
    shutil.rmtree(old, ignore_errors=True)
    return cache, cal


# ── 2. 因子 ──────────────────────────────────────────────────

def compute_factor(name: str, code: str, data: Path, work: Path) -> pd.Series:
    d = work / "factors" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "factor.py").write_text(code, encoding="utf-8")
    for f in ("daily_pv.h5", "daily_benchmark.h5"):
        if (data / f).exists() and not (d / f).exists():
            (d / f).symlink_to(data / f)
    (d / "result.h5").unlink(missing_ok=True)
    p = subprocess.run([sys.executable, "factor.py"], cwd=d, capture_output=True, text=True, timeout=FACTOR_TIMEOUT_S)
    if p.returncode != 0 or not (d / "result.h5").exists():
        tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
        raise RuntimeError(f"因子 {name} 运行失败：{' | '.join(tail)}")
    s = pd.read_hdf(d / "result.h5", key="data")
    s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
    if list(s.index.names) != ["datetime", "instrument"]:
        s = s.reorder_levels(["datetime", "instrument"])
    return s.sort_index().rename(name)


def reproduce(orig: pd.Series, new: pd.Series, end: str) -> dict:
    """RD-Agent 当时的因子值与重新计算的值在 end 之前逐日核对。"""
    a = orig.loc[:end].dropna()
    b = new.reindex(a.index)
    both = b.notna()
    if both.sum() == 0:
        return {"n": int(len(a)), "covered": 0.0, "match_share": 0.0, "corr": None}
    close = np.isclose(a[both].to_numpy(float), b[both].to_numpy(float), rtol=1e-5, atol=1e-8)
    return {"n": int(len(a)), "covered": round(float(both.mean()), 4), "match_share": round(float(close.mean()), 4),
            "corr": round(float(np.corrcoef(a[both], b[both])[0, 1]), 6) if both.sum() > 2 else None}


# ── 3. Qlib 配置与运行 ───────────────────────────────────────

def _plain(x):
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, (dt.date, dt.datetime, pd.Timestamp)):
        return str(pd.Timestamp(x).date())
    return x


SEED = 42


def deterministic(model: dict) -> dict:
    """固定训练的随机性（第 14.7 节）：LightGBM 用固定种子 + 确定性模式，PyTorch 模型固定 numpy / torch 种子。
    这样同一配置重新训练得到相同的打分，多次回测之间的差异只来自并列股票的排序。"""
    kw = model.setdefault("kwargs", {})
    if model.get("class") == "LGBModel":
        kw.update(seed=SEED, deterministic=True, force_row_wise=True)
    elif "pytorch" in str(model.get("module_path", "")):
        kw["seed"] = SEED
    return model


def make_config(base: dict, static: Path | None, test: tuple[str, str]) -> dict:
    """按该轮的配置生成 qrun 配置：只改数据路径、因子文件与测试期；static 为空时去掉 AI 因子（起点）。"""
    h = copy.deepcopy(_plain(base["data_handler_config"]))
    h["end_time"] = test[1]
    loaders = h["data_loader"]["kwargs"]["dataloader_l"]
    static_l = [x for x in loaders if x["class"].endswith("StaticDataLoader")]
    if static is None:
        h["data_loader"]["kwargs"]["dataloader_l"] = [x for x in loaders if x not in static_l]
    else:
        for x in static_l:
            x["kwargs"]["config"] = str(static)
    pa = copy.deepcopy(_plain(base["port_analysis_config"]))
    pa["backtest"]["end_time"] = test[1]
    task = _plain({k: v for k, v in base["task"].items() if k in ("model",)})
    deterministic(task["model"])
    hd = base["task"]["dataset"]["kwargs"]["handler"]
    seg = _plain(base["task"]["dataset"]["kwargs"]["segments"])
    seg["test"] = list(test)
    task["dataset"] = {"class": "DatasetH", "module_path": "qlib.data.dataset",
                       "kwargs": {"handler": {"class": hd["class"], "module_path": hd.get("module_path", "qlib.contrib.data.handler"), "kwargs": h}, "segments": seg}}
    task["record"] = [
        {"class": "SignalRecord", "module_path": "qlib.workflow.record_temp", "kwargs": {"model": "<MODEL>", "dataset": "<DATASET>"}},
        {"class": "SigAnaRecord", "module_path": "qlib.workflow.record_temp", "kwargs": {"ana_long_short": False, "ann_scaler": 252}},
        {"class": "PortAnaRecord", "module_path": "qlib.workflow.record_temp", "kwargs": {"config": pa}},
    ]
    return {"qlib_init": {"provider_uri": QLIB_DATA, "region": "us"}, "market": base["market"], "benchmark": base["benchmark"],
            "port_analysis_config": pa, "task": task}


def qrun(conf: dict, d: Path, tie_seed: int | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "conf.yaml").write_text(yaml.safe_dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "FACTOPIA_TIE_SEED"}
    if tie_seed:
        env["FACTOPIA_TIE_SEED"] = str(tie_seed)
    p = subprocess.run(["qrun", "conf.yaml"], cwd=d, capture_output=True, text=True, timeout=QRUN_TIMEOUT_S, env=env)
    found = sorted(d.glob("mlruns/*/*/artifacts/portfolio_analysis/report_normal_1day.pkl"))
    if p.returncode != 0 or not found:
        tail = [x for x in (p.stderr or p.stdout).strip().splitlines() if x.strip()][-3:]
        raise RuntimeError(f"Qlib 回测失败（{d.name}）：{' | '.join(tail)}")
    return found[-1].parent.parent


def evaluate(art: Path, periods: dict, horizon: int) -> tuple[dict, pd.DataFrame]:
    report = _load(art / "portfolio_analysis/report_normal_1day.pkl")
    ric = _load(art / "sig_analysis/ric.pkl")
    score, label = _load(art / "pred.pkl")["score"], _load(art / "label.pkl").iloc[:, 0]
    out = {}
    for key, (start, end) in periods.items():
        days = score.index.get_level_values("datetime")
        sel = (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))
        try:
            q = quantile_returns(score[sel], label.reindex(score.index)[sel], horizon)
        except Exception:
            q = None
        out[key] = metrics.period(report, ric, start, end, horizon, q)
    return out, report


SEED_DEPENDENT = ("annual_return_with_cost", "information_ratio_with_cost", "max_drawdown_with_cost", "annual_return_without_cost")


def repeated(conf: dict, name: str, work: Path, periods: dict, horizon: int, seeds: int) -> tuple[dict, list]:
    """默认排序跑一次，再用 seeds 个并列排序种子各跑一次：收益类指标给默认值、中位数、范围与全部取值；
    rank IC 等只取决于打分，取默认那次。"""
    runs, first_art = [], None
    for k in range(0, seeds + 1):            # 0 = 默认（按股票代码）
        art = qrun(conf, work / f"qrun_{name}_{k}", k or None)
        first_art = first_art or art
        m, report = evaluate(art, periods, horizon)
        runs.append((m, report))
        log(f"{name} {'默认排序' if k == 0 else f'并列种子 {k}'}：" + "，".join(f"{p} {m[p]['annual_return_with_cost']:.1%}" for p in periods))
    out = {}
    for p in periods:
        first = dict(runs[0][0][p])
        for key in SEED_DEPENDENT:
            vals = [r[0][p][key] for r in runs if r[0][p][key] is not None]
            first[f"{key}_default"] = first[key]
            first[key] = round(float(np.median(vals)), 6) if vals else None
            first[f"{key}_range"] = [min(vals), max(vals)] if vals else None
            first[f"{key}_all"] = vals
        out[p] = first
    # 打分并列（只取决于打分，各次相同）：按时间段统计持股分数线上的并列
    score = _load(first_art / "pred.pkl")["score"]
    days = score.index.get_level_values("datetime")
    for p, (start, end) in periods.items():
        out[p]["ties"] = metrics.ties(score[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))])
    curves = [(r[1]["return"] - r[1]["bench"] - r[1]["cost"]).cumsum() for r in runs]
    return out, curves


# ── 主流程 ───────────────────────────────────────────────────

def main(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    work, out_path, horizon = Path(spec["work_dir"]), Path(spec["output"]), spec["horizon"]
    work.mkdir(parents=True, exist_ok=True)
    result = {"run_id": spec["run_id"], "loop": spec["loop"], "market": spec["market"], "horizon": horizon,
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "errors": []}
    base = _load(Path(spec["config_path"]))
    seg = _plain(base["task"]["dataset"]["kwargs"]["segments"])
    test_start, test_end = seg["test"]

    data, cal = ensure_data(Path(spec["data_cache"]))
    holdout_start = next(d for d in cal if d > test_end)
    holdout_end = cal[-2]                       # 回测最后一步需要读取下一个交易日
    periods = {"test": (test_start, test_end), "holdout": (holdout_start, holdout_end)}
    result["periods"] = {k: {"start": a, "end": b} for k, (a, b) in periods.items()}

    # 2. 因子：重新计算并核对
    orig = pd.read_parquet(spec["combined_path"])
    cols = [c[1] if isinstance(c, tuple) else c for c in orig.columns]
    series, checks = [], []
    for name in cols:
        log(f"计算因子 {name}")
        s = compute_factor(name, spec["codes"][name], data, work)
        o = orig[("feature", name)] if ("feature", name) in orig.columns else orig[name]
        checks.append({"name": name, **reproduce(o, s, test_end)})
        series.append(s)
    full = pd.concat(series, axis=1)
    full.columns = pd.MultiIndex.from_tuples([("feature", c) for c in full.columns])
    full.index = full.index.set_names(["datetime", "instrument"])
    full = full.sort_index()                    # 各因子的日期范围不同，合并后需重新排序（Qlib 要求有序索引）
    static = work / "combined_full.parquet"
    full.to_parquet(static)
    result["factors"] = checks
    log("复现检查：" + "，".join(f"{c['name']} 一致 {c['match_share']:.1%}" for c in checks))

    # 3. AI 因子与起点：训练并回测到最新数据，每个各用多个哈希种子
    seeds = spec.get("seeds", 5)
    log(f"训练并回测（AI 因子 + 20 个基础因子）：{test_start} 至 {holdout_end}，{seeds} 个种子")
    ai_m, ai_curves = repeated(make_config(base, static, (test_start, holdout_end)), "ai", work, periods, horizon, seeds)
    log("训练并回测（起点：20 个基础因子）")
    bl_m, bl_curves = repeated(make_config(base, None, (test_start, holdout_end)), "baseline", work, periods, horizon, seeds)
    result["ai"], result["baseline"], result["seeds"] = ai_m, bl_m, seeds
    result["deterministic"] = True                # 训练固定种子与确定性模式（2026-10-13 起）
    dates = ai_curves[0].index[::5].append(ai_curves[0].index[-1:]).unique()   # 每 5 个交易日一个点
    pts = lambda cs: [[round(float(v), 5) for v in c.reindex(dates).ffill()] for c in cs]
    result["curve"] = {"dates": [d.strftime("%Y-%m-%d") for d in dates], "ai": pts(ai_curves), "baseline": pts(bl_curves)}
    result["original"] = spec.get("original")
    _write(out_path, result | {"status": "placebo"})

    # 4. 安慰剂：随机因子，原测试期
    runs = []
    idx = orig.index
    for seed in range(1, spec.get("placebo_n", 20) + 1):
        rng = np.random.default_rng(seed)
        rnd = pd.DataFrame(rng.standard_normal((len(idx), len(cols))).astype("float32"), index=idx,
                           columns=pd.MultiIndex.from_tuples([("feature", f"RANDOM_{i}") for i in range(len(cols))]))
        p = work / "placebo.parquet"
        rnd.to_parquet(p)
        try:
            m, _ = evaluate(qrun(make_config(base, p, (test_start, test_end)), work / f"qrun_placebo_{seed}"),
                            {"test": periods["test"]}, horizon)
            runs.append({"seed": seed, **{k: m["test"][k] for k in ("annual_return_with_cost", "max_drawdown_with_cost", "rank_ic")}})
            log(f"安慰剂 {seed}：扣成本年化 {m['test']['annual_return_with_cost']:.1%}")
        except Exception as e:  # 单次失败不影响其余
            result["errors"].append(f"安慰剂 {seed}：{e}")
    ai_test, bl_test = ai_m["test"]["annual_return_with_cost"], bl_m["test"]["annual_return_with_cost"]
    rets = [r["annual_return_with_cost"] for r in runs if r["annual_return_with_cost"] is not None]
    result["placebo"] = {"n": len(rets), "runs": runs,
                         "share_ge_ai": round(sum(r >= ai_test for r in rets) / len(rets), 4) if rets and ai_test is not None else None,
                         "share_ge_baseline": round(sum(r >= bl_test for r in rets) / len(rets), 4) if rets and bl_test is not None else None,
                         "median": round(float(np.median(rets)), 6) if rets else None}
    result["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _write(out_path, result | {"status": "done"})
    log("完成")


def _clean(x):
    """NaN / inf 改为 null：标准 JSON 不支持，后端无法返回（如常数因子的相关系数）。"""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_clean(data), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    main(sys.argv[1])
