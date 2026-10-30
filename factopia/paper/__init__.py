"""模拟盘（因子上线后的实盘前验证；Spec 第 14.15 节之后的闭环扩展）。

把信号目录中获批的市场版本（signal_id = <研究>.v<版本>.<市场>）放入模拟盘：
每日收盘后推进一轮（cycle）——重算冻结因子信号、按次月调仓下单、结算历史挂单、
估值拍净值快照、生成模拟盘报告（paper.json，写入 runtime/signals/<signal_id>/）。
预警时通过研究线索池的"模拟盘反馈"渠道回流，形成挖掘 → 审批 → 监控 → 模拟盘 → 再挖掘的闭环。

两种撮合模式：
- 影子撮合（默认，无需任何账户）：T 日收盘后按最新截面调仓，挂单记入 orders.jsonl，
  T+1 开盘价成交（开盘缺失用收盘价兜底），按 rules.costs 单边成本扣减现金；允许碎股（纯数值模拟）。
- 长桥 LongPort OpenAPI（演示级，可选）：三个环境变量（LONGPORT_APP_KEY / APP_SECRET / ACCESS_TOKEN）
  全部非空时启用；市价单按每手 100 股取整，持仓与现金以券商查询为准；仅支持港股，
  非港股信号此时自动使用影子撮合（两种模式按信号共存）。

目录结构（runtime/paper/<signal_id>/）：
    enrolled.json   登记信息（模式、初始资金、启停时间）
    state.json      推进状态（last_cycle / last_rebalance / positions / cash / pending）
    orders.jsonl    订单事件流（placed / filled / cancelled，只追加）
    nav.csv         每轮一条净值快照（date, nav, cash, positions_value）
    positions.csv   每轮的持仓明细（date, symbol, qty, price, value）
"""
from factopia.paper import broker
from factopia.paper import engine
from factopia.paper.broker import BrokerError, mode
from factopia.paper.engine import PAPER, PaperError, cycle, enroll, enrolled, unenroll
from factopia.paper import report                                  # 包属性 report = 模块（api 以 report.result() 使用）
from factopia.paper.report import result                           # result 保留函数导出（ideas 模拟盘反馈回流以 paper.result(sid) 使用）

__all__ = ["BrokerError", "PAPER", "PaperError", "broker", "cycle", "enroll", "enrolled",
           "mode", "report", "result", "unenroll"]
