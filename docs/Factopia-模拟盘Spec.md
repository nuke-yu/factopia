# Factopia 模拟盘需求规格（Spec）

状态：已实现于 `factopia/paper/`（`engine.py` 接入与推进、`broker.py` 长桥通道、`report.py` 报告）。
本文对接入与撮合语义做正式规格化；主需求见 [Factopia-最小POC-Spec.md](Factopia-最小POC-Spec.md) 第 14.15 节（信号目录与上线后监控），模拟盘为该节的扩展。

## 1. 概述

### 1.1 目的

给获批上线的信号一个"类实盘"验证环境：无账户即可闭环（影子撮合），配置券商模拟账户后港股可走真实通道（长桥），产出净值、成交、超额与 IC 报告，运行建议回流线索池。

### 1.2 范围

接入（enroll）→ 每日推进（cycle）→ 影子 / 长桥两种撮合 → 月度调仓 → 估值与报告 → 反馈回流。不含：真实资金、多账户管理、日内交易。

### 1.3 术语

| 术语 | 含义 |
|---|---|
| 影子撮合 | 本地行情模拟成交：截面日 T 收盘决策，T+1 起开盘价成交 |
| 长桥模式 | 经 LongPort OpenAPI 向券商模拟账户提交真实市价单 |
| 截面日 T | 调仓决策所用信号截面的最新日期（`placed_on` 即 T） |
| 挂单 pending | 已决策未成交的订单（影子：等待 T+1 行情；长桥：券商在途） |

## 2. 业务需求

| 编号 | 需求 | 状态 |
|---|---|---|
| BR-P1 | 获批（生产中 / 已暂停）信号可接入模拟盘，接入后逐日自动推进并出报告 | 已实现 |
| BR-P2 | 双撮合通道：默认影子撮合零门槛；配置长桥后港股走真实通道、非港股自动回落影子，两种模式可共存 | 已实现 |
| BR-P3 | 调仓与监控口径一致：月度、最新截面前 20% 等权、同一信号重算逻辑 | 已实现 |
| BR-P4 | 模拟盘运行建议（降杠杆 / 暂停等）回流线索池，形成反馈闭环 | 已实现 |
| BR-P5 | 同一信号重复接入 = 重置（只影响该市场版本）；停用保留历史可回看 | 已实现 |
| BR-P6 | 推进幂等：行情无新日期不重复调仓，重复执行不产生重复订单 | 已实现 |

## 3. 功能需求

### 3.1 接入（enroll）

- 门槛：信号状态 ∈ {active, paused}；行情来自模拟数据集（`sources.SIMULATED`）的信号拒绝接入；
- 参数：初始资金 `capital`，默认 `FACTOPIA_PAPER_CAPITAL`（100 万），范围 (0, 1e12]；
- 模式判定（接入时固化进 `enrolled.json` 的 `mode` 字段）：
  - `LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN` 全部非空 **且** 市场为港股 → `longport`；
  - 其余情形（含已配置长桥但信号非港股）→ `shadow`；
- 重复接入 = 重置：删除 `runtime/paper/<sid>/` 重建账本；接入即排入第一轮推进任务；
- 权限：接入 / 重置由 runner（root）执行——API 进程 uid 10001 无法删除 runner 写过的目录；
- 例外做法：要把某个港股信号强制接入为影子模式（例如长桥账户额度不足），在 runner 容器内的脚本进程先清空 `LONGPORT_*` 环境变量再调用 `engine.enroll(signal_id)`，模式即固化为 `shadow`。

### 3.2 停用（unenroll）

- 长桥在途挂单调券商撤单（失败不阻断停用，挂单会自行过期）；本地挂单全部写入 cancelled 事件留痕；
- 登记标记 `active=false` 并记录 `stop_reason`；报告与订单流水保留，可重新接入（= 重置）。

### 3.3 每日推进（cycle，五步）

