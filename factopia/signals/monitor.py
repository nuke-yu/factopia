"""信号监控（业务需求 BR-31、42；Spec 第 14.15 节）。由后台运行器执行（任务 kind = "monitor"，一个市场版本一个任务）。

"上线后"期间：测试期结束之后的数据（2024 年起，AI 与研究员都没有用过），作为演示用的上线期；参照期为该运行的测试期。
1. 用获批时冻结的因子代码（审批材料 materials/factors.json）在完整数据（可信度检查生成的 daily_pv.h5）上重算因子值；
   各因子截面标准化，按参照期 rank IC 的方向对齐后等权合成为一个信号（不重新训练模型，避免监控本身引入新的拟合）。
2. 按市场计算监控指标（参照期 vs 上线期）：
   信息系数（IC / rank IC、滚动 60 日、Newey-West t）、收益（前 20% 股票相对股票池等权的超额，扣成本前后）、
   衰减（预测周期 1–60 日的 rank IC）、命中率（rank IC 为正的月份占比、前 20% 跑赢中位数的比例）、
   暴露漂移（前 20% 的规模 / 动量 / 波动 / 反转暴露）、拥挤度（前 20% 股票之间的平均收益相关 / 全体平均相关）、
   换手率（前 20% 月度单边换手）、容量（10% 日均成交额参与率）、实施偏差（推迟一天执行的收益差 + 成本拖累）、
   数据漂移（信号分布的 PSI、因子缺失率）、市场状态敏感性（高 / 低波动期、指数涨 / 跌月份的 rank IC）。
3. 预警：统计（上线期 rank IC 显著低于参照期、或滚动 60 日 rank IC 低于获批阈值、或方向显著反转）与经济（扣成本超额为负且
   相对基准回撤超过获批阈值）同时成立时建议"暂停该市场版本"；只有观察项时建议"加强观察"。每条预警附诊断说明。
   只对受影响的市场提出建议（BR-42），是否暂停由人决定（信号目录）。
"""
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from factopia import rules
from factopia.validate import metrics as vm

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
SIGNALS = RUNTIME / "signals"
DATA = RUNTIME / "validate" / "data"
HORIZONS = (1, 5, 10, 20, 40, 60)
TOP = 0.2
ADV_DAYS, PARTICIPATION = 20, 0.10
FACTOR_TIMEOUT_S = 900


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), nd)


def _symbol(code: str) -> str:
    head, _, tail = code.rpartition("_")
    return f"{head}.{tail}" if head else code


# ---------------------------------------------------------------- 1. 信号

def compute_factor(name: str, code: str, data: Path, work: Path) -> pd.Series:
    """与可信度检查相同的方式运行因子代码（factor.py 读取 daily_pv.h5，写出 result.h5）。"""
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


def _zs(df: pd.DataFrame) -> pd.DataFrame:
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1).replace(0, np.nan), axis=0)


def _rank_ic(score: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    s, f = score.rank(axis=1), fwd.reindex_like(score).rank(axis=1)
    ok = score.notna() & fwd.reindex_like(score).notna()
    s, f = s.where(ok), f.where(ok)
    sc, fc = s.sub(s.mean(axis=1), axis=0), f.sub(f.mean(axis=1), axis=0)
    ic = (sc * fc).sum(axis=1) / np.sqrt((sc ** 2).sum(axis=1) * (fc ** 2).sum(axis=1))
    return ic[ok.sum(axis=1) >= 10]


def build_signal(factors: list[dict], market: str, ref: tuple[str, str], horizon: int, work: Path,
                 close: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], dict]:
    data = DATA / market
    if not (data / "daily_pv.h5").exists():
        raise RuntimeError(f"缺少完整数据 {data}/daily_pv.h5：先对该市场运行一次可信度检查")
    fwd = close.shift(-horizon) / close - 1
    parts, info, raw = [], [], {}
    for f in factors:
        s = compute_factor(f["name"], f["code"], data, work)
        df = s.unstack("instrument")
        df.columns = [_symbol(c) for c in df.columns]
        df.index = pd.to_datetime(df.index)
        df = df.reindex(index=close.index, columns=close.columns)
        z = _zs(df)
        ic_ref = _rank_ic(z.loc[ref[0]:ref[1]], fwd).mean() if z.loc[ref[0]:ref[1]].notna().any().any() else float("nan")
        sign = 1.0 if (ic_ref if not pd.isna(ic_ref) else 0) >= 0 else -1.0
        cov_ref = float(df.loc[ref[0]:ref[1]].notna().to_numpy().mean())
        cov_live = float(df.loc[ref[1]:].iloc[1:].notna().to_numpy().mean()) if len(df.loc[ref[1]:]) > 1 else 0.0
        info.append({"name": f["name"], "sign": sign, "ref_rank_ic": _r(ic_ref), "coverage_ref": _r(cov_ref, 3), "coverage_live": _r(cov_live, 3)})
        raw[f["name"]] = df
        parts.append(z * sign)
    # 各因子可用时取平均（有的因子只覆盖部分日期）；覆盖率写入结果，缺失本身也是数据漂移的一部分
    score = pd.concat(parts, keys=range(len(parts))).groupby(level=1).mean()
    score = score.reindex(close.index)
    return score, info, raw


