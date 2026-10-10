"""互补组合（业务需求 BR-28；Spec 第 14.19 节）：同一市场多个信号的冗余与互补，以及合成信号是否优于单个信号。

候选：该市场各研究版本最近一次完成运行的采纳轮次（逐股打分 score/<轮次>.parquet，覆盖测试期）；
只合成同一行情来源的信号（模拟数据与真实数据不混合）；打分按运行的 score_direction 统一为"越大越好"。

1. 每个信号：逐日截面标准化，逐日 rank IC（未来 N 日收益，次日起，与 Qlib 标签同口径）；
2. 冗余：两两之间打分的平均截面相关、逐日 rank IC 序列的相关；
3. 合成：权重 ∝ Σ⁻¹μ（μ、Σ 为逐日 rank IC 的均值与协方差，负权重截为 0；Σ 加收缩避免奇异），
   **权重只用测试期前半段估计，在后半段评估**（避免同一段数据既定权重又评估）；同时给出等权合成作对照；
4. 结论：后半段合成信号的 rank IC 与 IR 是否高于其中最好的单个信号（样本外）。
"""
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from factopia import sources
from factopia.factors.stats import newey_west_t

RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results"))
SHRINK = 0.2
MIN_STOCKS = 10


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), nd)


def _symbol(code: str) -> str:
    head, _, tail = code.rpartition("_")
    return f"{head}.{tail}" if head else code


def candidates(market: str, study_fn) -> list[dict]:
    """study_fn(research_id, version) → 研究结论；每个研究版本取该市场的采纳轮次。"""
    from factopia.research import store
    out = []
    for rid in store.research_ids():
        for v in store.versions(rid):
            try:
                st = study_fn(rid, v)
            except Exception:
                continue
            e = (st.get("markets") or {}).get(market) or {}
            f = e.get("focus")
            if not f or not (RESULTS / "runs" / str(e["run_id"]) / "score" / f"{f['loop']}.parquet").exists():
                continue
            run = json.loads((RESULTS / "runs" / e["run_id"] / "run.json").read_text(encoding="utf-8"))
            out.append({"key": f"{rid}.v{v}", "research_id": rid, "version": v, "title": st.get("title"), "run_id": e["run_id"],
                        "loop": f["loop"], "factors": f.get("factors") or [], "kind": run.get("kind", "rdagent"),
                        "direction": int(run.get("score_direction") or 1), "dataset": sources.of_run(run),
                        "horizon": run["horizon"], "credibility": (f.get("credibility") or {}).get("label")})
    return out


def _score(c: dict) -> pd.DataFrame:
    s = pd.read_parquet(RESULTS / "runs" / c["run_id"] / "score" / f"{c['loop']}.parquet")
    s["symbol"] = s["instrument"].map(_symbol)
    s["date"] = pd.to_datetime(s["date"])
    w = s.pivot_table(index="date", columns="symbol", values="score").sort_index() * c["direction"]
    return w.sub(w.mean(axis=1), axis=0).div(w.std(axis=1).replace(0, np.nan), axis=0)