1. **结算历史挂单**：先卖后买（同日先释放现金再买）；
2. **幂等守卫**：`last_cycle` ≥ 行情最新日期时只重算报告，不重复调仓；
3. **重算信号**：与监控 `monitor.run` 同一口径（冻结因子 / 文本信号、截面标准化、参照期方向对齐、等权合成），刷新 `runtime/signals/<sid>/score.parquet`；
4. **月度调仓**：每月首个有新数据的 cycle（含接入后的首次建仓）；
5. **估值快照与报告**：`nav.csv` / `positions.csv` / `paper.json`。

### 3.4 影子撮合语义

- 成交价：挂单日（placed_on = 截面日 T）之后首个有行情日的开盘价，开盘缺失用收盘价兜底；只用已实现数据（≤ 行情最新日，不用未来数据）；
- 数量：目标市值 / 价格，按可用现金封顶，允许碎股；成本按规则包 `rules.costs(market)` 单边扣减现金；
- 陈旧挂单：下月调仓时卖单按剩余持仓封顶、买单按可用现金缩放，余量撤销，避免与新月调仓冲突。

### 3.5 长桥通道语义

- 订单：市价单（OrderType.MO）+ 当日有效（TimeInForceType.Day）；
- 数量：`QuoteContext.static_info` 查询每手股数（进程内缓存；查询失败回退每手 100 股），向下取整，取整后为 0 不下单（记 cancelled 事件）；
- 重试：触发限流（429002 / rate limit）按券商提示的等待秒数自动重试；连接类错误（代理抖动）短暂等待后重试，均最多 3 次；
- 结算：查单 `today_orders` 优先、`history_orders` 兜底；成交均价字段按 `avg_executed_price → avg_price → price` 回退链解析（SDK 版本差异，防御式解析）；
- 记账：持仓与现金按成交回报本地记账；券商 `stock_positions / account_balance` 查询仅作对账（共享模拟账户带预置持仓与多币种余额，整体覆盖会污染本地账本）；
- TTL：挂单超过 7 个自然日未成交自动撤销。

### 3.6 月度调仓

- 目标：最新截面前 20%（TOP = 0.2）等权；截面可用股票少于 10（MIN_STABLE）不调仓；
- 容差：目标市值与当前市值差额 < 5%×目标市值（REBAL_TOL）跳过该笔；目标组合与当前一致则整轮不下单；
- 触发：`Period(行情最新日, "M") > Period(last_rebalance, "M")`；`last_rebalance` 记为截面日。

### 3.7 报告

- 文件：`runtime/signals/<signal_id>/paper.json`（页面实际读取；原子写入），快照与流水留在 `runtime/paper/<sid>/`；
- 内容：净值曲线与最新净值、持仓与挂单、成交统计（fills / buy_value / sell_value / costs，统计 orders.jsonl 的 filled 事件）、缺口感知年化波动、对等权基准的超额、rank IC 复检、预警与运行建议（经"模拟盘反馈"回流线索池）；
- **组合视图（portfolio 块，页面「组合（持股比例与个股贡献）」面板）**：
  - `created`（组合创建）：首个买入成交日、当日买入股票数与买入金额；
  - `holdings`（组合管理）：当前持仓明细——数量、累计平均成本、现价、市值、**持股比例**（市值 / 账户净值）、浮动盈亏，按市值降序；
  - `contributions`（个股贡献）：每只股票（含已清仓）的已实现盈亏 + 浮动盈亏 − 该股累计交易成本，贡献率以初始资金为分母；全组合贡献之和与「净值 − 初始资金」一致；
  - `rebalances`：调仓轮数（orders.jsonl 中 placed 事件的 distinct 下单日期数）；
  - 成本口径为累计平均成本（累计买入金额 / 累计买入股数），卖出按该均价结转；停牌 / 价格缺失的持仓按 0 估值，与引擎快照口径一致。

### 3.8 调度与手动推进

- runner 每日自动排队：`FACTOPIA_PAPER_ADVANCE_UTC_HOUR`（默认 UTC 9 = 港股 17:00 收盘后）；每信号每 UTC 日最多自动排一个任务，排队暂停时不排；
- 手动入口：`POST /api/signals/{sid}/paper/advance` 与 `python -m factopia.paper`（全部信号各一轮；`python -m factopia.paper report` 只重算报告）。

## 4. 接口清单

