"""模拟盘的数据库实现（对应 `runtime/paper/<signal_id>/`）。

文件时代一个模拟盘是六个文件，各自有不同的写入语义：

    enrolled.json    接入登记（一次写，之后只改 active/stopped_*）
    state.json       当前态（每轮整份覆盖：cash / positions / pending / last_cycle）
    orders.jsonl     订单事件流（只追加：placed / filled / cancelled）
    nav.csv          净值序列（同日重跑覆盖旧行）
    positions.csv    逐日持仓快照（同日重跑覆盖旧行）
    paper.json       运行报告（每次重算覆盖）

成表之后最实质的变化是 **orders 从"事件流"改成"一单一行"**：

    文件：placed / filled / cancelled 是三条独立记录，靠 order_id 在代码里 join
    表：  一个订单一行，状态原地更新（status / filled_qty / filled_px）

这不是丢信息——`filled_qty` / `filled_px` / `cost` / `filled_on` 就是原来那三条事件里的
全部字段，只是从"追加三条再回头拼"变成"改一行"。收益是"当前还有多少挂单"
从"扫全文件末尾状态"变成一句 `WHERE status='pending'`。

`state.positions` 拆进 `paper_position`，`state.pending` 由 `paper_order` 推导，
`paper_state` 只留账户级聚合值（现金 / 净值 / 持仓市值）。这样"某只股票被持有了多久"
和"哪笔挂单挂了三天没成交"都能直接查。

读回时仍然拼成调用方熟悉的 `state` 字典，引擎与报告一行都不用改。
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone

import pandas as pd
from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, fnum, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "paper"

_OVERVIEW = ("e.signal_id, e.market, e.mode, e.capital, e.horizon, e.thresholds, "
             "e.ref_start, e.ref_end, e.base_currency, e.enrolled_at, e.active, "
             "e.stopped_at, e.stop_reason, e.payload")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dstr(value) -> str | None:
    """date / Timestamp / 字符串 → 'YYYY-MM-DD'，与文件时代的字符串口径一致。"""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


# ── 接入登记 ─────────────────────────────────────────────────

def save_enrollment(rec: dict) -> None:
    payload = {k: v for k, v in rec.items()
               if k not in ("signal_id", "market", "mode", "capital", "horizon", "thresholds",
                            "ref_start", "ref_end", "base_currency", "enrolled_at", "active",
                            "stopped_at", "stop_reason")}
    db.execute(
        """INSERT INTO factopia.paper_enrollment
               (signal_id, market, mode, capital, horizon, thresholds, ref_start, ref_end,
                base_currency, enrolled_at, active, stopped_at, stop_reason, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (signal_id) DO UPDATE SET
               market = EXCLUDED.market, mode = EXCLUDED.mode, capital = EXCLUDED.capital,
               horizon = EXCLUDED.horizon, thresholds = EXCLUDED.thresholds,
               ref_start = EXCLUDED.ref_start, ref_end = EXCLUDED.ref_end,
               base_currency = EXCLUDED.base_currency, active = EXCLUDED.active,
               stopped_at = EXCLUDED.stopped_at, stop_reason = EXCLUDED.stop_reason,
               payload = EXCLUDED.payload""",
        (
            str(rec["signal_id"]), str(rec["market"]), str(rec["mode"]), fnum(rec.get("capital")),
            rec.get("horizon"), Jsonb(json_safe(rec.get("thresholds") or {})),
            as_dt(_dstr(rec.get("ref_start"))), as_dt(_dstr(rec.get("ref_end"))),
            rec.get("base_currency"), as_dt(rec.get("enrolled_at")) or as_dt(_now()),
            bool(rec.get("active", True)), as_dt(rec.get("stopped_at")), rec.get("stop_reason"),
            Jsonb(json_safe(payload)),
        ),
    )


def _merge_enrollment(row) -> dict:
    (signal_id, market, mode, capital, horizon, thresholds, ref_start, ref_end,
     base_currency, enrolled_at, active, stopped_at, stop_reason, payload) = row
    rec = {
        "signal_id": signal_id, "market": market, "mode": mode,
        "capital": fnum(capital), "horizon": horizon, "thresholds": thresholds or {},
        "ref_start": _dstr(ref_start), "ref_end": _dstr(ref_end),
        "base_currency": base_currency, "enrolled_at": iso(enrolled_at),
        "active": bool(active), "stopped_at": iso(stopped_at), "stop_reason": stop_reason,
    }
    rec.update(payload or {})
    return rec


def load_enrollment(signal_id: str) -> dict | None:
    row = db.query_one(
        f"SELECT {_OVERVIEW} FROM factopia.paper_enrollment e WHERE e.signal_id = %s", (signal_id,))
    return _merge_enrollment(row) if row else None


def enrollments() -> list[dict]:
    rows = db.query(
        f"SELECT {_OVERVIEW} FROM factopia.paper_enrollment e ORDER BY e.enrolled_at DESC")
    return [_merge_enrollment(r) for r in rows]


# ── 当前态 ───────────────────────────────────────────────────

def save_state(signal_id: str, state: dict) -> None:
    """写账户级聚合与持仓。挂单不在这里写——它由 order_event 维护，避免两处真相。"""
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO factopia.paper_state
                   (signal_id, last_cycle, last_rebalance, cash, nav, positions_value)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (signal_id) DO UPDATE SET
                   last_cycle = EXCLUDED.last_cycle, last_rebalance = EXCLUDED.last_rebalance,
                   cash = EXCLUDED.cash, nav = EXCLUDED.nav,
                   positions_value = EXCLUDED.positions_value""",
            (
                signal_id, as_dt(_dstr(state.get("last_cycle"))),
                as_dt(_dstr(state.get("last_rebalance"))), fnum(state.get("cash")) or 0,
                fnum(state.get("nav")) or 0, fnum(state.get("positions_value")) or 0,
            ),
        )
        conn.execute("DELETE FROM factopia.paper_position WHERE signal_id = %s", (signal_id,))
        for symbol, qty in (state.get("positions") or {}).items():
            q = fnum(qty) or 0
            if abs(q) <= 1e-9:
                continue
            conn.execute(
                """INSERT INTO factopia.paper_position (signal_id, symbol, qty)
                   VALUES (%s, %s, %s)""", (signal_id, symbol, q))


