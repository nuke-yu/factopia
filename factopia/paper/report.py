"""模拟盘报告：把运行状态汇总成 paper.json，供信号详情页与线索池回流使用。

输入（均已由 engine 落盘，均在 PAPER/<signal_id>/ 与 SIGNALS/<signal_id>/ 下）：
- enrolled.json —— 接入参数（模式 / 阈值 / 参考区间 / horizon）
- state.json     —— 最新状态（现金 / 持仓 / 挂单 / 净值）
- nav.csv        —— 每日净值序列（date, nav）
- orders.jsonl   —— 订单事件流（placed / filled / cancelled）
- score.parquet  —— 每轮重算的因子得分（engine._signal 刷新）

报告写入 SIGNALS/<signal_id>/paper.json（原子替换），指标口径：
- 年化波动按净值点之间的自然日缺口折算（（1+区间收益）^（1/天数）-1 后年化），兼容非逐日快照；
- 年化收益：区间不足 30 个自然日不年化（返回 null），避免短窗口复利外推爆炸；
- nav.series 归一化到 1 起点（nav.csv 存的是账户市值），与等权基准同尺度可直接同图比较；
- 基准为股票池等权（未扣成本），与已扣交易成本的模拟盘口径不完全可比；
- rank IC 复用 signals/monitor._rank_ic，显著性复用 validate/metrics.significance；
- 预警与建议（suggestion）镜像 monitor 的统计 + 经济双条件逻辑。
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from factopia import sources
from factopia.paper import engine

PAPER = engine.PAPER
SIGNALS = engine.SIGNALS

SUGGESTION_ZH = {
    "pause": "建议暂停该市场版本（统计与经济预警同时成立）",
    "watch": "加强观察",
    "ok": "正常",
}


def result(signal_id: str) -> dict | None:
    """读取已生成的模拟盘报告（未推进过则为 None）。"""
    return engine._read(SIGNALS / signal_id / "paper.json")


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except (FileNotFoundError, OSError):
        return pd.DataFrame()


def _nav_series(signal_id: str) -> pd.Series:
    df = _read_csv(PAPER / signal_id / "nav.csv")
    if df.empty or not {"date", "nav"}.issubset(df.columns):
        return pd.Series(dtype=float)
    s = pd.Series(df["nav"].to_numpy(dtype=float), index=pd.to_datetime(df["date"]))
    return s.sort_index().dropna()


MIN_ANN_DAYS = 30  # 区间短于 30 个自然日不年化：短窗口复利外推会放大成无意义的巨大数字


def _ann_from_total(total: float, days: int) -> float | None:
    """(1+区间收益)^(365/天数)-1；窗口不足 MIN_ANN_DAYS 天返回 None。"""
    if days < MIN_ANN_DAYS:
        return None
    years = max(days / 365.25, 1e-9)
    return (1.0 + total) ** (1.0 / years) - 1.0


def _nav_metrics(s: pd.Series) -> dict:
    """净值指标：累计 / 年化收益、缺口感知年化波动、夏普、最大回撤。"""
    out = {"total_return": None, "ann_return": None, "ann_vol": None, "sharpe": None, "max_drawdown": None}
    if len(s) < 2 or float(s.iloc[0]) <= 0:
        return out
    total = float(s.iloc[-1]) / float(s.iloc[0]) - 1.0
    ann = _ann_from_total(total, (s.index[-1] - s.index[0]).days)
    vol = None
    rets = s.pct_change(fill_method=None).dropna()
    if len(rets) > 1:
        # s 已去重去空，rets 与相邻净值点一一对应；把区间收益摊到缺口天数再年化
        gaps = np.array([(s.index[i + 1] - s.index[i]).days for i in range(len(s) - 1)], dtype=float)
        daily = np.power(1.0 + rets.to_numpy(), 1.0 / gaps) - 1.0
        vol = float(np.std(daily, ddof=1) * math.sqrt(252))
    sharpe = (ann / vol) if ann is not None and vol and vol > 0 else None
    out.update({
        "total_return": engine._r(total),
        "ann_return": engine._r(ann),
        "ann_vol": engine._r(vol),
        "sharpe": engine._r(sharpe),
        "max_drawdown": engine._r(float((s / s.cummax() - 1.0).min())),
    })
    return out


def _benchmark(close: pd.DataFrame | None, start, s: pd.Series):
    """股票池等权基准（未扣成本），对齐到净值日期序列；取不到行情返回 None。"""
    if close is None or close.empty or start is None or len(s) < 2:
        return None
    try:
        eq_ret = close.pct_change(fill_method=None).loc[start:].iloc[1:].mean(axis=1)
        eq = (1.0 + eq_ret.fillna(0.0)).cumprod()
        if not len(eq):
            return None
        bm = eq.reindex(eq.index.union(s.index)).sort_index().ffill().reindex(s.index)
        if pd.isna(bm.iloc[0]):
            bm.iloc[0] = 1.0
        if len(bm) < 2 or bm.isna().any() or float(bm.iloc[0]) <= 0:
            return None
        return bm.astype(float)
    except Exception:
        return None


def _rank_ic_block(signal_id: str, close, start, horizon: int) -> dict:
    """模拟盘期间的 rank IC 统计（score.parquet 由 engine._signal 每轮刷新）。"""
    out = {"mean": None, "std": None, "t": None, "n": 0, "rolling_60d_last": None, "monthly": {}}
    p = SIGNALS / signal_id / "score.parquet"
    if not p.exists() or close is None or close.empty or start is None:
        return out
    try:
        sc = pd.read_parquet(p).pivot(index="date", columns="symbol", values="score")
        sc.index = pd.to_datetime(sc.index)
        sc = sc.loc[start:]
        if sc.empty:
            return out
        fwd = close.shift(-int(horizon)) / close - 1.0
        from factopia.signals import monitor
        ric = monitor._rank_ic(sc, fwd).dropna()
    except Exception:
        return out
    if not len(ric):
        return out
    out["n"] = int(len(ric))
    out["mean"] = engine._r(float(ric.mean()))
    out["std"] = engine._r(float(ric.std(ddof=1))) if len(ric) > 1 else None
    if len(ric) > 30:
        try:
            from factopia.validate import metrics as vm
            sig = vm.significance(ric, horizon)
            out["t"] = engine._r(sig.get("t") if isinstance(sig, dict) else None)
        except Exception:
            out["t"] = None
    roll = ric.rolling(60, min_periods=40).mean()
    if len(roll) and pd.notna(roll.iloc[-1]):
        out["rolling_60d_last"] = engine._r(float(roll.iloc[-1]))
    monthly = ric.resample("ME").mean().dropna()
    out["monthly"] = {str(pd.Timestamp(k).date()): engine._r(float(v)) for k, v in monthly.items()}
    return out


def _trades(signal_id: str) -> dict:
    """成交流水统计（orders.jsonl 只追加，全部留痕）。"""
    out = {"fills": 0, "buy_value": 0.0, "sell_value": 0.0, "costs": 0.0}
    p = PAPER / signal_id / "orders.jsonl"
    if not p.exists():
        return out
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if ev.get("event") != "filled":
            continue
        out["fills"] += 1
        val = float(ev.get("value") or 0.0)
        if ev.get("side") == "buy":
            out["buy_value"] += val
        elif ev.get("side") == "sell":
            out["sell_value"] += val
        out["costs"] += float(ev.get("cost") or 0.0)
    out["buy_value"] = round(out["buy_value"], 2)
    out["sell_value"] = round(out["sell_value"], 2)
    out["costs"] = round(out["costs"], 2)
    return out


def _alerts(m: dict, rank_ic: dict, thresholds: dict, s: pd.Series, bm):
    """预警（镜像 monitor：统计 + 经济双条件，同时成立才建议暂停）。返回 (事件列表, 统计预警, 经济预警)。"""
    alerts: list[dict] = []
    stat_alert = econ_alert = False
    min_ic = thresholds.get("rolling_rank_ic_60d_min")
    last_roll = rank_ic.get("rolling_60d_last")
    if min_ic is not None and last_roll is not None and float(last_roll) < float(min_ic):
        stat_alert = True
        alerts.append({"kind": "statistical", "level": "alert", "title": "滚动 60 日 rank IC 低于获批阈值",
                       "detail": f"最近滚动 60 日均值 {float(last_roll):.4f}，低于获批阈值 {float(min_ic):.4f}。"})
    t_val = rank_ic.get("t")
    if t_val is not None and float(t_val) < -2:
        stat_alert = True
        alerts.append({"kind": "statistical", "level": "alert", "title": "模拟盘期间 rank IC 显著为负（方向反转）",
                       "detail": f"模拟盘期间 rank IC 均值的 t 值 {float(t_val):.2f}。"})
    ann = m.get("ann_return")
    if ann is not None and float(ann) < 0:
        dd_bench = None
        if bm is not None and len(s) >= 2 and float(s.iloc[0]) > 0:
            dd_bench = float(((s / float(s.iloc[0])) / bm - 1.0).min())  # 两侧都归一化到 1 起点，量纲一致
        lim = thresholds.get("drawdown_vs_bench_max")
        if lim is not None and dd_bench is not None and dd_bench < float(lim):
            econ_alert = True
            alerts.append({"kind": "economic", "level": "alert", "title": "模拟盘净值跑输基准并触及获批回撤上限",
                           "detail": f"对等权基准最大相对回撤 {dd_bench:.2%}，低于获批阈值 {float(lim):.2%}；年化收益 {float(ann):.2%}。"})
        else:
            alerts.append({"kind": "economic", "level": "watch", "title": "模拟盘年化收益为负",
                           "detail": f"年化收益 {float(ann):.2%}，注意观察是否持续。"})
    # suggestion 由 report() 汇总，这里只返回事件
    return alerts, stat_alert, econ_alert


def report(signal_id: str, log=print) -> dict:
    """汇总模拟盘运行报告，原子写入 SIGNALS/<signal_id>/paper.json 并返回。"""
    rec = engine._read(PAPER / signal_id / "enrolled.json")
    if rec is None:
        raise engine.PaperError(f"模拟盘未接入：{signal_id}")
    state = engine._read(PAPER / signal_id / "state.json")
    if state is None:
        raise engine.PaperError("没有模拟盘状态（先接入并推进一轮）")

    horizon = int(rec.get("horizon") or 20)
    thresholds = rec.get("thresholds") or {}
    close = None
    try:
        close = sources.close(rec.get("market"), rec.get("price_dataset"))
    except Exception:
        close = None

    s = _nav_series(signal_id)
    start = s.index[0] if len(s) else None
    if start is None:
        try:
            start = pd.Timestamp(str(rec.get("enrolled_at") or "")[:10])
        except ValueError:
            start = None

    m = _nav_metrics(s)
    bm = _benchmark(close, start, s)
    bm_ann = None
    if bm is not None:
        bm_ann = _ann_from_total(float(bm.iloc[-1]) / float(bm.iloc[0]) - 1.0, (bm.index[-1] - bm.index[0]).days)
    excess_ann = None
    if m["ann_return"] is not None and bm_ann is not None:
        excess_ann = float(m["ann_return"]) - bm_ann

    dates: list[str] = []
    nav_arr: list[float] = []
    bm_arr: list[float] = []
    if len(s) and float(s.iloc[0]) > 0:
        base = float(s.iloc[0])                            # 归一化到 1 起点，与基准同尺度（nav.csv 存的是账户市值）
        stride = max(1, len(s) // 200)
        idx = list(range(0, len(s), stride))
        if idx[-1] != len(s) - 1:
            idx.append(len(s) - 1)
        dates = [str(pd.Timestamp(s.index[i]).date()) for i in idx]
        nav_arr = [round(float(s.iloc[i]) / base, 4) for i in idx]
        if bm is not None:
            bm_arr = [round(float(bm.iloc[i]), 4) for i in idx]

    rank_ic = _rank_ic_block(signal_id, close, start, horizon)
    trades = _trades(signal_id)
    alerts, stat_alert, econ_alert = _alerts(m, rank_ic, thresholds, s, bm)
    suggestion = "pause" if (stat_alert and econ_alert) else ("watch" if alerts else "ok")

    positions = {k: v for k, v in (state.get("positions") or {}).items() if float(v) > 1e-9}
    mode_note = {
        "shadow": "影子撮合：T 日收盘截面决策、T+1 开盘价成交（开盘缺失用收盘价兜底），允许碎股，单边成本按规则包扣减。",
        "longport": "长桥演示通道：市价单实际成交，持仓与现金按成交回报本地记账，按每手 100 股取整。",
    }
    out = {
        "signal_id": signal_id,
        "market": rec.get("market"),
        "mode": rec.get("mode"),
        "active": bool(rec.get("active")),
        "computed_at": engine.now(),
        "start": str(pd.Timestamp(start).date()) if start is not None else None,
        "end": str(pd.Timestamp(s.index[-1]).date()) if len(s) else None,
        "days": int((s.index[-1] - s.index[0]).days) if len(s) >= 2 else 0,
        "capital": rec.get("capital"),
        "nav": {
            "last": round(float(s.iloc[-1]), 2) if len(s) else round(float(state.get("nav") or 0.0), 2),
            "first": round(float(s.iloc[0]), 2) if len(s) else None,
            **m,
            "series": {"dates": dates, "nav": nav_arr, "benchmark": bm_arr},
        },
        "excess": {
            "ann": engine._r(excess_ann),
            "benchmark_ann": engine._r(bm_ann),
            "note": "基准为股票池等权（未扣成本），模拟盘已含交易成本，两者口径不完全可比。",
        },
        "rank_ic": rank_ic,
        "trades": trades,
        "positions": {
            "count": len(positions),
            "cash": round(float(state.get("cash") or 0.0), 2),
            "value": round(float(state.get("positions_value") or 0.0), 2),
            "pending": len(state.get("pending") or []),
        },
        "alerts": alerts,
        "suggestion": suggestion,
        "suggestion_zh": SUGGESTION_ZH[suggestion],
        "thresholds": thresholds,
        "note": mode_note.get(rec.get("mode"), "") + f" 基准为股票池等权（未扣成本）；rank IC 含最后 {horizon} 日的未来收益。",
    }
    engine._write_json(SIGNALS / signal_id / "paper.json", out)
    log(f"[paper] {signal_id} 模拟盘报告已更新")
    return out