| 类型 | 入口 | 说明 |
|---|---|---|
| REST | `POST /api/signals/{signal_id}/paper` | 接入 / 重置；body `{"capital": <float>}` 可选 |
| REST | `POST /api/signals/{signal_id}/paper/stop` | 停用；body `{"reason": "..."}` |
| REST | `POST /api/signals/{signal_id}/paper/advance` | 手动推进一轮 |
| REST | `POST /api/research/{id}/versions/{v}/paper-demo` | 研究线结果一键接入演示线（获批市场生成信号并接入，可选 body.markets） |
| REST | `GET /api/signals` / `GET /api/signals/{sid}` | 列表（模拟盘摘要）/ 详情（完整 paper.json） |
| CLI | `python -m factopia.paper [report]` | 全部接入中信号推进 / 只重算报告 |

## 5. 数据与文件布局

```
runtime/paper/<signal_id>/
  enrolled.json            # 接入登记：mode / capital / horizon / thresholds / active ...
  state.json               # 账本：positions / cash / pending / nav / last_cycle / last_rebalance
  orders.jsonl             # 订单事件流（placed / filled / cancelled，只追加留痕）
  work/                    # 因子重算工作目录
  nav.csv / positions.csv  # 估值快照
runtime/signals/<signal_id>/
  paper.json               # 报告（页面读取）
  score.parquet            # 每轮刷新的信号打分
```

## 6. 业务规则

| 编号 | 规则 |
|---|---|
| R-P1 | 仅 active / paused 信号可接入；模拟数据集行情拒绝接入 |
| R-P2 | 长桥仅港股；已接入长桥的信号市场变为非港股时推进直接报错 |
| R-P3 | 模式接入后固化，运行期不改 `LONGPORT_*` 环境变量（中途切换会导致挂单无法结算） |
| R-P4 | 初始资金 ∈ (0, 1e12]；单市场版本独立账本（重复接入只重置该市场版本） |
| R-P5 | 接入后行情数据源变为模拟数据集时，推进直接报错 |
| R-P6 | 页面与接口只读 `SIGNALS/<sid>/paper.json`；`PAPER/<sid>/` 下为账本快照与流水 |

## 7. 非功能需求

- 幂等：cycle 重复执行不产生重复订单 / 重复调仓（幂等守卫 + 原子写 tmp→replace）；
- 留痕：orders.jsonl 只追加，cancelled 事件必带 reason；
- 容错：券商限流 / 断连自动重试；撤单失败不阻断停用；SDK 属性名差异防御式解析；
- 权限：paper 目录初始化时 chown 给 API uid；接入 / 重置任务固定经 runner（root）执行。

## 8. 约束与已知限制

1. **长桥共享模拟账户额度**：账户 HKD 购买力（现金 + 融资额度）不足时新买单报 `Insufficient Credit Limit`；处置：受影响信号按 3.1 的例外做法切回影子撮合，或提升额度 / 卖出释放购买力；
2. **行情更新是成交前提**：影子撮合的 T+1 成交与报告估值依赖 `download → standardize` 推进到新日期；数据目前手动更新，不更新则挂单持续 pending；
3. **推进时刻**：默认 UTC 9（港股收盘后）；如需交易时段内推进，调整 `FACTOPIA_PAPER_ADVANCE_UTC_HOUR`；
4. **网络依赖**：长桥通道经代理访问券商 OpenAPI 域名，代理抖动表现为连接错误（已自动重试）。

## 9. 验收要点

- [ ] 无长桥配置：港股 / 英国 / 法国信号全部以 shadow 接入，推进两轮（下单 + T+1 结算）后买入金额 > 0；
- [ ] 配置长桥：港股信号走真实市价单（数量按券商每手取整），非港股信号自动 shadow，两种模式同一进程内共存；
- [ ] 重复接入清空旧状态重新建仓；停用后在途挂单撤销且历史流水保留；
- [ ] 行情不更新时重复推进不产生新订单（幂等守卫生效）；
- [ ] 按手数取整后数量为 0 的订单不下单并留痕 cancelled；
- [ ] 报告指标（净值、成交、超额、rank IC）可读，运行建议回流线索池。