def load_state(signal_id: str) -> dict | None:
    row = db.query_one(
        """SELECT last_cycle, last_rebalance, cash, nav, positions_value
           FROM factopia.paper_state WHERE signal_id = %s""", (signal_id,))
    if row is None:
        return None
    last_cycle, last_rebalance, cash, nav, pos_val = row
    positions = {sym: fnum(q) for sym, q in db.query(
        "SELECT symbol, qty FROM factopia.paper_position WHERE signal_id = %s ORDER BY symbol",
        (signal_id,))}
    pending = [
        {"order_id": oid, "symbol": sym, "side": side, "qty": fnum(qty),
         "placed_on": _dstr(placed), "mode": mode}
        for oid, sym, side, qty, placed, mode in db.query(
            """SELECT order_id, symbol, side, qty, placed_on, mode FROM factopia.paper_order
               WHERE signal_id = %s AND status = 'pending'
                 AND left(order_id, 5) <> 'void_'
               ORDER BY created_at, order_id""",
            (signal_id,))
    ]
    return {"last_cycle": _dstr(last_cycle), "last_rebalance": _dstr(last_rebalance),
            "positions": positions, "cash": fnum(cash), "pending": pending,
            "nav": fnum(nav), "positions_value": fnum(pos_val)}


def reset(signal_id: str) -> None:
    """重复接入 = 重置：删登记即级联删掉状态、持仓、挂单、净值、报告。"""
    db.execute("DELETE FROM factopia.paper_enrollment WHERE signal_id = %s", (signal_id,))


# ── 订单 ─────────────────────────────────────────────────────

def order_event(signal_id: str, ev: dict) -> None:
    """`placed` 建行，`filled` / `cancelled` 改行。

    没有 order_id 的事件（券商没返回单号、按手数取整后为 0）也留一行：
    这类"没下出去的意图"是排查口径偏差的证据，丢掉它 report 里的成交笔数就对不上了。
    """
    kind = ev.get("event")
    oid = ev.get("order_id") or f"void_{secrets.token_hex(6)}"
    if kind == "placed":
        # 没有单号的 "空单"（券商没返回单号 / 按手数取整后为 0）用 status='void' 单独标记：
        # 它既不是挂单（不该出现在 pending 里），也不是成交（不该计入成交笔数），
        # 但必须留一行——否则 report 的"下了多少单"就对不上了。
        void = oid.startswith("void_")
        db.execute(
            """INSERT INTO factopia.paper_order
                   (order_id, signal_id, symbol, side, qty, placed_on, mode, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (order_id) DO NOTHING""",
            (oid, signal_id, str(ev.get("symbol") or ""), str(ev.get("side") or "buy"),
             fnum(ev.get("qty")) or 0, as_dt(_dstr(ev.get("placed_on"))),
             str(ev.get("mode") or "shadow"), "void" if void else "pending"),
        )
        return
    if kind == "filled":
        db.execute(
            """UPDATE factopia.paper_order
               SET status = 'filled', filled_qty = %s, filled_px = %s, filled_on = %s,
                   cost = %s, broker_ref = COALESCE(broker_ref, %s)
               WHERE order_id = %s""",
            (fnum(ev.get("qty")) or 0, fnum(ev.get("price")), as_dt(_dstr(ev.get("fill_date"))),
             fnum(ev.get("cost")), ev.get("source"), oid),
        )
        return
    if kind == "cancelled":
        db.execute(
            """UPDATE factopia.paper_order
               SET status = CASE WHEN filled_qty > 0 THEN 'filled' ELSE 'cancelled' END,
                   note = %s
               WHERE order_id = %s""",
            (str(ev.get("reason") or "")[:500], oid))


