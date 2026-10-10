"""研究结果诊断（业务需求 BR-04、05、26、27、28；Spec 第 14.11 节）。

对每次运行的每一轮（含起点），只读已导出的结果（backtest / positions / score）与标准化行情、汇率、证券主数据，不重跑回测：

| 项目 | 内容 |
|---|---|
| 交易成本 | 年化成本拖累、其中税费（印花税 / 金融交易税）、成本 0–3 倍时的扣成本超额、盈亏平衡成本倍数 |
| 换手率 | 日均换手、年化换手、平均持有天数 |
| 流动性 | 持仓的 20 日平均成交额（加权）、落在全市场成交额最低 20% 股票的权重 |
| 容量 | 每笔调仓不超过该股 20 日平均成交额的 10% 时可管理的资金规模（本地币种与基础币种） |
| 汇率影响 | 组合与基准换算为基础币种后的收益，汇率贡献 |
| 风险暴露 | 对基准的 beta；持仓的规模（成交额代理）、动量、波动、短期反转暴露；行业权重与集中度 |
| 中性化测试 | 每日把打分对行业与风格做横截面回归，残差的 Rank IC 与原始 Rank IC 对比 |
| 定义稳定性 | 分年度 Rank IC 与方向一致比例、不同持有期（1–40 日）的 IC 衰减 |
| 市场状态 | 高 / 低波动、上涨 / 下跌市场中的 Rank IC |
| 冗余 | 与起点模型打分、与各风格因子的平均横截面相关 |
| 经济意义（第四类） | 扣成本、扣汇率后是否仍有正超额；容量是否低于门槛 |

结果写入 results/runs/<运行>/diagnostics/<轮次>.json。
"""
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from factopia import rules
from factopia.factors.stats import newey_west_t

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results"))
N = 238                       # 年化交易日（与 Qlib 一致）
ADV_DAYS = 20
PARTICIPATION = 0.10          # 每笔调仓不超过日均成交额的比例
MIN_TRADE = 0.005             # 权重变化小于此值视为未交易
CAPACITY_FLOOR = 50e6         # 基础币种容量低于 5000 万视为"容量不足"
BASE_CURRENCY = os.environ.get("FACTOPIA_BASE_CURRENCY", "HKD")   # 第 0.4 节第 6 步改为按授权配置
LOCAL = {"hk": "HKD", "uk": "GBP", "fr": "EUR"}
# 单边成本与其中的税费：(开仓, 平仓)，来自市场规则包的 costs（RULE-04），与 RD-Agent 回测配置一致（open_cost / close_cost）
_COSTS = {m: rules.costs(m) for m in LOCAL}
TAX = {m: (c["buy"]["tax"], c["sell"]["tax"]) for m, c in _COSTS.items()}
COST = {m: (round(c["buy"]["total"], 6), round(c["sell"]["total"], 6)) for m, c in _COSTS.items()}
HORIZONS = (1, 5, 10, 20, 40)
# 回测并列顺序修正（第 14.8 节）生效时间；之前的运行，原始回测收益受随机选股影响
TIE_FIX_AT = "2026-10-10T10:22:48+00:00"


def _r(x, nd=6):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), nd)


def symbol(code: str) -> str:
    head, _, tail = code.rpartition("_")
    return f"{head}.{tail}" if head else code


# ---------------------------------------------------------------- 市场数据