# ---------------------------------------------------------------- 2. 指标

def _period_stats(score: pd.DataFrame, close: pd.DataFrame, adv: pd.DataFrame, styles: dict, idx: pd.Series,
                  start, end, horizon: int, costs: dict) -> dict:
    sc = score.loc[start:end]
    ret = close.pct_change(fill_method=None)
    fwd = close.shift(-horizon) / close - 1
    ric = _rank_ic(sc, fwd)
    sig = vm.significance(ric, horizon) if len(ric) > 30 else {"t": None}
    monthly_ic = ric.resample("ME").mean()
    # 前 20%：每月最后一个交易日选股，次日起持有一个月
    dates = sc.index
    rebal = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
    tops, held = [], {}
    for d in rebal:
        row = sc.loc[d].dropna()
        if len(row) < 10:
            continue
        held[d] = set(row.sort_values(ascending=False).index[: max(1, int(len(row) * TOP))])
    if not held:
        return {"rank_ic": None}
    keys = sorted(held)
    daily_top, daily_top_lag, turn = [], [], []
    for i, d in enumerate(keys):
        nxt = keys[i + 1] if i + 1 < len(keys) else ret.loc[start:end].index[-1]
        span = ret.loc[d:nxt].iloc[1:]
        names = list(held[d])
        daily_top.append(span[names].mean(axis=1) - span.mean(axis=1))
        lag = ret.loc[d:nxt].iloc[2:]                                  # 推迟一天执行
        daily_top_lag.append(lag[names].mean(axis=1) - lag.mean(axis=1))
        if i > 0:
            prev = held[keys[i - 1]]
            turn.append(len(set(names) - prev) / max(len(names), 1))
    ex = pd.concat(daily_top).sort_index()
    ex_lag = pd.concat(daily_top_lag).sort_index()
    years = max(len(ex) / 252, 1e-9)
    gross = float((1 + ex).prod() ** (1 / years) - 1) if len(ex) else None
    t_avg = float(np.mean(turn)) if turn else 0.0
    cost_ann = t_avg * 12 * (costs["buy"]["total"] + costs["sell"]["total"])
    nav = (1 + ex).cumprod()
    # 命中率
    hit_months = float((monthly_ic > 0).mean()) if len(monthly_ic) else None
    beat = []
    for d in keys:
        nxt = fwd.loc[d]
        beat.append(float((nxt[list(held[d])] > nxt.median()).mean()))
    # 暴露：前 20% 股票的标准化风格暴露（月度平均）
    expo = {}
    for k, st in styles.items():
        z = _zs(st)
        expo[k] = _r(np.nanmean([z.loc[d, list(held[d])].mean() for d in keys if d in z.index]), 3)
    # 拥挤度：前 20% 股票之间的平均两两收益相关，相对全体（每次调仓用之前 60 日）
    crowd = []
    for d in keys[::3]:
        w = ret.loc[:d].iloc[-60:]
        names = [n for n in held[d] if w[n].notna().sum() > 40]
        if len(names) < 3:
            continue
        c_top = w[names].corr().to_numpy()
        c_all = w.dropna(axis=1, thresh=40).corr().to_numpy()
        m_top = c_top[np.triu_indices_from(c_top, 1)].mean()
        m_all = np.nanmean(c_all[np.triu_indices_from(c_all, 1)])
        if m_all > 0:
            crowd.append(m_top / m_all)
    # 容量（本地币种）：前 20% 等权，按 10% 日均成交额参与率
    cap = []
    for d in keys:
        a = adv.loc[:d].iloc[-1][list(held[d])].dropna()
        if len(a):
            cap.append(float((a * PARTICIPATION).min() * len(a)))
    # 市场状态：指数 20 日波动高 / 低，指数月度涨 / 跌
    iv = idx.pct_change(fill_method=None).rolling(20).std().reindex(ric.index)
    hi = ric[iv > iv.median()].mean() if iv.notna().any() else None
    lo = ric[iv <= iv.median()].mean() if iv.notna().any() else None
    im = idx.resample("ME").last().pct_change(fill_method=None)
    up = monthly_ic[im.reindex(monthly_ic.index) > 0].mean()
    dn = monthly_ic[im.reindex(monthly_ic.index) <= 0].mean()
    roll = ric.rolling(60, min_periods=40).mean()
    return {
        "start": str(pd.Timestamp(sc.index[0]).date()), "end": str(pd.Timestamp(sc.index[-1]).date()), "days": int(len(sc)),
        "rank_ic": _r(ric.mean()), "rank_ic_t": _r(sig.get("t"), 2), "rank_ic_std": _r(ric.std()),
        "rolling_rank_ic_60d_last": _r(roll.dropna().iloc[-1]) if roll.notna().any() else None,
        "rolling_rank_ic_60d_min": _r(roll.min()),
        "monthly_rank_ic": {str(k.date()): _r(v) for k, v in monthly_ic.items()},
        "hit_rate_months": _r(hit_months, 3), "hit_rate_top": _r(np.mean(beat), 3),
        "excess_gross_ann": _r(gross), "cost_ann": _r(cost_ann), "excess_net_ann": _r(gross - cost_ann) if gross is not None else None,
        "max_drawdown_vs_universe": _r(float((nav / nav.cummax() - 1).min())) if len(nav) else None,
        "implementation_delay_ann": _r(float(((1 + ex_lag).prod() ** (1 / max(len(ex_lag) / 252, 1e-9)) - 1) - gross) if gross is not None and len(ex_lag) else None),
        "turnover_monthly": _r(t_avg, 3), "exposure": expo, "crowding": _r(np.mean(crowd), 3) if crowd else None,
        "capacity_local": _r(np.median(cap), 0) if cap else None,
        "regime": {"high_vol": _r(hi), "low_vol": _r(lo), "up_months": _r(up), "down_months": _r(dn)},
        "_ric": ric, "_monthly": monthly_ic,
    }