def order_events(signal_id: str) -> list[dict]:
    """把"一单一行"还原成事件流：report 的成交流水与组合视图按事件语义统计。"""
    rows = db.query(
        """SELECT order_id, symbol, side, qty, placed_on, mode, status,
                  filled_qty, filled_px, filled_on, cost, note
           FROM factopia.paper_order WHERE signal_id = %s ORDER BY created_at, order_id""",
        (signal_id,))
    out = []
    for (oid, sym, side, qty, placed, mode, status, fq, fp, fdate, cost, note) in rows:
        if oid.startswith("void_"):
            out.append({"signal_id": signal_id, "event": "cancelled", "order_id": None,
                        "symbol": sym, "side": side, "qty": fnum(qty), "reason": note})
            continue
        out.append({"signal_id": signal_id, "event": "placed", "order_id": oid, "symbol": sym,
                    "side": side, "qty": fnum(qty), "placed_on": _dstr(placed), "mode": mode})
        if status == "filled" and fq:
            out.append({"signal_id": signal_id, "event": "filled", "order_id": oid, "symbol": sym,
                        "side": side, "qty": fnum(fq), "price": fnum(fp),
                        "fill_date": _dstr(fdate),
                        "value": round(float(fq) * float(fp or 0), 6), "cost": fnum(cost)})
        elif status == "cancelled":
            out.append({"signal_id": signal_id, "event": "cancelled", "order_id": oid,
                        "symbol": sym, "side": side, "qty": fnum(qty), "reason": note})
    return out


def pending_count(signal_id: str) -> int:
    row = db.query_one(
        "SELECT count(*) FROM factopia.paper_order WHERE signal_id = %s AND status = 'pending'",
        (signal_id,))
    return int(row[0]) if row else 0


# ── 净值与持仓快照 ───────────────────────────────────────────

def save_nav(signal_id: str, dstr: str, nav: float, cash: float, positions_value: float,
             benchmark: float | None = None) -> None:
    db.execute(
        """INSERT INTO factopia.paper_nav (signal_id, date, nav, cash, positions_value, benchmark)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (signal_id, date) DO UPDATE SET
               nav = EXCLUDED.nav, cash = EXCLUDED.cash,
               positions_value = EXCLUDED.positions_value, benchmark = EXCLUDED.benchmark""",
        (signal_id, as_dt(dstr), nav, cash, positions_value, benchmark),
    )


def save_position_daily(signal_id: str, dstr: str, rows: list[tuple]) -> None:
    """rows: (symbol, qty, price, value)。同日重跑覆盖旧行——与引擎的幂等语义一致。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM factopia.paper_position_daily WHERE signal_id = %s AND date = %s",
                     (signal_id, as_dt(dstr)))
        for sym, qty, price, value in rows:
            conn.execute(
                """INSERT INTO factopia.paper_position_daily
                       (signal_id, date, symbol, qty, price, value)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (signal_id, as_dt(dstr), sym, fnum(qty), fnum(price), fnum(value)))


def nav(signal_id: str) -> pd.DataFrame:
    """净值序列（date/nav/cash/positions_value），列为 date 的 datetime。"""
    rows = db.query(
        """SELECT date, nav, cash, positions_value FROM factopia.paper_nav
           WHERE signal_id = %s ORDER BY date""", (signal_id,))
    if not rows:
        return pd.DataFrame(columns=["date", "nav", "cash", "positions_value"])
    df = pd.DataFrame(rows, columns=["date", "nav", "cash", "positions_value"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ("nav", "cash", "positions_value"):
        df[c] = df[c].astype(float)
    return df


def positions_daily(signal_id: str) -> pd.DataFrame:
    rows = db.query(
        """SELECT date, symbol, qty, price, value FROM factopia.paper_position_daily
           WHERE signal_id = %s ORDER BY date, symbol""", (signal_id,))
    if not rows:
        return pd.DataFrame(columns=["date", "symbol", "qty", "price", "value"])
    df = pd.DataFrame(rows, columns=["date", "symbol", "qty", "price", "value"])
    df["date"] = pd.to_datetime(df["date"])
    return df


# ── 运行报告 ─────────────────────────────────────────────────

def save_report(signal_id: str, report: dict) -> None:
    db.execute(
        """INSERT INTO factopia.paper_report (signal_id, computed_at, payload, suggestion)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (signal_id, computed_at) DO UPDATE SET
               payload = EXCLUDED.payload, suggestion = EXCLUDED.suggestion""",
        (signal_id, as_dt(report.get("computed_at")) or as_dt(_now()),
         Jsonb(json_safe(report)), report.get("suggestion")),
    )


def report_result(signal_id: str) -> dict | None:
    row = db.query_one(
        "SELECT payload FROM factopia.paper_report WHERE signal_id = %s "
        "ORDER BY computed_at DESC LIMIT 1", (signal_id,))
    return row[0] if row else None
