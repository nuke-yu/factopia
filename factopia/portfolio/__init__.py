"""授权组合、多币种回测、汇率拆分与归因（业务需求 BR-03、14、32；Spec PORT-01 至 09、第 14.13 节）。

输入：一个锁定的研究版本（研究规格 → 模拟授权）与各市场采纳轮次的逐股打分（结果导出的 score/<轮次>.parquet）。
- 股票池：成分股名单，剔除授权中的受限证券、授权自由文本要求回避的行业（规则包 GEN-ESG-001 的关键词 → 行业）。
- 调仓：每月最后一个交易日（三市场交易日的并集），用当日或之前最近的打分，次日起生效；只做多。
- 市场之间：调仓时回到复合基准权重（授权 portfolio.benchmark_weights），受市场上限约束。
- 市场之内：打分最高的 N 只等权（N = holdings_per_market），单只上限取规则包 GEN-CONC-001；
  与股票池等权组合按比例 α 混合，α 取满足跟踪误差上限（授权 max_tracking_error，过去 252 日历史模拟）的最大值；
  单次调仓换手超过授权上限时按比例只调整一部分。
- 成本：各市场规则包的单边成本（税费 + 佣金与冲击），调仓当日扣除。
- 记账（逐日、三市场交易日并集；某市场休市时该市场收益为 0）：
  本地收益 r_L、汇率收益 r_F、基础币种收益 r_B =（1 + r_L）×（1 + r_F）− 1（PORT-07，按构造恒等）。
- 归因（PORT-08）：Brinson（以复合基准为参照，基础币种）——配置效应、选股效应（本地币种）、汇率与交互效应；
  逐日三项之和等于当日超额，多日用 Carino 系数连接，使各项之和等于整个区间的几何超额。
- 反馈（BR-32）：按市场的选股效应、信号显著性与成本，建议保留 / 进一步研究 / 限定地区 / 重新校准 / 停用，研究员记录决定。

输出：runtime/portfolio/<研究编号>_v<版本>/（summary.json、daily.parquet、weights.parquet、feedback.json）。
"""
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from factopia import rules, sources
from factopia.rules import context as rctx

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(RUNTIME / "results")))
OUT_DIR = RUNTIME / "portfolio"
LOCAL = {"hk": "HKD", "uk": "GBP", "fr": "EUR"}
INDEX = {"hk": "^HSI", "uk": "^FTSE", "fr": "^FCHI"}
INDEX_ZH = {"hk": "恒生指数", "uk": "富时 100", "fr": "CAC 40"}
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}
LOOKBACK = 252
ALPHAS = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0]
DEFAULTS = {"max_tracking_error": 0.06, "max_turnover": 0.3, "holdings_per_market": 20, "rebalance": "monthly"}


class PortfolioError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 输入

def mandate_terms(mandate: dict, markets: list[str]) -> dict:
    """PORT-01：校验组合层必填条款，缺字段时指出字段名。"""
    pf = mandate.get("portfolio") or {}
    missing = [k for k in ("base_currency",) if not mandate.get(k)] + \
              [f"portfolio.{k}" for k in ("benchmark_weights", "market_limits") if not pf.get(k)]
    if missing:
        raise PortfolioError(f"授权 {mandate.get('id')} 缺少字段：{'、'.join(missing)}")
    bw = {m: float(pf["benchmark_weights"].get(m, 0)) for m in markets}
    if sum(bw.values()) <= 0:
        raise PortfolioError("复合基准权重之和须大于 0")
    tot = sum(bw.values())
    bw = {m: w / tot for m, w in bw.items()}
    limits = {m: float(pf["market_limits"].get(m, 1.0)) for m in markets}
    conc = next((r for p in rules.select(markets) for r in p["rules"] if r["check"]["type"] == "max_weight"), None)
    return {"base_currency": mandate["base_currency"], "report_currency": mandate.get("report_currency") or mandate["base_currency"],
            "benchmark_weights": bw, "market_limits": limits, "long_only": bool(mandate.get("long_only", True)),
            "max_tracking_error": float(pf.get("max_tracking_error", DEFAULTS["max_tracking_error"])),
            "max_turnover": float(pf.get("max_turnover", DEFAULTS["max_turnover"])),
            "holdings_per_market": int(pf.get("holdings_per_market", DEFAULTS["holdings_per_market"])),
            "max_stock_weight": float(conc["check"]["limit"]) if conc else 0.10,
            "max_stock_weight_rule": conc["id"] if conc else None,
            "restricted": list(mandate.get("restricted_securities") or [])}


