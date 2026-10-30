"""模拟盘引擎：影子撮合（默认，无需账户）+ 长桥 LongPort（可选真实通道）。

撮合语义（影子模式）：月度调仓在信号截面日 T 收盘后决策挂单（placed_on = T），
T+1 起逐日以开盘价成交（开盘缺失用收盘价兜底），按 rules.costs 单边成本扣减现金；允许碎股。
长桥模式由券商市价单实际撮合（每手 100 股向下取整、当日有效、挂单 7 个自然日未成交自动撤销），
持仓与现金按成交回报本地记账（券商查询留作对账：共享模拟账户带预置持仓，整体覆盖会污染账本）；仅支持港股。
配置长桥时，非港股信号自动回落为影子撮合（模式按市场判定，长桥与影子可在同一进程内共存）。
模式在接入时固化到 enrolled.json 的 mode 字段，
运行期间不要改动 LONGPORT_* 环境变量（接入中途切换会导致挂单无法结算）。

cycle() 每轮（每个交易日收盘后触发一次，runner 任务 kind = "paper"）：
1. 结算历史挂单（先卖后买，同日先释放现金再买）；
2. 幂等守卫：行情没有新日期时只重算报告，不重复调仓；
3. 重算信号（与监控 monitor.run 同一口径：冻结因子 / 文本信号，截面标准化 + 参照期方向对齐 + 等权合成），
   并刷新 runtime/signals/<signal_id>/score.parquet，让 paper 报告的 rank IC 逐日新鲜；
4. 月度调仓（最新截面前 20% 等权，与监控口径一致；差额小于 5%×目标市值跳过）；
5. 估值快照（nav.csv / positions.csv）并生成报告（factopia/paper/report.py）。

挂单在下一次月度调仓时若仍未成交：卖单按剩余持仓封顶（不足部分撤销），
买单按可用现金缩放（余量撤销），避免陈旧挂单与新月调仓冲突。
"""
import json
import math
import os
import secrets
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from factopia import rules, sources
from factopia.paper import broker

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
PAPER = RUNTIME / "paper"
SIGNALS = RUNTIME / "signals"
TOP = 0.2                     # 调仓目标：最新截面前 20%（与 signals/monitor.py 同口径）
MIN_STABLE = 10               # 截面可用股票少于该数时不调仓（与 monitor 的月度选股门槛一致）
REBAL_TOL = 0.05              # 目标市值与当前市值差额小于 5%×目标市值时跳过该笔
LONGPORT_TTL_DAYS = 7         # 长桥挂单超过 7 个自然日未成交 → 撤销
DEFAULT_CAPITAL = float(os.environ.get("FACTOPIA_PAPER_CAPITAL", "1000000"))


class PaperError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), nd)


def _dir(sid: str) -> Path:
    return PAPER / sid


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _append_event(sid: str, ev: dict) -> None:
    """订单事件流（orders.jsonl，只追加，全部留痕）。"""
    p = _dir(sid) / "orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"signal_id": sid, "at": now(), **ev}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 接入 / 停用 / 清单