def market_data(market: str) -> dict:
    px = pd.read_parquet(RUNTIME / "data" / "standardized" / f"{market}.parquet",
                         columns=["symbol", "date", "close", "volume", "dq_flag"])
    close = px.pivot(index="date", columns="symbol", values="close").sort_index()
    volume = px.pivot(index="date", columns="symbol", values="volume").reindex(close.index)
    ret = close.pct_change(fill_method=None)
    adv = (close * volume).rolling(ADV_DAYS, min_periods=10).mean()
    styles = {
        "size": np.log(adv.where(adv > 0)),                              # 规模代理：成交额（无市值数据）
        "momentum": close.shift(21) / close.shift(252) - 1,
        "volatility": ret.rolling(60, min_periods=40).std(),
        "reversal": close / close.shift(21) - 1,
    }
    sectors = {}
    sec = RUNTIME / "refdata" / "securities.parquet"
    if sec.exists():
        s = pd.read_parquet(sec, columns=["symbol", "sector"])
        sectors = dict(zip(s["symbol"], s["sector"]))
    return {"close": close, "ret": ret, "adv": adv, "styles": styles, "sectors": sectors}


def fx_rate(local: str, base: str) -> pd.Series | None:
    """1 单位本地币种 = x 基础币种（按港元交叉换算）。"""
    if local == base:
        return None
    fx = pd.read_parquet(RUNTIME / "data" / "raw" / "fx.parquet", columns=["date", "symbol", "close"])
    fx["date"] = pd.to_datetime(fx["date"]).dt.tz_localize(None).dt.normalize()
    w = fx.pivot_table(index="date", columns="symbol", values="close").sort_index().ffill()
    to_hkd = {"HKD": pd.Series(1.0, index=w.index), "GBP": w.get("GBPHKD=X"), "EUR": w.get("EURHKD=X")}
    if to_hkd.get(local) is None or to_hkd.get(base) is None:
        return None
    return (to_hkd[local] / to_hkd[base]).dropna()


def _z(df: pd.DataFrame) -> pd.DataFrame:
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


# ---------------------------------------------------------------- 各项诊断

def cost_turnover(bt: pd.DataFrame, market: str) -> dict:
    gross = (bt["return"] - bt["bench"]).mean() * N
    drag = bt["cost"].mean() * N
    o, c = COST[market]
    to, tc = TAX[market]
    tax_share = (to + tc) / (o + c)
    multiples = {str(k): _r(gross - k * drag) for k in (0, 1, 2, 3)}
    turn = bt["turnover"].iloc[1:].mean()        # 第一天为建仓
    return {
        "gross_excess": _r(gross), "net_excess": _r(gross - drag), "cost_drag": _r(drag),
        "tax_drag": _r(drag * tax_share), "tax_share": _r(tax_share, 3), "cost_rates": {"open": o, "close": c},
        "excess_by_cost_multiple": multiples, "breakeven_cost_multiple": _r(gross / drag, 2) if drag > 0 else None,
        "turnover_daily": _r(turn), "turnover_annual": _r(turn * N, 2),
        "holding_days": _r(2 / turn, 1) if turn > 0 else None,
    }


def liquidity_capacity(pos: pd.DataFrame, md: dict, fx: pd.Series | None) -> dict:
    adv = md["adv"]
    w = pos.assign(symbol=pos["instrument"].map(symbol)).pivot_table(index="date", columns="symbol", values="weight").fillna(0)
    w = w.reindex(columns=[c for c in w.columns if c in adv.columns])
    a = adv.reindex(index=w.index, columns=w.columns)
    held = w > 0
    wadv = (w * a).sum(axis=1) / w.sum(axis=1).replace(0, np.nan)
    low = adv.rank(axis=1, pct=True).reindex(index=w.index, columns=w.columns) <= 0.2
    low_weight = (w * low).sum(axis=1)
    dw = w.diff().abs()
    dw.iloc[0] = w.iloc[0]
    cap = (PARTICIPATION * a / dw.where(dw > MIN_TRADE)).min(axis=1).dropna()
    cap_local = float(cap.median()) if len(cap) else None
    rate = float(fx.reindex(cap.index).ffill().median()) if fx is not None and len(cap) else 1.0
    return {
        "holdings_adv_weighted": _r(wadv.median(), 0), "low_liquidity_weight": _r(low_weight.mean(), 4),
        "avg_holdings": _r(held.sum(axis=1).mean(), 1),
        "capacity_local": _r(cap_local, 0), "capacity_base": _r(cap_local * rate if cap_local else None, 0),
        "capacity_rule": f"每笔调仓 ≤ 该股 {ADV_DAYS} 日平均成交额的 {PARTICIPATION:.0%}，取每日最紧约束的中位数",
    }


