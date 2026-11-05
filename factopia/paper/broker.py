"""券商适配（模拟盘的可选真实通道）：长桥 LongPort OpenAPI，演示级实现。

- 启用条件：LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN 三个环境变量全部非空
  （缺任何一个都回退到影子撮合，系统无需账户即可跑通闭环）。仅支持港股。
- 平台代码 4 位补零（"0005.HK"），长桥代码去前导零（"5.HK"）。
- 下单：市价单（OrderType.MO）+ 当日有效（TimeInForceType.Day），数量按每手 100 股取整（向下）。
- 持仓与现金查询（stock_positions / account_balance）保留作对账用；模拟盘账本按成交回报本地记账
  （共享的长桥模拟账户带预置持仓与多币种余额，整体覆盖会污染本地账本）。
- 长桥 SDK 版本差异较大（属性名 quantity / qty、order 状态字符串不同），全部按防御式解析：
  先 today_orders 再 history_orders；状态字符串 lowercase 归并。
"""
import os

LOT = 100


class BrokerError(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def mode() -> str:
    """当前撮合模式：长桥配置齐全 → "longport"，否则 "shadow"（影子撮合）。"""
    return "longport" if all(_env(k) for k in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")) else "shadow"


def to_broker_symbol(code: str) -> str:
    """平台代码 → 长桥代码："0005.HK" → "5.HK"。"""
    head, _, tail = code.rpartition(".")
    return f"{head.lstrip('0') or '0'}.{tail}"


def from_broker_symbol(code: str) -> str:
    """长桥代码 → 平台代码："5.HK" → "0005.HK"。"""
    head, _, tail = code.rpartition(".")
    return f"{head.zfill(4)}.{tail}"


class LongPortBroker:
    """长桥交易通道（同步 TradeContext）。构造失败抛 BrokerError。"""

    def __init__(self):
        try:
            from longport.openapi import Config, TradeContext
        except ImportError as e:
            raise BrokerError(f"未安装 longport SDK：{e}（pip install longport 或 uv pip install 'factopia[paper]'）")
        try:
            # 新版 SDK（2.4+）Config 不再直接构造，改用 from_apikey 工厂方法
            cfg = Config.from_apikey(app_key=_env("LONGPORT_APP_KEY"),
                                     app_secret=_env("LONGPORT_APP_SECRET"),
                                     access_token=_env("LONGPORT_ACCESS_TOKEN"))
            self.ctx = TradeContext(cfg)
        except Exception as e:
            raise BrokerError(f"长桥交易通道初始化失败：{e}")

    # ---------------------------------------------------------------- 下单 / 撤单 / 查单

    def submit(self, symbol: str, side: str, qty: float) -> str | None:
        """市价单；按每手取整后数量为 0 时不下单（返回 None）。"""
        from decimal import Decimal
        from longport.openapi import OrderSide, OrderType, TimeInForceType
        n = int(qty // LOT) * LOT
        if n <= 0:
            return None
        try:
            resp = self.ctx.submit_order(to_broker_symbol(symbol), OrderType.MO,
                                         OrderSide.Buy if side == "buy" else OrderSide.Sell, Decimal(n), TimeInForceType.Day)
        except Exception as e:
            raise BrokerError(f"长桥下单失败 {symbol} {side} {n}：{e}")
        return str(getattr(resp, "order_id", "") or "") or None

    def check(self, order_id: str) -> dict:
        """查单结果：{"status": "filled"/"cancelled"/"pending", "filled_qty", "avg_price", "msg"}。"""
        out = {"status": "pending", "filled_qty": 0, "avg_price": None, "msg": ""}
        orders = None
        try:
            orders = self.ctx.today_orders()
        except Exception:
            pass
        if orders is None:
            try:
                orders = self.ctx.history_orders()
            except Exception as e:
                out["msg"] = f"查询订单失败：{e}"
                return out
        for o in orders or []:
            if str(getattr(o, "order_id", "") or "") != str(order_id):
                continue
            st = str(getattr(o, "status", "") or "").lower()
            filled_qty = float(getattr(o, "executed_quantity", 0) or getattr(o, "filled_qty", 0) or 0)
            avg_price = getattr(o, "avg_executed_price", None)
            if avg_price is None:
                avg_price = getattr(o, "avg_price", None)
            if any(k in st for k in ("cancel", "reject", "expire", "fail", "disused")):
                out["status"] = "cancelled"
                out["filled_qty"] = filled_qty
                out["msg"] = f"券商状态：{st}"
            elif any(k in st for k in ("fill", "all")) or (filled_qty > 0 and "partial" in st):
                out["status"] = "filled"                  # 部分成交按已成交量结算，余量不再追单
                out["filled_qty"] = filled_qty
                out["avg_price"] = float(avg_price) if avg_price is not None else None
            elif filled_qty > 0:
                out["status"] = "filled"
                out["filled_qty"] = filled_qty
                out["avg_price"] = float(avg_price) if avg_price is not None else None
            else:
                out["status"] = "pending"
                out["msg"] = f"券商状态：{st}"
            return out
        out["msg"] = "当日与历史订单中都没有找到该订单（可能已过期）"
        return out

    def cancel(self, order_id: str) -> None:
        """撤销挂单；已成交或无法撤销时忽略（状态以 check 为准）。"""
        try:
            self.ctx.cancel_order(order_id)
        except Exception:
            pass

    # ---------------------------------------------------------------- 持仓 / 现金

    def holdings(self) -> dict:
        """持仓与现金（券商查询，对账用）：{"positions": {平台代码: 股数}, "cash": float}。

        演示级实现：现金只统计交易币种 HKD（多币种账户直接累加会把 USD/SGD 混进来）。
        """
        try:
            pos = self.ctx.stock_positions()
        except Exception as e:
            raise BrokerError(f"长桥持仓查询失败：{e}")
        positions = {}
        for ch in getattr(pos, "channels", []) or []:
            for p in getattr(ch, "positions", []) or []:
                qty = getattr(p, "quantity", None)
                if qty is None:
                    qty = getattr(p, "qty", 0)
                symbol = getattr(p, "symbol", "") or getattr(p, "ticker", "")
                if not symbol or not qty:
                    continue
                positions[from_broker_symbol(str(symbol))] = float(qty)
        cash = 0.0
        try:
            bal = self.ctx.account_balance()
            for a in bal or []:
                if str(getattr(a, "currency", "") or "").upper() != "HKD":
                    continue
                cash += float(getattr(a, "total_cash", None) or getattr(a, "cash_amount", 0) or 0)
        except Exception:
            pass
        return {"positions": positions, "cash": cash}