def enroll(signal_id: str, capital: float | None = None) -> dict:
    """把获批信号接入模拟盘。同一信号重复接入 = 重置：清空旧状态从头开始。

    仅允许生产中 / 已暂停的信号接入；行情来自模拟数据集（虚构行情）的信号拒绝接入。
    长桥通道仅用于港股；其他市场一律影子撮合（即使配置了 LONGPORT_* 环境变量）。
    """
    from factopia import signals

    try:
        ent = signals.entry(signal_id)
    except signals.CatalogError as e:
        raise PaperError(str(e)) from e
    if ent["state"] not in ("active", "paused"):
        raise PaperError(f"信号当前状态为「{ent['state_zh']}」，仅生产中 / 已暂停的信号可接入模拟盘")
    entry = signals.monitor_entry(signal_id)
    m = entry["market"]
    if entry.get("price_dataset") in sources.SIMULATED:
        raise PaperError("该信号的行情来自模拟数据集（虚构行情），不能接入模拟盘；真实行情的信号才能接入")
    mode = "longport" if (broker.mode() == "longport" and m == "hk") else "shadow"
    try:
        cap = DEFAULT_CAPITAL if capital is None else float(capital)
    except (TypeError, ValueError) as e:
        raise PaperError(f"初始资金无效：{e}") from e
    if not 0 < cap <= 1e12:
        raise PaperError("初始资金必须为 (0, 1e12] 中的正数")
    d = _dir(signal_id)
    if d.exists():
        shutil.rmtree(d)                    # 重复接入 = 重置
    (d / "work").mkdir(parents=True)
    rec = {
        "signal_id": signal_id, "market": m, "price_dataset": entry.get("price_dataset"),
        "mode": mode, "capital": cap, "horizon": entry["horizon"],
        "thresholds": entry.get("thresholds") or {}, "ref_start": entry.get("ref_start"), "ref_end": entry.get("ref_end"),
        "base_currency": entry.get("base_currency"), "enrolled_at": now(),
        "active": True, "stopped_at": None, "stop_reason": None,
    }
    _write_json(d / "enrolled.json", rec)
    _write_json(d / "state.json", {
        "last_cycle": None, "last_rebalance": None, "positions": {}, "cash": cap,
        "pending": [], "nav": cap, "positions_value": 0.0, "updated_at": now(),
    })
    return rec


def unenroll(signal_id: str, reason: str = "") -> dict:
    """停用模拟盘：撤销未结挂单（长桥模式下真实撤单），登记与流水保留供报告回看。"""
    d = _dir(signal_id)
    rec = _read(d / "enrolled.json")
    if rec is None:
        raise PaperError(f"信号 {signal_id} 没有接入模拟盘")
    state = _read(d / "state.json") or {}
    pending = state.get("pending") or []
    if pending and rec.get("mode") == "longport":
        try:
            br = broker.LongPortBroker()
            for o in pending:
                br.cancel(o.get("order_id") or "")
        except broker.BrokerError:
            pass                           # 撤单失败不阻断停用（挂单会自行过期）
    why = f"模拟盘停用：{reason or '—'}"
    for o in pending:
        _append_event(signal_id, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": o.get("symbol"),
                                  "side": o.get("side"), "qty": o.get("qty"), "reason": why})
    state["pending"] = []
    state["updated_at"] = now()
    _write_json(d / "state.json", state)
    rec.update(active=False, stopped_at=now(), stop_reason=reason or None)
    _write_json(d / "enrolled.json", rec)
    return rec


def enrolled() -> list[dict]:
    """全部模拟盘登记（含已停用），按接入时间倒序。"""
    out = [rec for rec in (_read(p) for p in sorted(PAPER.glob("*/enrolled.json"))) if rec]
    return sorted(out, key=lambda r: r.get("enrolled_at") or "", reverse=True)


# ---------------------------------------------------------------- 每日推进