def _excluded_industries(mandate: dict, markets: list[str]) -> tuple[dict[str, list[str]], str | None]:
    text = " ".join(mandate.get("free_text") or []) + (mandate.get("sustainability") or "")
    for p in rules.select(markets):
        for r in p["rules"]:
            if r["check"]["type"] == "excluded_industries":
                return {k: v for k, v in r["check"]["keywords"].items() if k in text}, r["id"]
    return {}, None


def fx_to_base(base: str) -> pd.DataFrame:
    """各本地币种 → 基础币种的汇率（1 单位本地币种 = x 基础币种），按港元交叉换算。"""
    fx = pd.read_parquet(RUNTIME / "data" / "raw" / "fx.parquet", columns=["date", "symbol", "close"])
    fx["date"] = pd.to_datetime(fx["date"]).dt.tz_localize(None).dt.normalize()
    w = fx.pivot_table(index="date", columns="symbol", values="close").sort_index().ffill()
    to_hkd = {"HKD": pd.Series(1.0, index=w.index), "GBP": w["GBPHKD=X"], "EUR": w["EURHKD=X"]}
    return pd.DataFrame({m: to_hkd[c] / to_hkd[base] for m, c in LOCAL.items()})


def _prices(market: str, dataset: str | None = None) -> pd.DataFrame:
    return sources.close(market, dataset)


def _index(market: str, dataset: str | None = None) -> pd.Series:
    return sources.index(market, dataset)


def _run(run_id) -> dict:
    p = RESULTS / "runs" / str(run_id) / "run.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _scores(run_id: str, loop, direction: int = 1) -> pd.DataFrame:
    """逐股打分；direction = −1（文本信号：打分越低越好）时取反，组合统一按"越大越好"选股。"""
    s = pd.read_parquet(RESULTS / "runs" / run_id / "score" / f"{loop}.parquet")
    s["symbol"] = s["instrument"].map(rctx._symbol)
    s["date"] = pd.to_datetime(s["date"])
    return s.pivot_table(index="date", columns="symbol", values="score").sort_index() * direction


# ---------------------------------------------------------------- 组合构建

def _sleeve_target(score: pd.Series, universe: list[str], hist: pd.DataFrame, bench_hist: pd.Series,
                   t: dict) -> tuple[pd.Series, dict]:
    """一个市场在一次调仓时的目标权重（市场内部，和为 1）与约束记录。"""
    elig = [s for s in universe if s in hist.columns and hist[s].notna().sum() >= LOOKBACK * 0.8]
    sc = score.reindex(elig).dropna()
    info = {"eligible": len(elig), "scored": int(len(sc))}
    eq = pd.Series(1.0 / len(elig), index=elig) if elig else pd.Series(dtype=float)
    if len(sc) == 0:
        info.update(alpha=0.0, note="没有打分：等权持有股票池")
        return eq, info
    n = min(t["holdings_per_market"], len(sc))
    cap = t["max_stock_weight"]
    n = max(n, math.ceil(1 / cap))                      # 等权时单只不超过上限
    top = sc.sort_index().sort_values(ascending=False, kind="mergesort").index[:n]   # 并列时按股票代码，结果可复现
    sig = pd.Series(1.0 / len(top), index=top)
    r = hist[elig].fillna(0.0)
    best = None
    for a in ALPHAS:
        w = (a * sig.reindex(elig).fillna(0.0) + (1 - a) * eq)
        te = float((r @ w - bench_hist.reindex(r.index).fillna(0.0)).std() * math.sqrt(252))
        best = (a, w, te)
        if te <= t["max_tracking_error"]:
            break
    a, w, te = best
    info.update(alpha=a, n_signal=int(len(top)), te_ex_ante=round(te, 4), te_ok=te <= t["max_tracking_error"])
    return w[w > 1e-12], info