def _decay(score, close, start, end) -> dict:
    out = {}
    for h in HORIZONS:
        fwd = close.shift(-h) / close - 1
        out[str(h)] = _r(_rank_ic(score.loc[start:end], fwd).mean())
    return out


def _psi(a: np.ndarray, b: np.ndarray, bins: int = 10) -> float | None:
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 100 or len(b) < 100:
        return None
    edges = np.unique(np.quantile(a, np.linspace(0, 1, bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    pa = np.histogram(a, edges)[0] / len(a) + 1e-6
    pb = np.histogram(b, edges)[0] / len(b) + 1e-6
    return float(np.sum((pb - pa) * np.log(pb / pa)))


def alerts(ref: dict, live: dict, thresholds: dict, horizon: int, psi: float | None, missing_live: float) -> list[dict]:
    out = []
    a, b = ref["_monthly"].dropna(), live["_monthly"].dropna()
    welch = None
    if len(a) >= 6 and len(b) >= 6:
        welch = (b.mean() - a.mean()) / math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    th_ic = thresholds.get("rolling_rank_ic_60d_min")
    th_dd = thresholds.get("drawdown_vs_bench_max")
    if welch is not None and welch < -2:
        out.append({"kind": "statistical", "level": "alert", "title": "上线期 rank IC 显著低于参照期",
                    "detail": f"月度 rank IC 均值 {a.mean():.4f} → {b.mean():.4f}，Welch t = {welch:.2f}"})
    if live.get("rank_ic_t") is not None and (ref.get("rank_ic") or 0) * (live.get("rank_ic") or 0) < 0 and abs(live["rank_ic_t"]) > 2:
        flip = abs(ref.get("rank_ic_t") or 0) >= 2
        out.append({"kind": "statistical", "level": "alert", "title": "信号方向显著反转" if flip else "上线期 rank IC 显著与假设方向相反",
                    "detail": f"参照期 rank IC {ref['rank_ic']:.4f}，上线期 {live['rank_ic']:.4f}（t = {live['rank_ic_t']:.2f}）"})
    if th_ic is not None and live.get("rolling_rank_ic_60d_last") is not None and live["rolling_rank_ic_60d_last"] < th_ic:
        out.append({"kind": "statistical", "level": "alert", "title": "滚动 60 日 rank IC 低于获批阈值",
                    "detail": f"最新 {live['rolling_rank_ic_60d_last']:.4f} < 阈值 {th_ic}"})
    if (live.get("excess_net_ann") or 0) < 0:
        dd = live.get("max_drawdown_vs_universe")
        lvl = "alert" if th_dd is not None and dd is not None and dd < th_dd else "watch"
        out.append({"kind": "economic", "level": lvl, "title": "扣成本后超额为负" + ("且回撤超过获批阈值" if lvl == "alert" else ""),
                    "detail": f"前 20% 相对股票池：扣成本前 {live['excess_gross_ann']:+.2%}，扣成本后 {live['excess_net_ann']:+.2%}/年；相对回撤 {dd:.1%}"
                              + (f"（阈值 {th_dd:.0%}）" if th_dd is not None else "")})
    if ref.get("crowding") and live.get("crowding") and live["crowding"] > ref["crowding"] * 1.3:
        out.append({"kind": "crowding", "level": "watch", "title": "拥挤度上升",
                    "detail": f"前 20% 股票之间的收益相关（相对全体）{ref['crowding']:.2f} → {live['crowding']:.2f}"})
    if ref.get("turnover_monthly") and live.get("turnover_monthly") and live["turnover_monthly"] > ref["turnover_monthly"] * 1.5:
        out.append({"kind": "turnover", "level": "watch", "title": "换手率上升",
                    "detail": f"月度单边换手 {ref['turnover_monthly']:.0%} → {live['turnover_monthly']:.0%}"})
    drift = {k: (live["exposure"].get(k) or 0) - (ref["exposure"].get(k) or 0) for k in live.get("exposure", {})}
    big = {k: v for k, v in drift.items() if abs(v) > 0.5}
    if big:
        out.append({"kind": "exposure", "level": "watch", "title": "风格暴露漂移",
                    "detail": "；".join(f"{k} {ref['exposure'][k]:+.2f} → {live['exposure'][k]:+.2f}" for k in big)})
    if psi is not None and psi > 0.25:
        out.append({"kind": "data_drift", "level": "watch", "title": "信号分布漂移", "detail": f"PSI = {psi:.2f}（> 0.25）"})
    if missing_live > 0.2:
        out.append({"kind": "data_drift", "level": "watch", "title": "因子缺失率偏高", "detail": f"上线期缺失 {missing_live:.0%}"})
    return out


def diagnose(ref: dict, live: dict, decay_ref: dict, decay_live: dict) -> list[str]:
    """可解释诊断：用状态拆分与衰减曲线说明变化来自哪里。"""
    out = []
    rg_r, rg_l = ref["regime"], live["regime"]
    if rg_l.get("high_vol") is not None and rg_l.get("low_vol") is not None:
        worse = "高波动期" if (rg_l["high_vol"] or 0) < (rg_l["low_vol"] or 0) else "低波动期"
        out.append(f"上线期 rank IC：高波动期 {rg_l['high_vol']:+.4f}、低波动期 {rg_l['low_vol']:+.4f}（参照期 {rg_r['high_vol']:+.4f} / {rg_r['low_vol']:+.4f}），{worse}表现较弱")
    if rg_l.get("up_months") is not None and rg_l.get("down_months") is not None:
        out.append(f"指数上涨月份 {rg_l['up_months']:+.4f}、下跌月份 {rg_l['down_months']:+.4f}")
    best_r = max(decay_ref, key=lambda k: decay_ref[k] or -9)
    best_l = max(decay_live, key=lambda k: decay_live[k] or -9)
    out.append(f"rank IC 最强的预测周期：参照期 {best_r} 日、上线期 {best_l} 日" + ("（信号变快或变慢，调仓频率可能需要调整）" if best_r != best_l else ""))
    if live.get("implementation_delay_ann") is not None:
        out.append(f"推迟一天执行使年化超额变化 {live['implementation_delay_ann']:+.2%}；成本拖累 {live['cost_ann']:.2%}/年")
    return out


# ---------------------------------------------------------------- 入口

def run(entry: dict, log=print) -> dict:
    """entry：信号目录中的一个市场版本（signal_id、market、factors、ref 期间、horizon、thresholds）。"""
    m, sid = entry["market"], entry["signal_id"]
    out_dir = SIGNALS / sid
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    px = pd.read_parquet(RUNTIME / "data" / "standardized" / f"{m}.parquet", columns=["symbol", "date", "close", "volume"])
    px["date"] = pd.to_datetime(px["date"])
    close = px.pivot(index="date", columns="symbol", values="close").sort_index()
    vol = px.pivot(index="date", columns="symbol", values="volume").reindex(close.index)
    adv = (close * vol).rolling(ADV_DAYS, min_periods=10).mean()
    ret = close.pct_change(fill_method=None)
    styles = {"size": np.log(adv.where(adv > 0)), "momentum": close.shift(21) / close.shift(252) - 1,
              "volatility": ret.rolling(60, min_periods=40).std(), "reversal": close / close.shift(21) - 1}
    ix = pd.read_parquet(RUNTIME / "data" / "raw" / m / "index.parquet", columns=["date", "close"])
    ix["date"] = pd.to_datetime(ix["date"]).dt.tz_localize(None).dt.normalize()
    idx = ix.set_index("date")["close"].sort_index()
    ref = (entry["ref_start"], entry["ref_end"])
    log(f"{sid}：重算 {len(entry['factors'])} 个因子（冻结代码）")
    score, finfo, raw = build_signal(entry["factors"], m, ref, entry["horizon"], work, close)
    live_start = (pd.Timestamp(ref[1]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    live_end = str(score.dropna(how="all").index.max().date())
    costs = rules.costs(m)
    log(f"{sid}：计算监控指标 参照期 {ref[0]}~{ref[1]}，上线期 {live_start}~{live_end}")
    rs = _period_stats(score, close, adv, styles, idx, ref[0], ref[1], entry["horizon"], costs)
    ls = _period_stats(score, close, adv, styles, idx, live_start, live_end, entry["horizon"], costs)
    dr, dl = _decay(score, close, *ref), _decay(score, close, live_start, live_end)
    psi = _psi(score.loc[ref[0]:ref[1]].to_numpy().ravel(), score.loc[live_start:].to_numpy().ravel())
    miss = float(np.mean([df.loc[live_start:].isna().to_numpy().mean() for df in raw.values()]))
    al = alerts(rs, ls, entry.get("thresholds") or {}, entry["horizon"], psi, miss)
    stat = any(a["kind"] == "statistical" and a["level"] == "alert" for a in al)
    econ = any(a["kind"] == "economic" for a in al)
    suggestion = ("pause" if stat and econ else "watch" if al else "ok")
    roll = ls["_ric"].rolling(60, min_periods=40).mean()
    roll_ref = rs["_ric"].rolling(60, min_periods=40).mean()
    series = pd.concat([roll_ref, roll]).dropna()
    result = {
        "signal_id": sid, "market": m, "computed_at": now(), "horizon": entry["horizon"], "factors": finfo,
        "reference": {k: v for k, v in rs.items() if not k.startswith("_")}, "live": {k: v for k, v in ls.items() if not k.startswith("_")},
        "decay": {"reference": dr, "live": dl}, "data_drift": {"psi": _r(psi, 3), "missing_live": _r(miss, 3)},
        "alerts": al, "suggestion": suggestion,
        "suggestion_zh": {"pause": "建议暂停该市场版本（统计与经济预警同时成立）", "watch": "加强观察", "ok": "正常"}[suggestion],
        "diagnosis": diagnose(rs, ls, dr, dl), "thresholds": entry.get("thresholds") or {},
        "rolling": {"dates": [str(d.date()) for d in series.index[::3]], "values": [_r(v) for v in series.to_numpy()[::3]],
                    "live_start": live_start},
        "note": "上线期为测试期之后的数据（2024 年起），用于演示上线后的监控；信号为冻结因子的等权合成，不含模型重新训练",
    }
    score.stack().rename("score").reset_index().rename(columns={"level_0": "date", "level_1": "symbol"}).to_parquet(out_dir / "score.parquet", index=False)
    tmp = out_dir / "monitor.json.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_dir / "monitor.json")
    return result