def cycle(signal_id: str, log=print) -> dict:
    """推进一轮（幂等：行情没有新日期时只重算报告）。返回模拟盘报告（report.report 的结果）。"""
    from factopia import signals
    from factopia.paper.report import report as _report  # 直接导入函数（report.py 单向依赖 engine，无循环）

    d = _dir(signal_id)
    rec = _read(d / "enrolled.json")
    if rec is None or not rec.get("active"):
        raise PaperError(f"信号 {signal_id} 没有接入中的模拟盘")
    state = _read(d / "state.json") or {}
    entry = signals.monitor_entry(signal_id)          # 每轮重取：审批材料与阈值可能变化
    m = entry["market"]
    if entry.get("price_dataset") in sources.SIMULATED:
        raise PaperError("该信号的行情变成模拟数据（不能继续推进模拟盘）")
    if rec["mode"] == "longport" and m != "hk":
        raise PaperError("长桥模式仅支持港股，该信号已不适用")
    close = sources.close(m, entry.get("price_dataset"))
    if close.empty:
        raise PaperError("行情数据为空")
    last = close.index[-1]
    costs = rules.costs(m)
    # 1) 结算历史挂单（先卖后买在结算顺序中生效）
    if state.get("pending"):
        if rec["mode"] == "longport":
            _settle_longport(signal_id, state, costs, log)
        else:
            _settle_shadow(signal_id, state, close, _open(m), last, costs, log)
        _write_json(d / "state.json", state)
    # 2) 幂等守卫：行情没有新日期时只重算报告，不重复调仓
    if state.get("last_cycle") and pd.Timestamp(state["last_cycle"]) >= pd.Timestamp(last):
        log(f"{signal_id}：行情没有更新（最新 {pd.Timestamp(last).date()}），只重算报告")
        _snapshot(signal_id, state, close, last)   # 结算可能已改变持仓/现金：刷新估值（同日覆盖旧行，幂等）
        _write_json(d / "state.json", state)
        return _report(signal_id, log=log)
    # 3) 重算信号（与监控同一口径；每个有新数据的 cycle 都跑，让报告的 rank IC 逐日新鲜）
    score = _signal(signal_id, entry, m, close, d, log)
    # 4) 月度调仓：每月第一次有新数据的 cycle（含接入后的首次建仓）
    if state.get("last_rebalance") is None or pd.Period(last, "M") > pd.Period(state["last_rebalance"], "M"):
        try:
            _rebalance(signal_id, score, state, close, last, costs, rec["mode"], log)
        except PaperError as e:
            log(f"{signal_id}：{e}")
        _write_json(d / "state.json", state)
    # 5) 估值快照 + 报告
    _snapshot(signal_id, state, close, last)
    _write_json(d / "state.json", state)
    return _report(signal_id, log=log)


def _signal(sid: str, entry: dict, m: str, close: pd.DataFrame, d: Path, log) -> pd.DataFrame:
    """重算信号（与 monitor.run 相同口径），并刷新 runtime/signals/<sid>/score.parquet。"""
    from factopia.signals import monitor

    ref = (entry.get("ref_start"), entry.get("ref_end"))
    work = d / "work"
    work.mkdir(parents=True, exist_ok=True)
    if all(f.get("kind") == "text" for f in entry["factors"]):
        log(f"{sid}：读取文本信号（情报台按新公告持续更新）")
        score = monitor.text_signal(entry["factors"], ref, entry["horizon"], close)[0]
    else:
        log(f"{sid}：重算 {len(entry['factors'])} 个因子（冻结代码）")
        score = monitor.build_signal(entry["factors"], m, ref, entry["horizon"], work, close)[0]
    out = SIGNALS / sid
    out.mkdir(parents=True, exist_ok=True)
    score.stack().rename("score").reset_index().rename(columns={"level_0": "date", "level_1": "symbol"}) \
        .to_parquet(out / "score.parquet", index=False)
    return score


# ---------------------------------------------------------------- 影子撮合

def _open(market: str) -> pd.DataFrame:
    """日期 × 股票 开盘价（影子撮合的成交价；与 sources.close 同口径）。"""
    df = pd.read_parquet(RUNTIME / "data" / "standardized" / f"{market}.parquet", columns=["symbol", "date", "open"])
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="symbol", values="open").sort_index()


def _fill_price(symbol: str, placed_on: str, open_px: pd.DataFrame, close: pd.DataFrame, last) -> tuple:
    """挂单的成交日与成交价：T+1 起开盘价优先、收盘价兜底；限制在最新行情日之内（不用未来数据）。"""
    if symbol not in close.columns:
        return None, None
    idx = close.index[(close.index > pd.Timestamp(placed_on)) & (close.index <= last)]
    o = open_px[symbol] if symbol in open_px.columns else pd.Series(dtype=float)
    for dt in idx:
        p = o.get(dt)
        if p is not None and pd.notna(p) and float(p) > 0:
            return dt, float(p)
        c = close[symbol].get(dt)
        if c is not None and pd.notna(c) and float(c) > 0:
            return dt, float(c)
    return None, None