def fx_impact(bt: pd.DataFrame, fx: pd.Series | None, local: str, base: str) -> dict:
    if fx is None:
        return {"base": base, "local": local, "same_currency": True, "fx_contribution": 0.0}
    f = fx.reindex(pd.to_datetime(bt["date"])).ffill().pct_change().fillna(0).to_numpy()
    rp = bt["return"].to_numpy() - bt["cost"].to_numpy()
    rb = bt["bench"].to_numpy()
    rp_base, rb_base = (1 + rp) * (1 + f) - 1, (1 + rb) * (1 + f) - 1
    return {"base": base, "local": local, "same_currency": False,
            "portfolio_local": _r(rp.mean() * N), "portfolio_base": _r(rp_base.mean() * N),
            "fx_contribution": _r((rp_base - rp).mean() * N), "fx_return": _r(f.mean() * N),
            "excess_local": _r((rp - rb).mean() * N), "excess_base": _r((rp_base - rb_base).mean() * N)}


def exposures(bt: pd.DataFrame, pos: pd.DataFrame, md: dict) -> dict:
    x, y = bt["bench"].to_numpy(), (bt["return"] - bt["cost"]).to_numpy()
    beta = float(np.polyfit(x, y, 1)[0]) if len(x) > 20 else None
    w = pos.assign(symbol=pos["instrument"].map(symbol)).pivot_table(index="date", columns="symbol", values="weight").fillna(0)
    style = {}
    for k, df in md["styles"].items():
        z = _z(df).reindex(index=w.index, columns=w.columns)
        style[k] = _r(((w * z).sum(axis=1) / w.sum(axis=1).replace(0, np.nan)).mean(), 3)
    sector_w = {}
    if md["sectors"]:
        s = pd.Series(md["sectors"])
        avg = w.mean()
        for sym, v in avg.items():
            sector_w[s.get(sym) or "未知"] = sector_w.get(s.get(sym) or "未知", 0) + float(v)
        tot = sum(sector_w.values()) or 1
        sector_w = {k: round(v / tot, 4) for k, v in sorted(sector_w.items(), key=lambda kv: -kv[1])}
        if sector_w.get("未知", 0) > 0.5:      # 行业数据缺失过半：不报告行业权重，避免误读为行业集中
            sector_w = {}
    return {"beta": _r(beta, 3), "style": style, "sector_weights": sector_w,
            "max_sector_weight": max(sector_w.values()) if sector_w else None,
            "sector_hhi": _r(sum(v * v for v in sector_w.values()), 4) if sector_w else None,
            "style_note": "风格暴露为持仓相对全市场的标准分（0 = 与市场平均相同）；规模用成交额代理"}