def _ric(score: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    f = fwd.reindex_like(score)
    ok = score.notna() & f.notna()
    a, b = score.where(ok).rank(axis=1), f.where(ok).rank(axis=1)
    a, b = a.sub(a.mean(axis=1), axis=0), b.sub(b.mean(axis=1), axis=0)
    return ((a * b).sum(axis=1) / np.sqrt((a ** 2).sum(axis=1) * (b ** 2).sum(axis=1))).where(ok.sum(axis=1) >= MIN_STOCKS).dropna()


def _stats(ric: pd.Series, h: int) -> dict:
    return {"rank_ic": _r(ric.mean()), "ir": _r(ric.mean() / ric.std() * math.sqrt(252)) if len(ric) > 2 and ric.std() > 0 else None,
            "t": _r(newey_west_t(ric, h), 2) if len(ric) > h + 2 else None, "days": int(len(ric))}


def analyse(market: str, cands: list[dict], keys: list[str] | None = None) -> dict:
    cs = [c for c in cands if not keys or c["key"] in keys]
    groups = {}
    for c in cs:
        groups.setdefault(c["dataset"], []).append(c)
    ds, cs = max(groups.items(), key=lambda kv: len(kv[1])) if groups else (None, [])
    for c in cands:
        c["excluded"] = ("行情来源不同（模拟数据与真实数据不合成）" if c["dataset"] != ds else None)
    if len(cs) < 2:
        return {"market": market, "ok": False, "reason": "该市场同一行情来源的候选信号不足 2 个", "candidates": cands}
    h = cs[0]["horizon"]
    close = sources.close(market, ds)
    fwd = close.shift(-(h + 1)) / close.shift(-1) - 1
    scores = {c["key"]: _score(c) for c in cs}
    common = sorted(set.intersection(*[set(s.index) for s in scores.values()]))
    if len(common) < 60:
        return {"market": market, "ok": False, "reason": f"候选信号的共同日期只有 {len(common)} 天", "candidates": cands}
    idx = pd.DatetimeIndex(common)
    scores = {k: s.reindex(idx) for k, s in scores.items()}
    rics = pd.DataFrame({k: _ric(s, fwd) for k, s in scores.items()}).dropna()
    mid = rics.index[len(rics) // 2]
    first, second = rics.loc[:mid].iloc[:-1], rics.loc[mid:]
    ks = list(scores)
    # 权重：前半段估计
    mu = first.mean().to_numpy()
    cov = first.cov().to_numpy()
    cov = (1 - SHRINK) * cov + SHRINK * np.diag(np.diag(cov))
    try:
        w = np.linalg.solve(cov, mu)
    except np.linalg.LinAlgError:
        w = mu.copy()
    w = np.clip(w, 0, None)
    w = w / w.sum() if w.sum() > 0 else np.ones(len(ks)) / len(ks)
    weights = dict(zip(ks, w))

    def combo(ws: dict) -> pd.DataFrame:
        parts = [scores[k] * ws[k] for k in ks if ws[k] > 0]
        return sum(parts) / sum(ws[k] for k in ks if ws[k] > 0)

    eq = {k: 1 / len(ks) for k in ks}
    r_opt, r_eq = _ric(combo(weights), fwd), _ric(combo(eq), fwd)
    second_idx = second.index
    singles = {k: {"full": _stats(rics[k], h), "second": _stats(rics[k].loc[second_idx], h), "first": _stats(rics[k].loc[first.index], h)} for k in ks}
    best = max(ks, key=lambda k: singles[k]["second"]["ir"] or -9)
    opt_s, eq_s = _stats(r_opt.reindex(second_idx).dropna(), h), _stats(r_eq.reindex(second_idx).dropna(), h)
    corr_ic = rics.corr()
    corr_score = {f"{a}|{b}": _r(np.nanmean([scores[a].loc[d].corr(scores[b].loc[d]) for d in idx[::5]]), 3)
                  for i, a in enumerate(ks) for b in ks[i + 1:]}
    better = (opt_s["ir"] or -9) > (singles[best]["second"]["ir"] or -9)
    return {
        "market": market, "ok": True, "dataset": ds, "price_source": sources.describe(ds), "horizon": h,
        "period": {"start": str(rics.index.min().date()), "end": str(rics.index.max().date()),
                   "fit": [str(first.index.min().date()), str(first.index.max().date())],
                   "eval": [str(second_idx.min().date()), str(second_idx.max().date())]},
        "signals": [{**{k: c[k] for k in ("key", "title", "run_id", "loop", "factors", "kind", "credibility")},
                     "weight": _r(weights[c["key"]], 3), **singles[c["key"]]} for c in cs],
        "ic_corr": {a: {b: _r(corr_ic.loc[a, b], 3) for b in ks} for a in ks}, "score_corr": corr_score,
        "combined": {"optimal": {"second": opt_s, "full": _stats(r_opt, h)}, "equal": {"second": eq_s, "full": _stats(r_eq, h)}},
        "best_single": best,
        "verdict": ("互补：后半段（样本外）合成信号的 IR 高于最好的单个信号" if better
                    else "不互补：后半段合成信号没有超过最好的单个信号（信号高度相关或其余信号无增益）"),
        "note": "权重 ∝ Σ⁻¹μ（逐日 rank IC 的均值与协方差，收缩 20%，负权重截为 0），只用前半段估计、后半段评估；"
                "候选为各研究版本的采纳轮次，统计期为其测试期（2022–2023）",
        "candidates": cands,
    }