def _settle_shadow(sid: str, state: dict, close: pd.DataFrame, open_px: pd.DataFrame, last, costs: dict, log) -> None:
    """影子撮合结算：先卖后买逐单成交，现金按单边成本扣减；卖单按剩余持仓封顶，买单按现金缩放。"""
    remaining: list[dict] = []
    for side in ("sell", "buy"):                      # 先卖后买：先释放现金再买
        for o in state["pending"]:
            if o["side"] != side:
                continue
            symbol, qty = o["symbol"], float(o["qty"] or 0)
            dt, price = _fill_price(symbol, o.get("placed_on") or "", open_px, close, last)
            if price is None:
                future = close.index[(close.index > pd.Timestamp(o.get("placed_on"))) & (close.index <= last)]
                if len(future) or symbol not in close.columns:
                    _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": symbol,
                                        "side": o["side"], "qty": qty, "reason": "行情缺失（挂单期间无有效价格）"})
                else:
                    remaining.append(o)               # 还没有 T+1 行情，继续挂
                continue
            if side == "sell":
                held = float((state.get("positions") or {}).get(symbol, 0) or 0)
                if held <= 1e-9:
                    _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": symbol,
                                        "side": "sell", "qty": qty, "reason": "无持仓（可能已被清除）"})
                    continue
                q = min(qty, held)
                value = q * price
                cost = value * costs["sell"]["total"]
                state["positions"][symbol] = held - q
                state["cash"] = float(state.get("cash") or 0) + value - cost
                _append_event(sid, {"event": "filled", "order_id": o.get("order_id"), "symbol": symbol, "side": "sell",
                                    "qty": _r(q), "price": _r(price), "fill_date": str(pd.Timestamp(dt).date()),
                                    "value": _r(value, 2), "cost": _r(cost, 2)})
                if q < qty - 1e-9:
                    _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": symbol,
                                        "side": "sell", "qty": _r(qty - q), "reason": "持仓不足（部分成交）"})
            else:
                cash = float(state.get("cash") or 0)
                q = math.floor(min(qty, cash / (price * (1 + costs["buy"]["total"]))) * 1000) / 1000
                if q <= 0:
                    _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": symbol,
                                        "side": "buy", "qty": qty, "reason": "现金不足"})
                    continue
                value = q * price
                cost = value * costs["buy"]["total"]
                state["positions"][symbol] = float((state.get("positions") or {}).get(symbol, 0) or 0) + q
                state["cash"] = cash - value - cost
                _append_event(sid, {"event": "filled", "order_id": o.get("order_id"), "symbol": symbol, "side": "buy",
                                    "qty": _r(q), "price": _r(price), "fill_date": str(pd.Timestamp(dt).date()),
                                    "value": _r(value, 2), "cost": _r(cost, 2)})
                if q < qty - 1e-9:
                    _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": symbol,
                                        "side": "buy", "qty": _r(qty - q), "reason": "现金不足（部分成交）"})
    state["pending"] = remaining


# ---------------------------------------------------------------- 长桥结算