def build(spec: dict, study: dict) -> dict:
    """组合构建与回测。study：研究结论（各市场的采纳轮次）。"""
    markets = spec["markets"]
    mandate = rctx.mandate(spec.get("mandate"))
    t = mandate_terms(mandate, markets)
    base = t["base_currency"]
    excl, excl_rule = _excluded_industries(mandate, markets)
    inds = rctx.industries()
    universe = rctx.universe(markets)
    fx = fx_to_base(base)

    px, idx, sig, src = {}, {}, {}, {}
    for m in markets:
        e = (study.get("markets") or {}).get(m) or {}
        run = _run(e.get("run_id"))
        ds = sources.of_run(run)                  # 文本信号研究的模拟市场：模拟行情、虚构公司股票池、等权基准
        px[m] = _prices(m, ds)
        idx[m] = _index(m, ds)
        if ds:
            universe[m] = sources.universe(m, ds)
        f = e.get("focus")
        p = RESULTS / "runs" / str(e.get("run_id")) / "score" / f"{(f or {}).get('loop')}.parquet"
        ps = sources.describe(ds)
        if f and p.exists():
            sig[m] = _scores(e["run_id"], f["loop"], int(run.get("score_direction") or 1))
            src[m] = {"run_id": e["run_id"], "loop": f["loop"], "factors": f.get("factors") or [],
                      "hypothesis": f.get("hypothesis"), "rank_ic_t": (f.get("significance") or {}).get("t"),
                      "kind": run.get("kind", "rdagent"), "price_source": ps}
        else:
            src[m] = {"run_id": e.get("run_id"), "loop": None, "note": "没有采纳轮次的打分：该市场等权持有股票池",
                      "kind": run.get("kind", "rdagent"), "price_source": ps}
    if not sig:
        raise PortfolioError("各市场都还没有采纳轮次的逐股打分：先完成运行并导出结果")

    start = max(s.index.min() for s in sig.values())
    end = max(s.index.max() for s in sig.values())
    cal = sorted(set().union(*[set(px[m].loc[start:end].index) for m in markets]))
    cal = pd.DatetimeIndex(cal)
    # 每月最后一个交易日调仓（最后一个月末之后不再调仓）
    rebal = set(pd.Series(cal, index=cal).groupby(cal.to_period("M")).max())
    rebal.discard(cal[-1])

    # 逐股本地收益（并集日历；休市日收益为 0）
    rets, hist_rets, hist_bench = {}, {}, {}
    for m in markets:
        p = px[m].reindex(px[m].index.union(cal)).sort_index().ffill()
        rets[m] = p.pct_change(fill_method=None).reindex(cal)
        hist_rets[m] = px[m].pct_change(fill_method=None)                     # 本市场交易日，用于事前跟踪误差
        hist_bench[m] = idx[m].pct_change(fill_method=None).reindex(px[m].index).fillna(0.0)
    fxr = fx.reindex(fx.index.union(cal)).sort_index().ffill().pct_change(fill_method=None).reindex(cal).fillna(0.0)
    bl = {m: idx[m].reindex(idx[m].index.union(cal)).sort_index().ffill().pct_change(fill_method=None).reindex(cal).fillna(0.0)
          for m in markets}

    # 剔除受限证券与回避行业
    removed = {}
    for m in markets:
        rs = [s for s in universe[m] if s in t["restricted"]]
        ind = [s for s in universe[m] if any((inds.get(s) or "") in v for v in excl.values())]
        removed[m] = {"restricted": rs, "industry": ind}
        universe[m] = [s for s in universe[m] if s not in rs and s not in ind]

    W = {m: pd.Series(dtype=float) for m in markets}        # 当前持仓（占总组合，基础币种）
    rows, wrows, rebal_log = [], [], []
    started = False
    for i, d in enumerate(cal):
        # 1) 当日收益（使用前一日收盘后的持仓）
        if started:
            rec = {"date": d}
            V0 = sum(W[m].sum() for m in markets)
            gross = 0.0
            for m in markets:
                w = W[m]
                Vm = w.sum()
                rl = rets[m].loc[d].reindex(w.index).fillna(0.0)
                rf = float(fxr.at[d, m])
                g_l = float((w * rl).sum() / Vm) if Vm > 0 else 0.0
                c = pending_cost.get(m, 0.0) / Vm if Vm > 0 else 0.0
                r_l = (1 - c) * (1 + g_l) - 1
                r_b = (1 + r_l) * (1 + rf) - 1
                rec |= {f"{m}_w": Vm / V0, f"{m}_rl": r_l, f"{m}_rf": rf, f"{m}_rb": r_b, f"{m}_cost": c,
                        f"{m}_bl": float(bl[m].at[d]), f"{m}_bb": (1 + float(bl[m].at[d])) * (1 + rf) - 1}
                W[m] = w * (1 - c) * (1 + rl) * (1 + rf)
                gross += Vm * r_b
            rec["port"] = gross / V0
            rec["bench"] = sum(t["benchmark_weights"][m] * rec[f"{m}_bb"] for m in markets)
            rows.append(rec)
            tot = sum(W[m].sum() for m in markets)
            W = {m: W[m] / tot for m in markets}
            pending_cost = {}
        # 2) 调仓：当日收盘按打分生成目标，次日起生效
        if d in rebal:
            pending_cost = {}
            log = {"date": d.strftime("%Y-%m-%d"), "markets": {}}
            drift = {m: W[m].sum() for m in markets}
            for m in markets:
                hist = hist_rets[m].loc[:d].iloc[-LOOKBACK:]
                bh = hist_bench[m].loc[:d].iloc[-LOOKBACK:]
                if m in sig:
                    s_day = sig[m].loc[:d]
                    score = s_day.iloc[-1] if len(s_day) else pd.Series(dtype=float)
                else:
                    score = pd.Series(dtype=float)
                target, info = _sleeve_target(score, universe[m], hist, bh, t)
                Wm = min(t["benchmark_weights"][m], t["market_limits"][m])
                cur = (W[m] / drift[m]) if drift[m] > 0 else pd.Series(dtype=float)
                idx_all = target.index.union(cur.index)
                tg, cu = target.reindex(idx_all).fillna(0.0), cur.reindex(idx_all).fillna(0.0)
                to = float((tg - cu).abs().sum() / 2)
                k = 1.0 if not started or to <= t["max_turnover"] else t["max_turnover"] / to
                new = cu + k * (tg - cu)
                new = new[new > 1e-9]
                new = new / new.sum()
                # 成本（占总组合）：本市场新权重 vs 漂移后权重
                trade = (Wm * new).reindex(idx_all).fillna(0.0) - W[m].reindex(idx_all).fillna(0.0)
                cb = rules.costs(m)
                cost = float(trade.clip(lower=0).sum() * cb["buy"]["total"] + (-trade.clip(upper=0)).sum() * cb["sell"]["total"])
                pending_cost[m] = cost
                W[m] = Wm * new
                info |= {"turnover_target": round(to, 4), "turnover_scale": round(k, 3), "market_weight": round(Wm, 4),
                         "holdings": int(len(new)), "cost": round(cost, 6), "max_weight": round(float(new.max()), 4)}
                log["markets"][m] = info
                wrows += [{"date": d, "market": m, "symbol": s, "weight_in_market": float(v), "weight": float(Wm * v)}
                          for s, v in new.items()]
            tot = sum(W[m].sum() for m in markets)
            W = {m: W[m] / tot for m in markets}
            rebal_log.append(log)
            started = True
    if not rows:
        raise PortfolioError("回测区间内没有可用的交易日")
    daily = pd.DataFrame(rows).set_index("date")
    weights = pd.DataFrame(wrows)
    attr = attribution(daily, markets, t["benchmark_weights"])
    metrics = performance(daily, markets)
    turns = [sum(mk["market_weight"] * mk["turnover_target"] * mk["turnover_scale"] for mk in r["markets"].values()) for r in rebal_log[1:]]
    metrics["avg_turnover"] = float(np.mean(turns)) if turns else None
    constraints = _constraints(t, mandate, excl, excl_rule, removed, rebal_log)
    pit = RESULTS / "pit_status.json"
    summary = {
        "research_id": spec["id"], "version": spec["version"], "mandate": mandate.get("id"), "mandate_name": mandate.get("name"),
        "built_at": now(), "markets": markets, "base_currency": base, "report_currency": t["report_currency"],
        "benchmark": {"weights": t["benchmark_weights"], "indices": {m: INDEX[m] for m in markets},
                      "label": " + ".join(f"{INDEX_ZH[m]} {t['benchmark_weights'][m]:.0%}" for m in markets) + f"（{base}）"},
        "period": {"start": daily.index[0].strftime("%Y-%m-%d"), "end": daily.index[-1].strftime("%Y-%m-%d"),
                   "days": int(len(daily)), "rebalances": len(rebal_log)},
        "terms": t, "signals": src, "constraints": constraints, "rebalances": rebal_log[-3:],
        "metrics": metrics, "fx_split": fx_split(daily, markets), "attribution": attr,
        "costs": {m: rules.costs(m) for m in markets},
        "rule_packs": [{"pack": p["pack"], "version": p["version"]} for p in rules.select(markets)],
        "pit": json.loads(pit.read_text(encoding="utf-8")) if pit.exists() else None,
        "backtest_note": "回测基于 Yahoo Finance 复权收盘价与当前成分股（非时间点一致，见第 14.10 节）；成本按规则包，未模拟冲击随规模变化",
    }
    summary["feedback"] = suggestions(summary)
    out = OUT_DIR / f"{spec['id']}_v{spec['version']}"
    out.mkdir(parents=True, exist_ok=True)
    daily.reset_index().to_parquet(out / "daily.parquet", index=False)
    weights.to_parquet(out / "weights.parquet", index=False)
    tmp = out / "summary.json.tmp"
    tmp.write_text(json.dumps(_clean(summary), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out / "summary.json")
    return summary


def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if math.isnan(x) or math.isinf(x) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.isoformat()
    return x


def _constraints(t, mandate, excl, excl_rule, removed, log) -> list[dict]:
    """PORT-02：每个约束注明来源条款或规则编号，并给出执行情况。"""
    te = [mk["te_ex_ante"] for r in log for mk in r["markets"].values() if mk.get("te_ex_ante") is not None]
    te_bad = sum(1 for r in log for mk in r["markets"].values() if mk.get("te_ok") is False)
    scaled = sum(1 for r in log for mk in r["markets"].values() if mk.get("turnover_scale", 1) < 1)
    mx = max((mk["max_weight"] for r in log for mk in r["markets"].values()), default=None)
    out = [
        {"name": "只做多", "value": "不允许卖空" if t["long_only"] else "允许卖空（本组合仍只做多）",
         "source": "授权 long_only；规则 GEN-SHORT-001", "status": "满足：所有权重 ≥ 0"},
        {"name": "复合基准与市场权重", "value": "、".join(f"{MARKET_ZH[m]} {w:.0%}" for m, w in t["benchmark_weights"].items()),
         "source": "授权 portfolio.benchmark_weights", "status": "每次调仓回到基准权重，期间随行情漂移"},
        {"name": "市场上限", "value": "、".join(f"{MARKET_ZH[m]} ≤ {w:.0%}" for m, w in t["market_limits"].items()),
         "source": "授权 portfolio.market_limits", "status": "调仓时满足"},
        {"name": "单只股票上限（市场内）", "value": f"≤ {t['max_stock_weight']:.0%}", "source": f"规则 {t['max_stock_weight_rule']}",
         "status": f"调仓时最大 {mx:.1%}" if mx is not None else "—"},
        {"name": "跟踪误差上限", "value": f"≤ {t['max_tracking_error']:.0%}（年化，过去 252 日历史模拟，各市场相对本地指数）",
         "source": "授权 portfolio.max_tracking_error（risk_budget）",
         "status": (f"调仓时事前跟踪误差 {min(te):.1%}–{max(te):.1%}" if te else "—") + (f"；{te_bad} 个（市场 × 调仓）即使完全等权持有股票池仍超出——股票池等权组合本身相对指数的偏离已超过上限" if te_bad else "")},
        {"name": "单次调仓换手上限", "value": f"≤ {t['max_turnover']:.0%}（单边，市场内）", "source": "授权 portfolio.max_turnover",
         "status": f"{scaled} 次调仓按比例只调整了一部分" if scaled else "均未触及"},
        {"name": "受限证券", "value": "、".join(t["restricted"]) or "无", "source": "授权 restricted_securities；规则 GEN-RESTRICT-001",
         "status": "已从股票池剔除：" + ("、".join(s for v in removed.values() for s in v["restricted"]) or "无")},
        {"name": "回避行业", "value": "、".join(excl) or "无", "source": f"授权自由文本；规则 {excl_rule}" if excl else "授权自由文本",
         "status": "已从股票池剔除：" + ("、".join(s for v in removed.values() for s in v["industry"]) or "无") + "（行业分类近似匹配，须人工确认）"},
        {"name": "交易成本与税费", "value": "按市场规则包 costs", "source": "规则包 HK-TAX-001 / UK-TAX-001 / FR-TAX-001（RULE-04）",
         "status": "调仓当日扣除"},
    ]
    return out


# ---------------------------------------------------------------- 业绩、汇率拆分、归因

def _ann(r: pd.Series) -> float:
    years = max((r.index[-1] - r.index[0]).days / 365.25, 1 / 365.25)
    return float((1 + r).prod() ** (1 / years) - 1)


def _ppy(r: pd.Series) -> float:
    years = max((r.index[-1] - r.index[0]).days / 365.25, 1 / 365.25)
    return len(r) / years


def _mdd(r: pd.Series) -> float:
    nav = (1 + r).cumprod()
    return float((nav / nav.cummax() - 1).min())


def _stats(r: pd.Series, b: pd.Series | None) -> dict:
    ppy = _ppy(r)
    vol = float(r.std() * math.sqrt(ppy))
    out = {"return_ann": _ann(r), "return_total": float((1 + r).prod() - 1), "vol": vol,
           "sharpe": _ann(r) / vol if vol > 0 else None, "max_drawdown": _mdd(r)}
    if b is not None:
        a = r - b
        te = float(a.std() * math.sqrt(ppy))
        ex = _ann(r) - _ann(b)
        out |= {"bench_return_ann": _ann(b), "excess_ann": ex, "tracking_error": te, "info_ratio": ex / te if te > 0 else None}
    return out


def performance(daily: pd.DataFrame, markets: list[str]) -> dict:
    """PORT-09：全组合（基础币种，相对复合基准）+ 每个市场（本地币种与基础币种，相对本地指数）。"""
    out = {"total": _stats(daily["port"], daily["bench"]), "markets": {}}
    for m in markets:
        out["markets"][m] = {"local": _stats(daily[f"{m}_rl"], daily[f"{m}_bl"]), "base": _stats(daily[f"{m}_rb"], daily[f"{m}_bb"]),
                             "avg_weight": float(daily[f"{m}_w"].mean()),
                             "cost_ann": float(daily[f"{m}_cost"].sum() / ((daily.index[-1] - daily.index[0]).days / 365.25))}
    return out


def fx_split(daily: pd.DataFrame, markets: list[str]) -> dict:
    """BR-14 / PORT-06、07：各市场区间累计的本地收益、汇率收益、基础币种收益，以及逐日恒等误差。"""
    out = {}
    for m in markets:
        rl, rf, rb = daily[f"{m}_rl"], daily[f"{m}_rf"], daily[f"{m}_rb"]
        err = float(((1 + rl) * (1 + rf) - 1 - rb).abs().max())
        L, F, B = (1 + rl).prod() - 1, (1 + rf).prod() - 1, (1 + rb).prod() - 1
        out[m] = {"local": float(L), "fx": float(F), "base": float(B), "cross": float(B - L - F), "identity_max_error": err,
                  "local_ann": _ann(rl), "fx_ann": _ann(rf), "base_ann": _ann(rb)}
    return out


def _carino(r: pd.Series, b: pd.Series) -> tuple[pd.Series, float]:
    """Carino 连接系数：逐日贡献 × k_t / K 之和等于区间几何超额（R − B）。"""
    def k(x, y):
        return np.where(np.abs(x - y) > 1e-12, (np.log1p(x) - np.log1p(y)) / np.where(np.abs(x - y) > 1e-12, x - y, 1), 1 / (1 + x))
    R, B = float((1 + r).prod() - 1), float((1 + b).prod() - 1)
    K = float(k(np.array([R]), np.array([B]))[0])
    return pd.Series(k(r.to_numpy(), b.to_numpy()), index=r.index) / K, R - B


def attribution(daily: pd.DataFrame, markets: list[str], bw: dict[str, float]) -> dict:
    """PORT-08：Brinson（基础币种，复合基准为参照）。
    配置 =（w_p − w_b）×（R_b,m − R_b）；选股 = w_p ×（r_p,m 本地 − r_b,m 本地）；
    汇率与交互 = w_p × [（R_p,m − R_b,m）−（r_p,m 本地 − r_b,m 本地）]；三项逐日之和恒等于当日超额。"""
    wb = {m: pd.Series(bw[m], index=daily.index) for m in markets}
    rb_tot = daily["bench"]
    comp = {}
    for m in markets:
        wp = daily[f"{m}_w"]
        alloc = (wp - wb[m]) * (daily[f"{m}_bb"] - rb_tot)
        sel = wp * (daily[f"{m}_rl"] - daily[f"{m}_bl"])
        fxi = wp * ((daily[f"{m}_rb"] - daily[f"{m}_bb"]) - (daily[f"{m}_rl"] - daily[f"{m}_bl"]))
        comp[m] = {"allocation": alloc, "selection": sel, "fx_interaction": fxi}
    excess_daily = daily["port"] - daily["bench"]
    resid = excess_daily - sum(v for c in comp.values() for v in c.values())
    k, total = _carino(daily["port"], daily["bench"])
    out = {"total_excess": total, "method": "Brinson（基础币种，复合基准）+ Carino 连接", "markets": {},
           "daily_identity_max_error": float(resid.abs().max())}
    s = 0.0
    for m, c in comp.items():
        out["markets"][m] = {name: float((v * k).sum()) for name, v in c.items()}
        s += sum(out["markets"][m].values())
    out["sum_of_effects"] = s
    out["linking_error"] = s - total
    out["by_effect"] = {e: sum(out["markets"][m][e] for m in markets) for e in ("allocation", "selection", "fx_interaction")}
    # 组合的汇率贡献（绝对，不相对基准）：w_p × 汇率收益 ×（1 + 本地收益）
    kp = _link_abs(daily["port"])
    out["fx_contribution_abs"] = {m: float((daily[f"{m}_w"] * daily[f"{m}_rf"] * (1 + daily[f"{m}_rl"]) * kp).sum()) for m in markets}
    out["local_contribution_abs"] = {m: float((daily[f"{m}_w"] * daily[f"{m}_rl"] * kp).sum()) for m in markets}
    return out


def _link_abs(r: pd.Series) -> pd.Series:
    """绝对收益的连接系数（对照为 0 收益的 Carino）：Σ 贡献 × k_t = 区间总收益。"""
    R = float((1 + r).prod() - 1)
    K = math.log1p(R) / R if abs(R) > 1e-12 else 1.0
    kt = np.where(np.abs(r) > 1e-12, np.log1p(r) / np.where(np.abs(r) > 1e-12, r, 1), 1.0)
    return pd.Series(kt, index=r.index) / K


# ---------------------------------------------------------------- 反馈闭环

ACTIONS = {"keep": "保留", "research": "进一步研究", "restrict": "限定地区（暂停该市场）", "recalibrate": "重新校准", "retire": "停用"}


def suggestions(summary: dict) -> list[dict]:
    """BR-32：按市场给出建议（平台建议，研究员决定）。"""
    attr, met = summary["attribution"]["markets"], summary["metrics"]["markets"]
    sel_pos = {m: a["selection"] > 0 for m, a in attr.items()}
    out = []
    for m, a in attr.items():
        sig = summary["signals"].get(m) or {}
        t = sig.get("rank_ic_t")
        cost = met[m]["cost_ann"] * met[m]["avg_weight"]
        why = [f"选股效应 {a['selection']:+.2%}", f"汇率与交互 {a['fx_interaction']:+.2%}", f"成本拖累约 {cost:.2%}/年（占总组合）"]
        if t is not None:
            why.append(f"信号 rank IC t = {t:.1f}")
        if sig.get("loop") is None:
            act = "research"; why.append("该市场没有采纳轮次")
        elif a["selection"] > 0 and t is not None and abs(t) >= 2:
            act = "keep"
        elif a["selection"] > 0:
            act = "research"; why.append("选股为正但信号不显著")
        elif any(v for k, v in sel_pos.items() if k != m):
            act = "restrict"; why.append("其他市场选股为正，本市场为负")
        elif a["selection"] + cost > 0:
            act = "recalibrate"; why.append("扣成本前为正：降低换手可能改善")
        else:
            act = "retire"; why.append("所有市场选股均为负")
        out.append({"market": m, "action": act, "action_zh": ACTIONS[act], "reasons": why,
                    "signal": {k: sig.get(k) for k in ("run_id", "loop")}})
    return out


def out_dir(research_id: str, version: int) -> Path:
    return OUT_DIR / f"{research_id}_v{version}"


def load(research_id: str, version: int) -> dict | None:
    p = out_dir(research_id, version) / "summary.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def record_feedback(research_id: str, version: int, entry: dict) -> list[dict]:
    p = out_dir(research_id, version) / "feedback.json"
    items = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    items.append({**entry, "at": now()})
    p.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


def feedback(research_id: str, version: int) -> list[dict]:
    p = out_dir(research_id, version) / "feedback.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