def _rank_ic(score: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    s, f = score.rank(axis=1), fwd.reindex_like(score).rank(axis=1)
    ok = score.notna() & fwd.reindex_like(score).notna()
    s, f = s.where(ok), f.where(ok)
    sc, fc = s.sub(s.mean(axis=1), axis=0), f.sub(f.mean(axis=1), axis=0)
    den = np.sqrt((sc ** 2).sum(axis=1) * (fc ** 2).sum(axis=1))
    return ((sc * fc).sum(axis=1) / den).where(ok.sum(axis=1) >= 10).dropna()


def signal_diagnostics(score: pd.DataFrame, base_score: pd.DataFrame | None, md: dict, horizon: int,
                       bench: pd.Series) -> dict:
    close = md["close"].reindex(columns=score.columns)
    fwd = {h: (close.shift(-(h + 1)) / close.shift(-1) - 1).reindex(score.index) for h in HORIZONS}
    ric = _rank_ic(score, fwd[horizon])
    # 中性化：每日对行业哑变量与风格标准分回归，取残差
    zs = {k: _z(v.reindex(columns=score.columns)).reindex(score.index) for k, v in md["styles"].items()}
    sectors = pd.Series({c: md["sectors"].get(c) for c in score.columns})
    if sectors.notna().mean() < 0.5:          # 行业数据缺失过半时不做行业中性化
        sectors = pd.Series({c: None for c in score.columns})
    resid = pd.DataFrame(index=score.index, columns=score.columns, dtype=float)
    for d in score.index:
        y = score.loc[d]
        X = pd.DataFrame({k: z.loc[d] for k, z in zs.items()})
        if sectors.notna().any():
            X = X.join(pd.get_dummies(sectors, dtype=float))
        ok = y.notna() & X.notna().all(axis=1)
        if ok.sum() < X.shape[1] + 5:
            continue
        A = np.column_stack([np.ones(ok.sum()), X[ok].to_numpy()])
        beta, *_ = np.linalg.lstsq(A, y[ok].to_numpy(), rcond=None)
        resid.loc[d, ok[ok].index] = y[ok].to_numpy() - A @ beta
    ric_n = _rank_ic(resid.dropna(how="all"), fwd[horizon])
    by_year = ric.groupby(ric.index.year).mean()
    sign = np.sign(ric.mean()) if len(ric) else 0
    vol = bench.rolling(60, min_periods=40).std()
    trend = (1 + bench).rolling(60, min_periods=40).apply(np.prod, raw=True) - 1
    reg = pd.DataFrame({"ic": ric, "vol": vol.reindex(ric.index), "trend": trend.reindex(ric.index)}).dropna()
    hi = reg["vol"] > reg["vol"].median()
    redundancy = {}
    if base_score is not None:
        redundancy["baseline_model"] = _r(_cs_corr(score, base_score.reindex_like(score)).mean(), 3)
    for k, z in zs.items():
        redundancy[k] = _r(_cs_corr(score, z).mean(), 3)
    return {
        "rank_ic": _r(ric.mean(), 4), "rank_ic_t": _r(newey_west_t(ric, horizon), 2),
        "neutral_rank_ic": _r(ric_n.mean(), 4), "neutral_rank_ic_t": _r(newey_west_t(ric_n, horizon), 2),
        "neutral_retained": _r(ric_n.mean() / ric.mean(), 2) if len(ric) and ric.mean() else None,
        "neutral_controls": list(zs) + (["行业"] if sectors.notna().any() else []),
        "ic_by_year": {str(y): _r(v, 4) for y, v in by_year.items()},
        "year_sign_consistency": _r((np.sign(by_year) == sign).mean(), 2) if len(by_year) else None,
        "ic_by_horizon": {str(h): _r(_rank_ic(score, fwd[h]).mean(), 4) for h in HORIZONS},
        "regime": {"high_vol": _r(reg.loc[hi, "ic"].mean(), 4), "low_vol": _r(reg.loc[~hi, "ic"].mean(), 4),
                   "up_market": _r(reg.loc[reg["trend"] > 0, "ic"].mean(), 4),
                   "down_market": _r(reg.loc[reg["trend"] <= 0, "ic"].mean(), 4)},
        "redundancy": redundancy,
    }


def _cs_corr(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    ra, rb = a.rank(axis=1), b.reindex_like(a).rank(axis=1)
    ok = ra.notna() & rb.notna()
    ra, rb = ra.where(ok), rb.where(ok)
    ac, bc = ra.sub(ra.mean(axis=1), axis=0), rb.sub(rb.mean(axis=1), axis=0)
    return ((ac * bc).sum(axis=1) / np.sqrt((ac ** 2).sum(axis=1) * (bc ** 2).sum(axis=1))).where(ok.sum(axis=1) >= 10).dropna()


def economic(ct: dict, fxi: dict, lc: dict) -> dict:
    """第四类迁移标签：扣除成本、汇率或容量（市场结构）后是否仍有经济意义（BR-05）。"""
    reasons = []
    if (ct["gross_excess"] or 0) > 0 and (ct["net_excess"] or 0) <= 0:
        reasons.append("扣交易成本（含税费）后超额收益不再为正")
    if not fxi.get("same_currency") and (fxi.get("excess_local") or 0) > 0 and (fxi.get("excess_base") or 0) <= 0:
        reasons.append(f"换算为基础币种（{fxi['base']}）后超额收益不再为正")
    if lc.get("capacity_base") is not None and lc["capacity_base"] < CAPACITY_FLOOR:
        reasons.append(f"容量约 {lc['capacity_base'] / 1e6:.0f} 百万 {fxi['base']}，低于门槛 {CAPACITY_FLOOR / 1e6:.0f} 百万")
    meaningful = not reasons and (ct["net_excess"] or 0) > 0
    return {"meaningful": meaningful, "reasons": reasons,
            "label": "扣除成本 / 汇率 / 容量后不再具有经济意义" if reasons else
                     ("扣除成本与汇率后仍有正超额" if meaningful else "扣成本前即无正超额")}


# ---------------------------------------------------------------- 入口

def diagnose_round(run_dir: Path, key: str, md: dict, fx: pd.Series | None, run: dict) -> dict | None:
    bt_p, pos_p = run_dir / "backtest" / f"{key}.parquet", run_dir / "positions" / f"{key}.parquet"
    if not bt_p.exists() or not pos_p.exists():
        return None
    market, horizon = run["market"], run["horizon"]
    bt, pos = pd.read_parquet(bt_p), pd.read_parquet(pos_p)
    bt["date"] = pd.to_datetime(bt["date"])
    ct = cost_turnover(bt, market)
    lc = liquidity_capacity(pos, md, fx)
    fxi = fx_impact(bt, fx, LOCAL[market], BASE_CURRENCY)
    pre_fix = (run.get("started_at") or "") < TIE_FIX_AT
    out = {"run_id": run_dir.name, "round": key, "market": market, "horizon": horizon, "base_currency": BASE_CURRENCY,
           "backtest_note": "基于 RD-Agent 原始回测（并列顺序修正前，收益受随机选股影响，见第 14.7–14.8 节）" if pre_fix else "",
           "cost": ct, "liquidity": lc, "fx": fxi, "exposure": exposures(bt, pos, md), "economic": economic(ct, fxi, lc)}
    sc_p = run_dir / "score" / f"{key}.parquet"
    if sc_p.exists():
        def load(p):
            s = pd.read_parquet(p)
            s["symbol"] = s["instrument"].map(symbol)
            return s.pivot_table(index="date", columns="symbol", values="score").sort_index()
        score = load(sc_p)
        base_p = run_dir / "score" / "baseline.parquet"
        base = load(base_p) if key != "baseline" and base_p.exists() else None
        out["signal"] = signal_diagnostics(score, base, md, horizon, bt.set_index("date")["bench"])
    else:
        out["signal"] = None      # 旧的导出没有逐股打分：重新导出后补齐
    return out


def diagnose_run(run_dir: Path) -> int:
    run = json.loads((run_dir / "run.json").read_text())
    md = market_data(run["market"])
    fx = fx_rate(LOCAL[run["market"]], BASE_CURRENCY)
    (run_dir / "diagnostics").mkdir(exist_ok=True)
    keys = sorted(p.stem for p in (run_dir / "backtest").glob("*.parquet"))
    n = 0
    for key in keys:
        d = diagnose_round(run_dir, key, md, fx, run)
        if d:
            tmp = run_dir / "diagnostics" / f"{key}.json.tmp"
            tmp.write_text(json.dumps(d, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            tmp.replace(run_dir / "diagnostics" / f"{key}.json")
            n += 1
    return n