def _settle_longport(sid: str, state: dict, costs: dict, log) -> None:
    """长桥结算：查单后按券商成交回报本地记账（现金按单边成本扣减，与影子撮合同口径）。"""
    br = broker.LongPortBroker()
    remaining: list[dict] = []
    today = datetime.now(timezone.utc).date()
    for o in state["pending"]:
        try:
            res = br.check(o.get("order_id") or "")
        except broker.BrokerError as e:
            log(f"{sid}：查单失败 {o.get('symbol')} {o.get('side')}：{e}（保留挂单）")
            remaining.append(o)
            continue
        st, qty, px = res["status"], float(res["filled_qty"] or 0), res["avg_price"]
        if st == "filled" and qty > 0 and px:
            value = qty * float(px)
            cost = value * costs[o["side"]]["total"]
            if o["side"] == "sell":
                held = float((state.get("positions") or {}).get(o["symbol"], 0) or 0)
                state["positions"][o["symbol"]] = max(0.0, held - qty)
                state["cash"] = float(state.get("cash") or 0) + value - cost
            else:
                state["positions"][o["symbol"]] = float((state.get("positions") or {}).get(o["symbol"], 0) or 0) + qty
                state["cash"] = float(state.get("cash") or 0) - value - cost
            _append_event(sid, {"event": "filled", "order_id": o.get("order_id"), "symbol": o["symbol"], "side": o["side"],
                                "qty": _r(qty), "price": _r(float(px)), "value": _r(value, 2), "cost": _r(cost, 2),
                                "source": "longport"})
            if qty < float(o["qty"] or 0) - 1e-9:      # 部分成交：按已成交量结算，余量不再追单
                _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": o["symbol"],
                                    "side": o["side"], "qty": _r(float(o["qty"]) - qty),
                                    "reason": "部分成交，余量不再追单"})
            continue
        if st == "cancelled":
            _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": o["symbol"], "side": o["side"],
                                "qty": o.get("qty"), "reason": res.get("msg") or "券商已撤销"})
            continue
        if st == "filled" and qty > 0:
            log(f"{sid}：订单 {o.get('order_id')} 已成交但券商未回报均价，保留待下轮结算")
            remaining.append(o)
            continue
        try:
            age = (today - date.fromisoformat(str(o.get("placed_on"))[:10])).days
        except ValueError:
            age = 0
        if age > LONGPORT_TTL_DAYS:
            br.cancel(o.get("order_id") or "")
            _append_event(sid, {"event": "cancelled", "order_id": o.get("order_id"), "symbol": o["symbol"], "side": o["side"],
                                "qty": o.get("qty"), "reason": f"挂单超过 {LONGPORT_TTL_DAYS} 个自然日未成交，已撤销"})
            continue
        remaining.append(o)
    state["pending"] = remaining


# ---------------------------------------------------------------- 月度调仓

def _rebalance(sid: str, score: pd.DataFrame, state: dict, close: pd.DataFrame, last, costs: dict, mode: str, log) -> None:
    """月度调仓：最新截面前 20% 等权（与监控口径一致），先卖后买，小额差额跳过。

    截面可用股票不足、或信号没有任何可用值时抛 PaperError（cycle 捕获后本月跳过，下轮重试）。
    """
    avail = score.loc[:last].dropna(how="all")
    if avail.empty:
        raise PaperError("信号没有任何可用值，无法调仓")
    rdate = avail.index[-1]
    row = avail.loc[rdate].dropna()
    if len(row) < MIN_STABLE:
        raise PaperError(f"最新截面可用股票只有 {len(row)} 只（< {MIN_STABLE}），本月不调仓")
    n = max(1, int(len(row) * TOP))
    targets = list(row.sort_values(ascending=False).index[:n])
    px = close.ffill().loc[rdate]

    def _p(s):
        v = px.get(s)
        return float(v) if v is not None and pd.notna(v) else None

    positions = state.get("positions") or {}
    pos_val = 0.0
    for s, q in positions.items():
        q, p = float(q or 0), _p(s)
        if q > 1e-9 and p:
            pos_val += q * p
    total = float(state.get("cash") or 0) + pos_val
    target_value = total / n
    plan = []                                          # (symbol, side, qty)
    for s, q in sorted(positions.items()):             # 不在目标中的持仓全部卖出
        q = float(q or 0)
        if q <= 1e-9 or s in targets:
            continue
        p = _p(s)
        if not p:
            log(f"{sid}：{s} 价格缺失，无法卖出（保留持仓）")
            continue
        plan.append((s, "sell", q))
    for s in targets:                                  # 目标持仓向等权靠拢
        p = _p(s)
        if not p:
            log(f"{sid}：{s} 价格缺失，本月不买入")
            continue
        cur = float(positions.get(s, 0) or 0)
        diff = target_value - cur * p
        if diff >= REBAL_TOL * target_value:
            plan.append((s, "buy", diff / p))
        elif -diff >= REBAL_TOL * target_value and cur > 1e-9:
            plan.append((s, "sell", min(cur, -diff / p)))
    if not plan:
        log(f"{sid}：目标组合与当前持仓一致，无需下单")
        state["last_rebalance"] = str(pd.Timestamp(rdate).date())
        return
    new_pending: list[dict] = []
    br = broker.LongPortBroker() if mode == "longport" else None
    for symbol, side, qty in plan:
        if mode == "longport":
            n_lot = int(qty // broker.LOT) * broker.LOT
            if n_lot <= 0:
                _append_event(sid, {"event": "cancelled", "order_id": None, "symbol": symbol, "side": side, "qty": 0,
                                    "reason": "按每手 100 股取整后数量为 0，未下单"})
                continue
            try:
                oid = br.submit(symbol, side, n_lot)
            except broker.BrokerError as e:
                log(f"{sid}：下单失败 {symbol} {side}：{e}")
                continue
            if oid is None:
                _append_event(sid, {"event": "cancelled", "order_id": None, "symbol": symbol, "side": side, "qty": 0,
                                    "reason": "券商未返回订单号"})
                continue
            placed_qty, placed_on = float(n_lot), now()[:10]
        else:
            placed_qty = math.floor(qty * 1000) / 1000
            if placed_qty <= 0:
                continue
            oid, placed_on = f"sh_{secrets.token_hex(4)}", str(pd.Timestamp(rdate).date())
        _append_event(sid, {"event": "placed", "order_id": oid, "symbol": symbol, "side": side,
                            "qty": _r(placed_qty), "placed_on": placed_on, "mode": mode})
        new_pending.append({"order_id": oid, "symbol": symbol, "side": side, "qty": placed_qty,
                            "placed_on": placed_on, "mode": mode})
    state["pending"] = (state.get("pending") or []) + new_pending
    state["last_rebalance"] = str(pd.Timestamp(rdate).date())
    log(f"{sid}：调仓下单 {len(new_pending)} 笔（目标 {len(targets)} 只等权，截面 {pd.Timestamp(rdate).date()}）")


# ---------------------------------------------------------------- 估值快照

def _snapshot(sid: str, state: dict, close: pd.DataFrame, last) -> None:
    """估值快照：现金 + 持仓市值 = 净值；写 nav.csv / positions.csv（同日重跑覆盖旧行）。"""
    px = close.ffill()
    positions = state.get("positions") or {}
    for s in list(positions):
        if float(positions[s] or 0) <= 1e-9:
            del positions[s]
    pos_val, rows = 0.0, []
    for s, q in sorted(positions.items()):
        q = float(q or 0)
        p = px.at[last, s] if s in px.columns else float("nan")
        v = q * float(p) if pd.notna(p) else 0.0       # 价格缺失按 0 估值（停牌 / 退市防御）
        pos_val += v
        rows.append((s, q, float(p) if pd.notna(p) else None, v))
    cash = float(state.get("cash") or 0)
    nav = cash + pos_val
    dstr = str(pd.Timestamp(last).date())
    state.update(last_cycle=dstr, nav=_r(nav, 2), positions_value=_r(pos_val, 2), updated_at=now())
    d = _dir(sid)
    p = d / "nav.csv"
    df = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["date", "nav", "cash", "positions_value"])
    df = df[df["date"] != dstr]
    df = pd.concat([df, pd.DataFrame([{"date": dstr, "nav": round(nav, 2), "cash": round(cash, 2),
                                       "positions_value": round(pos_val, 2)}])], ignore_index=True)
    df.to_csv(p, index=False)
    p2 = d / "positions.csv"
    df2 = pd.read_csv(p2) if p2.exists() else pd.DataFrame(columns=["date", "symbol", "qty", "price", "value"])
    df2 = df2[df2["date"] != dstr]
    if rows:
        df2 = pd.concat([df2, pd.DataFrame([{"date": dstr, "symbol": s, "qty": round(q, 4), "price": _r(pr),
                                             "value": round(v, 2)} for s, q, pr, v in rows])], ignore_index=True)
    df2.to_csv(p2, index=False)
