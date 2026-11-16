# Factopia 数据存储现状分析与数据库迁移方案

> 目标库：Team12 RDS PostgreSQL 16（`pgm-2ze9t2uh39o1115k.rwlb.rds.aliyuncs.com:5432/team12`，pgvector 0.8.0.2 已启用）
> 配额：8 GB 软配额（数据 + 索引），账号连接上限 30，建议应用连接池 5–10
> 配套文件：[`migrations/001_init_schema.sql`](../migrations/001_init_schema.sql)（可直接执行的建表脚本）、[`scripts/db/`](../scripts/db/)（隧道连接与验证脚本）
> 编写日期：2026-11-15 ｜ **已实测状态：DDL 已在真实 RDS 执行，7 项行为断言全绿（见第 8 节）**

---

## 0. 摘要

这个系统目前**没有任何数据库**。`pyproject.toml` 里没有数据库驱动，`docker-compose.yml` 里只有三个命名卷，全部业务状态以 JSON / YAML / JSONL / Parquet 文件形式落在 `runtime` 卷上（挂载点 `/workspace/runtime`），由 5 个容器共享。代码里 115 处引用 `RUNTIME` 路径，249 处直接读文件、149 处直接写文件。

代码里其实早就写好了迁移意图——三个核心 store 模块的文档字符串原话：

- `factopia/research/store.py`：*"最小 POC 用文件，阶段 2 改存数据库。"*
- `factopia/agents/store.py`：*"P0 用文件存储；阶段 2 可改数据库。"*
- `factopia/approval/__init__.py`：*"POC 用文件，正式平台改数据库。"*

所以这次迁移不是架构转向，而是**兑现一个已经预留的抽象**。

**核心判断（一句话）**：PostgreSQL 接管全部"会被查询、会被并发修改、需要事务"的业务状态；不可规范化的冻结件用 JSONB 保真；只整块读写的二进制（Qlib 数据集、RD-Agent 工作目录、逐股打分的 Parquet）留在文件/对象存储，用一张 `artifact` 表登记 sha256。

按这个划分，入库数据约 **350 MB – 1 GB**，占 8 GB 配额约 5%–12%；如果错误地把逐股打分分数也塞进库，光 50 次运行就会吃掉 3 GB 以上，且这些数据**全生命周期只被整体读一次**。

| 维度 | 现状 | 迁移后 |
|---|---|---|
| 事实来源 | `runtime` 命名卷上的文件 | PostgreSQL `factopia` schema（51 张表 + 4 个视图） |
| 事务 | 无。一次业务操作跨 3–8 个文件 | 单事务提交，跨表原子 |
| 并发 | 无。后写覆盖先写 | 行锁 / `FOR UPDATE SKIP LOCKED` / 唯一约束 |
| 查询 | 全目录扫描 + 全量解析 | 索引查询，O(1) |
| 多实例 | 不可能（卷只能单机） | api 可水平扩展 |
| 备份 | 停容器拷卷 | RDS 自动备份 + `pg_dump` |
| 留痕 | 文件追加 + 哈希链 | 表追加 + 触发器禁改 + 哈希链校验函数 |

---

## 1. 现状：数据存在哪里

### 1.1 存储地图

一个 Docker 命名卷 `runtime`，同时挂给 `app` / `rdagent` / `runner` / `exporter` / `api` 五个容器，写权限靠 uid 约定（API 是 10001，runner 是 root）。共 17 个数据域：

| 域 | 路径 | 写入者 | 读取者 | 形态 |
|---|---|---|---|---|
| 研究规格版本 | `research/<id>/v<k>.yaml` | api | api、runner、diagnostics、portfolio、approval | YAML，只新建不覆盖 |
| 研究终止标记 | `research/<id>/terminated.json` | api | api | JSON |
| 任务队列 | `jobs/<job>.json`、`.cancel`、`.log`、`.live.json`、`.paused` | api 建、runner 改 | api、runner | JSON + 控制文件 |
| 运行结果 | `results/runs/<run_id>/` | exporter、runner | api、approval、agents、portfolio、diagnostics、library、acceptance | JSON + Parquet |
| 授权组合 | `portfolio/<id>_v<k>/` | api / runner | api、approval、rules | JSON + Parquet |
| 审批 | `approvals/<sid>/` | api | api、signals、library | JSON + JSONL + 材料目录 |
| 信号与监控 | `signals/events.jsonl`、`signals/<sid>/` | api、runner | api | JSONL + JSON + Parquet |
| 模拟盘 | `paper/<sid>/` | runner | api | JSON + CSV + JSONL + 工作目录 |
| 线索池 | `ideas/<id>/`、`ideas/sources.json` | api | api、approval | JSON + JSONL + 附件 |
| 研究 Agent | `agents/<agt>/` | runner | api | JSON + JSONL + MD |
| 行情 | `data/raw/`、`data/standardized/` | runner / app | 几乎所有模块 | Parquet |
| 参考数据 | `refdata/*.parquet` | app | diagnostics、portfolio、paper | Parquet |
| 文本与公告 | `text/<market>/` | app | api | JSON + CSV |
| Qlib 数据集 | `qlib_home/` | app | rdagent、qlib-runner | 二进制 `.bin` |
| RD-Agent | `rdagent/<market>/h<n>/` | rdagent | exporter | mlruns / pickle / h5 |
| 验收 | `acceptance/` | api | api | JSON + MD |
| 运维留痕 | `access/events.jsonl`、`llm_costs.jsonl`、`reports/` | 多 | api | JSONL / HTML |

### 1.2 数据规模实测与估算

本地 `runtime/` 只有演示种子（`demo_e2e`），真实数据在 Docker 卷里，因此按代码里的口径推算：

| 数据 | 口径 | 估算 |
|---|---|---|
| 股票池 | `configs/universe/{hk,uk,fr}.csv` | 85 + 100 + 40 = **225 只** |
| 历史区间 | `data/download.py`：`START = "2015-01-01"` | 2015-01 至 2026-11 ≈ **2 950 个交易日** |
| 标准化行情 | 225 × 2950 | **≈ 66 万行**（Parquet 约 25 MB，入库后含索引约 100 MB） |
| 单轮回测产物 | 测试期 2022-01-01 至 2023-12-31 ≈ 490 日 × 225 | 逐股打分 **≈ 11 万行/轮**；一个 run（baseline + 5 轮）≈ 66 万行 |
| 逐日持仓 | 490 日 × 45 只（TOP 20%） | ≈ 2.2 万行/run |
| 逐日回测 / IC | 490 行 | 各 ≈ 500 行/run |
| 过程事件 | 每轮约 40 条 | ≈ 200 条/run |
| 审批提交 | `materials` JSONB（含三市场结果、两次预检、报告指纹） | ≈ 100–300 KB/提交 |
| 线索论文附件 | `ideas/<id>/files/` | 单个 ≤ 20 MB（`MAX_BYTES`） |

### 1.3 现有设计的六个结构性问题

这些不是"文件系统不好"，而是**业务已经长到文件系统撑不住**：

**① 没有事务边界。** `approval.submit()` 要连续写：`materials/` 下 4 类文件 → `submission.json` → `events.jsonl`。如果进程在 `_save(sub)` 之后、`_append(sid, ...)` 之前挂掉，就产生"提交存在但无提交留痕"的脏状态，且没有任何机制能发现它。`signals.act()`、`ideas.act()` 同样是"改状态 + 追加留痕"两步操作。

**② 没有并发控制。** `runner` 每 3 秒 `store.list_jobs()` 扫描整个 `jobs/` 目录逐文件解析，选中一个任务后在**另一次写盘**里把 `state` 改成 `running`——这两步之间就是竞态窗口。同时 API 也在写同一个 `job.json`（续跑、重试），是典型的 read-modify-write 丢更新。文件权限不一致还导致了一条硬限制，README 里写得很直白：*"接入 / 重置必须在 runner 容器内执行（root）：API 进程（uid 10001）无法删除 runner 写过的模拟盘目录。"*

**③ 查询等于全量扫描。** `signals.catalog()` 每次调用都 `approval.list_all()`（读所有 `submission.json`）→ 对每个提交读整份 `events.jsonl` → 再读每个 `monitor.json`，然后在 Python 里做聚合。页面越用越慢，且无法分页。`/api/overview/management` 同理。

**④ 无法水平扩展。** api 只能单副本（共享卷 + 文件写冲突）；卷只能挂在一台机器上，runner / dind 必须与卷同机。

**⑤ 备份与一致性难做。** 备份 `runtime` 卷必须停容器；即使停了，也没有跨域一致性快照——`run.json` 与 `rounds.json` 可能来自不同时刻。

**⑥ 血缘靠字符串约定。** `job.run_id` → `results/runs/<run_id>` → `submission.materials.results[m].run_id` → `signals/<sid>`，全是路径拼接，没有外键，改名/删除全靠人工小心。

---

## 2. 目标架构：三层分治

```
┌─────────────────────────────────────────────────────────────────────┐
│ L1  PostgreSQL 16 + pgvector  →  业务事实、状态、留痕、时序聚合       │
│     · 会按条件查询 / 排序 / 聚合 / join 的                            │
│     · 会被多个进程并发修改的                                          │
│     · 需要事务保证多表一致的                                          │
├─────────────────────────────────────────────────────────────────────┤
│ L2  文件 / 对象存储（ECS 数据盘或 OSS）  →  不可入库的二进制与冻结原件 │
│     · 只整块读写、体积大、可由上游重建的：parquet / h5 / pkl / pdf / log │
│     · 全部在 artifact 表登记 path + sha256 + bytes                    │
├─────────────────────────────────────────────────────────────────────┤
│ L3  Qlib 二进制数据集  →  保留文件，纯派生物，可由 L1 重建            │
│     · Qlib 只认 .bin，且 RD-Agent 直接挂载；这层不可替代               │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 归类规则（写进评审清单，避免逐条争论）

| 判据 | 去向 | 例子 |
|---|---|---|
| 需要按条件查、排序、分页、聚合、join | **PG 表 + 列 + 索引** | 任务队列、审批提交与事件、信号事件、模拟盘持仓与挂单、行情日线 |
| 一次写入、整体读取、必须字节级保真（冻结件） | **PG JSONB + sha256** | 审批 `materials`、研究规格 `spec`、组合 `summary`、监控结果 |
| 大体量、只整块读写、可由上游重建 | **L2 文件 + artifact 登记** | 逐股打分 `score/*.parquet`、报告 HTML、审计 zip、运行日志、RD-Agent workspace |
| 有专门格式要求的计算输入 | **L3 / L2 原样保留** | Qlib `.bin`、mlruns pickle、`.h5` |
| 只是运行期缓存 | **不持久化** | `paper/<sid>/work/`（可由冻结因子代码 + 行情重算） |

### 2.2 为什么逐股打分（`score/*.parquet`）必须留在库里之外

这是本次设计最容易做错的一个决定，单独说明：

- 单轮 11 万行、单 run 66 万行。50 次运行 = **3 300 万行**，含主键索引约 **3.3 GB**，直接吃掉 8 GB 配额的 40%。
- 而这份数据的读取方式只有一种：`diagnostics` 里整份 `pd.read_parquet()` 一次性加载做中性化 / 冗余 / 稳定性计算——**从不按条件筛行**。
- Parquet 列式压缩后单轮约 3 MB，比入库小一个数量级。

所以：分数留 Parquet（L2，登记 `artifact`），而**由它算出来的聚合结果**（逐日 IC、分组收益、单调性）入库，因为那些是页面要画、要跨 run 比较的。这就是"入库的是结论，不是原料"。

---

## 3. 数据库设计

完整 DDL 见 [`migrations/001_init_schema.sql`](../migrations/001_init_schema.sql)。以下说明结构与关键取舍。

### 3.1 全局约定

| 项 | 约定 | 理由 |
|---|---|---|
| schema | 独立 `factopia`，不动 `public` | RDS 是同队共用实例，隔离便于授权与清理 |
| 主键 | 沿用现有字符串编号（`research_id` / `job_id` / `run_id` / `agent_id` / `signal_id` / 提交编号） | 这些字符串已经写进报告、审计 zip、URL。**重新编号等于断链** |
| 时间 | `timestamptz`（现状是 UTC ISO 字符串，`timespec="seconds"`） | 保留秒级精度，跨时区运算正确 |
| 业务日 | `date` | 交易日、调仓日、估值日天然是日期 |
| 金额 | `numeric(14,6)` / `numeric(18,2)` | 费用、现金不能有二进制浮点误差 |
| 指标 | `double precision` | 允许 `NaN` / `Infinity`，与 Python 侧一致 |
| 数组 | `text[]`（markets、hits） | 需要 `ANY()` / GIN 检索 |

### 3.2 表清单（51 张表 / 4 个视图，按域分组）

| 域 | 表 |
|---|---|
| 研究规格 | `research`、`research_version`、`app_state` |
| 任务队列 | `job`、`job_resume`、`job_live` |
| 运行结果 | `run`、`run_round`、`run_factor`、`run_event`、`backtest_daily`、`backtest_position`、`run_ic_daily`、`run_quantile`、`run_validation` |
| 授权组合 | `portfolio`、`portfolio_daily`、`portfolio_weight`、`portfolio_feedback` |
| 审批 | `approval_submission`、`approval_step`、`approval_event`、`approval_rule_decision` |
| 信号与模拟盘 | `signal_event`、`signal_monitor`、`paper_enrollment`、`paper_state`、`paper_position`、`paper_position_daily`、`paper_order`、`paper_nav`、`paper_report` |
| 线索池 | `idea`、`idea_event`、`idea_document`、`idea_source` |
| 研究 Agent | `agent`、`agent_round`、`agent_memory`、`agent_trace` |
| 行情与参考数据 | `daily_bar`（按市场分区）、`fx_rate`、`data_quality`、`security_master`、`index_membership`、`corporate_action` |
| 知识检索 | `knowledge_chunk`（pgvector） |
| 运维 | `artifact`、`llm_cost`、`access_event`、`migration_file` |

视图：`v_signal_monitor_latest`、`v_job_overview`、`v_submission_latest`、`v_paper_position_value`。

### 3.3 四个关键设计决定

#### 3.3.1 冻结件用 JSONB，不规范化

审批的 `materials` 里有：假设、经济逻辑、三市场结果、两次预检的完整命中列表、组合摘要、报告 sha256、提示词与模型版本、规则包指纹、代码指纹、冻结翻译。这些内容一旦提交就**不允许变**，审计要求回答"当时是什么"。如果拆成 20 张表，任何一次 schema 演进或字段重算都会破坏保真度。

所以：`approval_submission.materials jsonb`（约 100–300 KB，TOAST 自动压缩）+ `fingerprint` 列存整体 sha256。**要查询的部分单独拆列**：`research_id` / `version` / `status` / `markets` / `approved_markets` / `current_step`。

同理：`research_version.spec`、`portfolio.summary`、`signal_monitor.payload`、`paper_report.payload`。

> 判据：这份数据是"被查询的"还是"被引用的"？被查询的拆列，被引用的进 JSONB。

#### 3.3.2 哈希链原样保留，并用触发器与权限双重防改

现状 `approval/events.jsonl`、`signals/events.jsonl`、`ideas/events.jsonl`、`access/events.jsonl` 都是只追加的哈希链（每条记录 `prev` 指向上一条 `hash`）。这是合规要求（BR-23），不能在迁移中丢掉。

表设计保留 `seq` / `prev_hash` / `hash` 三列，并加：

```sql
CREATE TRIGGER approval_event_append_only
    BEFORE UPDATE OR DELETE ON factopia.approval_event
    FOR EACH ROW EXECUTE FUNCTION factopia.tg_forbid_mutation();
```

应用账号另做 `REVOKE UPDATE, DELETE`。校验方式不变：按 `seq` 顺序重算 `sha256(除 hash 外全部字段)`，与 `prev_hash` 串联比对——只是从"读一个文件"变成"查一段行"。

**注意**：现有哈希是把整条事件 JSON 规范化（`sort_keys=True`）后取 sha256。迁移脚本必须用**同一套序列化规则**重算，否则历史链会校验失败。这是迁移验收的硬指标之一。

#### 3.3.3 任务队列改为 `FOR UPDATE SKIP LOCKED`

这是迁移收益最直接的地方——把 3 秒轮询全目录扫描，换成一条带行锁的取任务语句：

```sql
WITH picked AS (
    SELECT job_id FROM factopia.job
     WHERE state = 'queued'
       AND market = ANY($1)                      -- 本 runner 可处理的市场
     ORDER BY created_at
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE factopia.job j
   SET state = 'running', started_at = now()
  FROM picked
 WHERE j.job_id = picked.job_id
RETURNING j.*;
```

- **取任务与置为 running 在同一个事务里**，彻底消除"选中后还没写状态就被另一个 runner 再选中"的窗口。
- 同一市场不并发的约束，用事务级 advisory lock：`pg_try_advisory_xact_lock(hashtext(market))`。
- 取消请求不再靠 `.cancel` 文件轮询，改为 `job.payload ? 'cancel_requested_at'`，runner 每轮读一次该行即可。
- `job_live` 独立成表：运行器每 3–15 秒 UPSERT 一次实时状态，不与任务主行争锁。

#### 3.3.4 信号目录仍是代码派生，但数据源变成一条 SQL

`signals.catalog()` 的业务规则很重（获批市场取最近一次获批、重新提交不影响已生效版本、暂停/恢复事件叠加、复核日按监控建议提前、地区适用性标签要读规则包状态……）。**这些规则不该也不可能全部下推到 SQL**，硬做会变成不可维护的存储过程。

正确的改法：保持 `catalog()` 为 Python 函数，但把"读 N 个文件"换成"查一次库"：

```
approval_submission (按 research_id, version 倒序)
  ⋈ approval_event (只取 return/reject，用于重新提交说明)
  ⋈ v_signal_monitor_latest
  ⋈ signal_event (按 signal_id)
  ⋈ approval_rule_decision (用于合规例外标签)
```

一次往返拿全量，Python 里做规则判定。目录页从"随数据量线性变慢"变成"一次索引查询"。

同理，`paper.json` 不再手写快照文件——净值、持仓、成交、告警全部可由 `paper_nav` / `paper_position` / `paper_order` 聚合出来；只保留 `paper_report` 表存**每次报告成文时的完整快照**（因为报告口径会随规则包与基准变化，留快照才能回答"当时怎么说"）。

### 3.4 分区与索引

- `daily_bar` 按 `LIST (market)` 分三区（hk/uk/fr）：三市场永远分开查，分区让单市场扫描量降到 1/3，也方便单独重建某一市场。
- 两套索引覆盖两种访问模式：`(date, market)` 走截面，`(symbol, date)` 走时序。
- `run_event` 按 `(run_id, kind, seq)`：`/api/runs/{id}/events/{index}/prompt` 直接定位，不必解析整份 `events.json`。
- `approval_submission(research_id, version, created_at DESC)`：信号目录与风控台的主查询。
- `job (created_at) WHERE state = 'queued'`：部分索引，队列取任务只扫待办行。
- `knowledge_chunk.embedding` 建 HNSW（`vector_cosine_ops`，m=16，ef_construction=64）。

### 3.5 pgvector 的用法

现状 `research/semantic.py` 是"大模型三语改写 + BM25 关键词检索"，注释明确写了*"不依赖向量模型"*。向量检索是**增量能力**，不是迁移必需项，但 pgvector 已经在，收益明确：

- `knowledge_chunk` 存三类内容的切片：已锁定研究规格、因子库条目、经批准的外部公告。
- 检索改为 hybrid：HNSW 召回候选 → 沿用现有 BM25 打分 → 合并排序。**保留原有的可解释性**（每条结果仍标注命中词），只是召回率提升。
- 关键词列用 `lexemes text[]` + GIN，而不是 `tsvector`：中文分词在 RDS 上没有 `zhparser`，而现有代码已经有成熟的"中文双字切分 + 西文去重音"逻辑（`semantic.tokens()`），Python 侧算好词元写库，检索时用 GIN 过滤、BM25 排序。**不引入新的分词器依赖。**

维度按 `vector(1024)` 设定（bge-m3 / text-embedding-v3 均为此维度）；换模型需改列类型并重建索引，写进运维手册。

### 3.6 容量预算（8 GB 软配额）

| 项 | 估算 | 说明 |
|---|---|---|
| `daily_bar` + 索引 | 100 MB | 22.5 万行/市场 |
| 运行结果（50 次运行） | 150 MB | 回测/持仓/IC/事件/轮次/因子 |
| 审批（200 次提交） | 60 MB | materials JSONB 为主要占用 |
| 线索池（500 条 + 附件索引） | 25 MB | 附件本体在 L2 |
| Agent（100 个 × 1000 条 trace） | 30 MB | |
| 模拟盘（20 个信号 × 3 年净值/持仓/订单） | 20 MB | |
| 知识切片（5 万条 × 1024 维） | 220 MB | 向量占大头，HNSW 索引另约 60 MB |
| **合计** | **≈ 700 MB（8.7%）** | 留出 7 GB 余量给增长与临时排序空间 |

配套治理：`run` 级联删除（30 天前的非采纳轮次产物）、`artifact` 按 `created_at` 归档到 OSS、`log` 保留 90 天。审计相关的（审批、留痕、获批材料）**永不自动清理**。

---

## 4. 代码改造方案

### 4.1 核心策略：换实现，不换签名

这个系统的存储层虽然散，但**每个域的读写在模块内已经收敛成了函数**：`store.save_version()` / `store.list_jobs()` / `approval.load()` / `approval._append()` / `agents.store.save()`……调用方（111 个 API 端点、runner、exporter、diagnostics）只依赖这些函数。

因此改造方式是 **Repository 替换**：保持函数签名与返回值形状完全不变，只换内部实现。这样：

- 111 个端点**几乎不用动**（只有直接读文件的那 30 处需要改）。
- 迁移可以按域灰度，一次上线一个域，互不阻塞。
- 单测可以继续按"调用函数 → 断言返回 dict"的方式写。

新增两个包：

```
factopia/
  db/
    __init__.py     engine（连接池）、tx() 上下文管理器、retry、健康检查
    sql.py          参数化查询助手、批量 COPY
    migrate.py      迁移工具入口（见第 5 节）
  repo/
    research.py     研究规格与任务队列
    runs.py         运行结果与回测时序
    portfolio.py
    approval.py
    signals.py
    paper.py
    ideas.py
    agents.py
    market.py       daily_bar / fx / quality
    knowledge.py    knowledge_chunk + 向量
    artifacts.py    文件登记与清理
```

旧模块变成薄壳，只做转发，保留原有异常类型与返回结构：

```python
# factopia/research/store.py（改造后）
from factopia.repo import research as _repo

def load_version(research_id: str, version: int) -> dict:
    return _repo.load_version(research_id, version)      # 返回形状不变：完整 spec dict
```

### 4.2 双写与读切换开关

一个环境变量控制三种模式，便于灰度与回滚：

```
FACTOPIA_STORAGE = files | dual | db        # 默认 files，灰度期 dual，终态 db
```

- `files`：完全走老路径（迁移前的行为，用于回滚）。
- `dual`：**写双份**（文件为准、DB 异步补写并记录差异），**读走文件**。用于验证 DB 写入正确性而不影响用户。
- `db`：读写都走 DB，文件停止写入（保留只读用于兜底排查）。

更细的控制粒度：`FACTOPIA_STORAGE_<DOMAIN>`，例如 `FACTOPIA_STORAGE_JOBS=db` 让任务队列先切，其他域仍在 `files`。**按域灰度**是这套方案能落地的关键。

### 4.3 文件 → 表 映射表（迁移脚本的规格说明）

| 现状路径 | 目标 | 处理方式 |
|---|---|---|
| `research/<id>/v<k>.yaml` | `research_version.spec` | 整体入 JSONB + 规范化 sha256 |
| `research/<id>/terminated.json` | `research.terminated` | 整体入 JSONB |
| `jobs/<job>.json` | `job` | 公共字段拆列，`kind` 专有字段进 `payload` |
| `jobs/<job>.cancel` | `job.payload.cancel_requested_at` | 文件存在时间戳 |
| `jobs/<job>.live.json` | `job_live.state` | 整体入 JSONB |
| `jobs/<job>.log` | `artifact`（`kind='log'`） | **留在文件**，只登记 |
| `jobs/.paused` | `app_state('queue.paused')` | 单例 JSON |
| `results/runs/<r>/run.json` | `run` | |
| `results/runs/<r>/rounds.json` | `run_round` + `run_factor` | 数组拆行 |
| `results/runs/<r>/events.json` | `run_event` | 数组拆行，`seq` 按原顺序 |
| `backtest/*.parquet` | `backtest_daily` | 列名映射（`return`→`ret`） |
| `positions/*.parquet` | `backtest_position` | |
| `signal/*.parquet` | `run_ic_daily` | |
| `quantile/*.json` | `run_quantile` | 标量拆列，数组进 JSONB |
| `score/*.parquet` | **`artifact`** | **不入库**（见 2.2） |
| `validation/*.json` | `run_validation.payload` | |
| `report.md` | `artifact` | 留文件，登记 |
| `portfolio/<id>_v<k>/summary.json` | `portfolio.summary` | |
| `portfolio/.../daily.parquet` | `portfolio_daily` | 核心列 + `payload` |
| `portfolio/.../weights.parquet` | `portfolio_weight` | |
| `portfolio/.../feedback.json` | `portfolio_feedback` | 数组拆行 |
| `approvals/<sid>/submission.json` | `approval_submission` + `approval_step` + `approval_rule_decision` | 一个文档拆三处 |
| `approvals/<sid>/events.jsonl` | `approval_event` | 保留哈希链，`seq` 按文件顺序 |
| `approvals/<sid>/materials/**` | `artifact` | 留文件（审计 zip 要打原字节），`materials` 已含指纹 |
| `signals/events.jsonl` | `signal_event` | 全局链，`seq` 即 `bigserial` |
| `signals/<sid>/monitor.json` | `signal_monitor` | 每次计算一行（按 `computed_at`） |
| `signals/<sid>/paper.json` | `paper_report` | 快照入 JSONB |
| `signals/<sid>/score.parquet` | `artifact` | 不入库 |
| `paper/<sid>/enrolled.json` | `paper_enrollment` | |
| `paper/<sid>/state.json` | `paper_state` + `paper_position` + `paper_order` | 一个状态拆三处：账户聚合 / 持仓 / 挂单 |
| `paper/<sid>/nav.csv` | `paper_nav` | |
| `paper/<sid>/positions.csv` | `paper_position_daily` | |
| `paper/<sid>/orders.jsonl` | `paper_order` | 事件流折叠成订单行（`status` 取最终态） |
| `paper/<sid>/work/**` | 不迁移 | 可重算的缓存 |
| `ideas/<id>/idea.json` | `idea` | |
| `ideas/<id>/events.jsonl` | `idea_event` | 保留哈希链 |
| `ideas/<id>/files/*` | `idea_document` + `artifact` | 附件留文件，登记 sha256 |
| `ideas/sources.json` | `idea_source` | 去重键成为主键 |
| `agents/<a>/agent.json` | `agent` | `report.md` 并入 `report` 列 |
| `agents/<a>/rounds/<n>/*.json` | `agent_round` | 5 个文件合成 1 行 5 列 |
| `agents/<a>/memory.json` | `agent_memory` | |
| `agents/<a>/trace.jsonl` | `agent_trace` | 追加流 → 行 |
| `data/standardized/{m}.parquet` | `daily_bar` | **PG 成为事实来源**，parquet 降为物化缓存 |
| `data/standardized/quality_report.json` | `data_quality` | 按市场拆行 |
| `data/raw/*.parquet` | `artifact` | 原始数据可重下，只登记 |
| `refdata/securities.parquet` | `security_master` | |
| `refdata/relations.parquet` | `artifact`（暂） | 关系图暂不入库，后续可加 `security_relation` |
| `text/<m>/**` | `knowledge_chunk` + `artifact` | 文本切片入向量表 |
| `qlib_home/**` | **不迁移** | Qlib 只认二进制 |
| `rdagent/**` | **不迁移** | RD-Agent 工作目录 |
| `acceptance/**` | `artifact` | 验收报告 |
| `access/events.jsonl` | `access_event` | 保留哈希链 |
| `llm_costs.jsonl` | `llm_cost` | 追加流 → 行 |
| `reports/*.html` | `artifact` | |

### 4.4 需要改动的代码清单

**存储层（签名不变，换实现）**——9 个文件：

| 文件 | 函数 |
|---|---|
| `factopia/research/store.py` | `versions` `load_version` `save_version` `research_ids` `termination` `terminate` `create_jobs` `create_*_job` `write_job` `load_job` `list_jobs` `request_cancel` `cancel_requested` `write_live` `load_live` `pause` `paused` `resume` `resume_info` `resume_job` `retry_job` |
| `factopia/agents/store.py` | `create` `save` `load` `list_agents` `write_round` `read_round` `append_trace` `memory` `save_memory` |
| `factopia/approval/__init__.py` | `load` `_save` `events` `_append` `verify_chain` `list_all` `submit` `act` `handling` |
| `factopia/signals/__init__.py` | `events` `_append` `monitor_result` `catalog` `entry` `act` `monitor_entry` |
| `factopia/ideas/__init__.py` | `load` `_save` `events` `_append` `all_ideas` `_sources` `create` `act` |
| `factopia/portfolio/__init__.py` | `build`（落盘段）`load` `record_feedback` `feedback` |
| `factopia/paper/engine.py` | 状态读写、`_snapshot`、`orders` |
| `factopia/access.py` | `log` / `events` |
| `factopia/llm_cost.py` | 追加与汇总 |

**直接读文件的散点**（约 50 处，必须改）：`factopia/api/main.py`（33 处，含 `read_text` / `read_parquet` / `json.load`）、`factopia/diagnostics/__init__.py`、`factopia/rules/context.py`、`factopia/refdata/*.py`、`factopia/research/library.py`、`factopia/research/semantic.py`、`factopia/approval/__init__.py`（submit 里读 `run.json` / `rounds.json`）、`factopia/agents/{observe,tools,loop}.py`、`factopia/runner.py`、`factopia/acceptance/__init__.py`。

**新增**：`factopia/db/`、`factopia/repo/`、`migrations/`、`tools/migrate_files.py`、`tools/verify_migration.py`。

### 4.5 依赖与连接池

`pyproject.toml` 增加：`psycopg[binary,pool]>=3.2`。

选 psycopg3 而不是 SQLAlchemy + Alembic 的理由：这里的读写形态是"少量 SQL + 大量 JSONB UPSERT + 批量 COPY"，ORM 提供的对象映射用不上；迁移脚本是手写编号 `.sql`，比 autogenerate 更可控（尤其冻结件必须字节可控）。如果团队更习惯 Alembic，也可以只用它的版本管理，SQL 仍手写。

连接池按官方建议的 5–10 分配（总上限 30）：

| 进程 | 池大小 | 说明 |
|---|---|---|
| `api`（uvicorn，多 worker） | 每 worker 3–5 | 上限 10；只做短查询 |
| `runner` | 3–5 | 取任务、写状态、写 live |
| `exporter` | 2 | 批量写回测时序 |
| CLI / 一次性脚本 | 1 | |
| **峰值合计** | **≤ 15** | 留一半余量给 psql 排查与迁移工具 |

必须设 `statement_timeout`（API 15s、批处理 300s）与 `idle_in_transaction_session_timeout`（60s），避免慢查询把 30 个连接占满。

---

## 5. 迁移执行

### 5.1 五个阶段

| 阶段 | 内容 | 产出 | 完成判据 |
|---|---|---|---|
| **P0 连通性与基线** ✅ | 建 SSH 隧道；验证 pgvector；记录容量基线；备份现有 `runtime` 卷 | 连通性报告 | ✅ 隧道已通，向量算子 = 1，库大小 10 MB（第 8 节） |
| **P1 结构与工具** ◐ | 执行 `001_init_schema.sql` ✅ 已完成；实现 `tools/migrate_files.py`（按域解析 + 幂等写入）待做 | schema + 迁移工具 | ✅ 重复执行零变化；51 张逻辑表与 DDL 一致（第 8.2 节） |
| **P2 冷数据回填** | 迁移只读历史：`research` / `runs` / `approvals` / `ideas` / `signals` / `portfolio` / `agents` | 全量历史入库 | 逐域校验通过（见 5.3） |
| **P3 热路径切换** | 先切 `jobs`（并发问题最痛、写入者单一），再切 `research` / `approvals` / `paper` | 双写 → 读切换 | 影子读比对一致率 100%，持续 3 天 |
| **P4 二进制外置** | `artifact` 登记全部 L2 文件；`daily_bar` 改为从 PG 派生 parquet | 清单 + 校验 | `artifact` 行数与磁盘文件数一致，sha256 全对 |
| **P5 收尾** | 停文件写；保留只读副本 7 天；更新 README 与部署文档；接监控告警 | 生产切换 | 连续 7 天无回滚触发 |

**顺序理由**：P2 全是只读历史，零风险，先跑通工具链；P3 把 `jobs` 排在第一位，因为它是一个**单写者、高频、竞态明确**的域，切完立刻能观察到收益（不再有丢更新），且失败影响面小（任务可以重排）。

### 5.2 迁移工具的四个要求

1. **幂等**：`migration_file` 表按 `path` 记录源文件 sha256；重跑时 sha256 未变直接跳过。断点续传天然成立。
2. **按域可单跑**：`python -m tools.migrate_files --domain approvals --dry-run`，先出报告再落库。
3. **顺序敏感域按时间序**：事件流（审批、信号、线索、trace、llm_cost）必须按文件内的原始顺序写入，`seq` 连续。哈希链的 `prev_hash` 依赖前一条，顺序错了链就断。
4. **批量写用 COPY**：`daily_bar` 66 万行、`backtest_daily` 数万行用 `COPY ... FROM STDIN`，比逐行 INSERT 快一到两个数量级；写入前 `SET session_replication_role` 不需要——外键按拓扑顺序导入即可。

### 5.3 校验清单（每条都是一个断言，不是"看一眼"）

| 校验 | 方法 | 通过标准 |
|---|---|---|
| 行数 | 源文件解析出的对象数 vs 目标表行数 | 完全相等 |
| 字段完整性 | 抽样 20 条，源 JSON 与目标行逐字段比对 | 无缺失、无类型漂移 |
| **哈希链** | 在库内按 `seq` 重算 `hash`，串联 `prev_hash` | 与 `verify_chain()` 结果一致，`head` 相同 |
| 冻结件保真 | `materials` 的 JSONB 重新规范化后取 sha256 | 等于 `fingerprint` 列 |
| 数值精度 | 金额、收益率抽样比对（相对误差 < 1e-9） | 无 `NaN` 变 `NULL` 以外的差异 |
| 时序连续性 | `backtest_daily` 按 `(run_id,key)` 的日期序列 | 与 Parquet 日期数组逐项相等 |
| 外键完整性 | `run.research_id` → `research`、`job.run_id` → `run` | 无孤儿行（允许 NULL，不允许悬空） |
| 二进制 | `artifact` 的 sha256 vs 磁盘 | 全部一致 |
| 规模 | `pg_database_size` | < 1.5 GB（P2 完成时） |

**哈希链校验是最容易出问题的一环**：现有实现是 `sha256(json.dumps(ev_without_hash, sort_keys=True, ensure_ascii=False, default=str))`。迁移脚本必须复用同一函数，不能自己另写一套序列化——`ensure_ascii` 与 `sort_keys` 任一不同，哈希就完全不同。

### 5.4 数据迁移之外的两件事

1. **配置外置**：`configs/` 下的规则包、术语表、股票池保持文件（它们是**代码资产**，要进 Git、要 review，不是运行时状态）。但审批提交时会把规则包全文冻结进 `materials`，所以数据库也能独立回答"当时用的哪版规则"。
2. **时间点一致性不受影响**：`daily_bar.available_at` 列保留了"实际可获取时间"，这是系统做 PIT 校验的基础，迁移时不能丢。

---

## 6. 切流与回滚

### 6.1 三阶段切流

```
阶段 A（双写）     写：文件 ✓  DB ✓（异步，失败只记差异不阻塞）
                   读：文件
                   持续 3–5 天，每天比对差异清单，直到差异为 0

阶段 B（影子读）   写：文件 ✓  DB ✓（同步）
                   读：文件为主，DB 抽样比对（关键 12 个接口全量比对）
                   一致率 100% 连续 3 天 → 进入 C

阶段 C（切读）     写：DB ✓（文件停写，保留只读副本）
                   读：DB
                   观察 7 天
```

### 6.2 回滚

- **代码级秒回**：`FACTOPIA_STORAGE=files`，重启 api / runner。因为 `dual` 阶段文件一直是准的，回滚不丢数据。
- **保留窗口**：阶段 C 起 7 天内，文件写入改为"停写但不删"，卷保留快照。
- **不可逆点**：只有删掉 `runtime` 卷里的业务文件才是不可逆的——**这一步放在 P5 最后，且必须先做一次完整恢复演练**。

### 6.3 灰度顺序（按风险从低到高）

| 序 | 域 | 风险 | 理由 |
|---|---|---|---|
| 1 | `llm_cost`、`access_event` | 极低 | 纯追加，无读依赖 |
| 2 | `jobs` | 低 | 单写者，失败可重排 |
| 3 | `research_version` | 低 | 只新建不覆盖 |
| 4 | `runs` + 回测时序 | 中 | 数据量大，读点分散 |
| 5 | `approvals` | 中高 | 事务 + 哈希链 + 冻结件 |
| 6 | `signals` / `paper` | 高 | 周期性写入 + 派生视图 |
| 7 | `ideas` / `agents` | 中 | |
| 8 | `daily_bar` | 高 | 所有计算的输入，切换要配合数据管道 |

---

## 7. 部署与网络

### 7.1 连通性现状（已实测）

本机 DNS 解析 `pgm-2ze9t2uh39o1115k.rwlb.rds.aliyuncs.com` → `10.0.0.236`（VPC 内网地址），TCP 5432 **超时不可达**，与"仅向比赛 ECS 内网 IP 开放"一致。

本队 ECS `101.201.239.22:22` 可达（`OpenSSH_9.6p1 Ubuntu`），私网 `10.0.0.220` 与 RDS 的 `10.0.0.236` 同 VPC 同 /24，ECS → RDS 走内网直通。**隧道已打通并用于执行 DDL**。

#### 一个实战坑：`ssh -N -L` 在自动化环境里跑不起来

PostgreSQL 官方文档的写法需要交互输入密码，而 CI / 脚本 / 本仓库的 Bash 环境都是**非交互**的，无法给 ssh 的密码提示喂输入（本机也没有 `sshpass`）。因此改用 paramiko 完成认证并开 `direct-tcpip` 通道，链路与官方文档完全等价：

```
本机 ──SSH(公网,加密)──> ECS 101.201.239.22 ──VPC 内网──> RDS 10.0.0.236:5432
```

封装成了 [`scripts/db/pg_tunnel.py`](../scripts/db/pg_tunnel.py)，三种模式：

```bash
export FACTOPIA_SSH_PASSWORD='<ECS root 密码>'     # 口令只走环境变量，不进代码与命令行历史
export FACTOPIA_PGPASSWORD='<team12 密码>'

python scripts/db/pg_tunnel.py check       # 体检：版本/扩展/连接数/容量/权限
python scripts/db/pg_tunnel.py apply migrations/001_init_schema.sql
python scripts/db/pg_tunnel.py keepalive --local-port 15432   # 常驻隧道，给 psql/DBeaver 用
```

隧道在本机回环 `127.0.0.1:15432` 上监听，`.env` 里 `PGHOST=127.0.0.1`、`PGPORT=15432`。**用 15432 而不是 5432**，避免与本机可能已有的 PG 冲突。

- **ECS 上的容器**：直接连内网地址 `10.0.0.236`，不走隧道。
- **备选认证**：若不方便传口令，把 `~/.ssh/factopia_ecs.pub` 加进 ECS 的 `authorized_keys`，然后 `--ssh-key ~/.ssh/factopia_ecs`。
- **compose 配置**：新增 `x-db-env` 锚点，供 `api` / `runner` / `exporter` 复用；`app`（一次性 CLI）按需注入。

### 7.2 时区与时间

现状全部是 UTC ISO 字符串（`datetime.now(timezone.utc).isoformat(timespec="seconds")`）。迁移到 `timestamptz` 后行为不变，但要注意：

- 迁移脚本解析时**必须显式按 UTC**（`fromisoformat` 后 `astimezone(timezone.utc)`），否则本机时区（GMT+8）会让所有历史时间偏 8 小时。
- 会话 `TimeZone` 统一设 `UTC`，避免 psql 里看到的和 API 返回的不一致。

### 7.3 备份

| 层 | 手段 | 频率 | 保留 |
|---|---|---|---|
| RDS | 自动备份（物理） | 每日 | 7 天 |
| 逻辑 | `pg_dump -Fc --schema=factopia` → OSS | 每日 | 30 天 |
| L2 文件 | ECS 数据盘快照 或 OSS 同步 | 每日 | 7 天 |
| 演练 | 恢复到临时库 + 跑校验脚本 | 每月 | — |

8 GB 配额下 `pg_dump` 压缩后约 100–200 MB，成本可忽略。

### 7.4 监控（最小集）

- 连接数使用率（> 24 / 30 告警）
- 库容量（> 6 GB 告警，> 7 GB 严重）
- 慢查询（`pg_stat_statements` top 10，> 1s 记录）
- 队列积压（`state='queued'` 且 `created_at` 超过 10 分钟）
- 迁移差异数（`dual` 阶段专用，> 0 即告警）

---

## 8. 真实实例验证结果（2026-11-15 实测）

前几版遗留的"DDL 未经真实实例执行"这个缺口**已经关闭**。经本队 ECS 的 SSH 隧道连到 RDS 后，51 张逻辑表全部建成，7 项行为断言全绿，测试数据零残留。机器可读的体检报告见 [`docs/db-verify-report.json`](db-verify-report.json)，可复跑脚本见 [`scripts/db/`](../scripts/db/)。

### 8.1 环境实测

| 项 | 实测值 | 对方案的影响 |
|---|---|---|
| 服务端版本 | PostgreSQL **16.15**，UTF8 | 与方案假设一致；分区、`SKIP LOCKED`、HNSW 均可用 |
| 账号 `team12` | **非超级用户**，但有库级 `CREATE` | 能建 schema、建表、建触发器；不能建非 trusted 扩展 |
| `vector` 扩展 | **0.8.0.2，位于 `public`**，owner 是 `campaign_admin`（平台预装） | 直接可用，`CREATE EXTENSION IF NOT EXISTS` 幂等跳过 |
| `pg_trgm` 扩展 | 原库**没有**；由 DDL 创建，落在 `factopia` | 见 8.4 第 3 条：不要试图移动它的 schema |
| 向量算子 | `'[1,2,3]'::vector <-> '[1,2,4]'::vector` = **1** | 与赛方给的测试 SQL 一致 |
| 连接数 | 28 / 2420（集群级） | 集群上限很宽，但**队内 30 上限仍是真约束**，连接池仍按 5–10 设计 |
| 库容量 | 10 MB（其中 `factopia` 2.6 MB） | 空 schema 开销可忽略，8 GB 配额几乎全留给数据 |

### 8.2 DDL 执行结果

| 对象 | 数量 | 说明 |
|---|---|---|
| 表 | **51 逻辑**（54 物理） | 物理多出的 3 张是 `daily_bar` 的分区子表 `_hk` / `_uk` / `_fr` |
| 视图 | 4 | `v_job_overview`、`v_submission_latest`、`v_signal_monitor_latest`、`v_paper_position_value`，均可查询 |
| 索引 | 101 | 其中 DDL 中显式 `CREATE INDEX` 36 条，其余为 54 主键 + 5 唯一约束的隐式索引与分区继承索引 |
| 约束 | 主键 54 / 唯一 5 / 外键 29 / CHECK 10 | 血缘关系已由外键钉死，不再靠路径字符串 |
| 显式触发器 | **13** | 3 个只追加保护（`approval_event` / `signal_event` / `idea_event`）+ 10 个 `updated_at` 自动维护 |

**幂等性**：首次执行 0.22 s 建完全部对象；第二次执行 0.13 s，对象数零变化。DDL 可以安全地重复应用到同一个库。

### 8.3 行为断言（7/7 通过）

不是"看一眼"，每条都是可复跑的断言：

| 断言 | 结果 | 证据 |
|---|---|---|
| 分区路由 | PASS | `hk→daily_bar_hk`、`uk→daily_bar_uk`、`fr→daily_bar_fr`，按 `market` 正确分发 |
| 只追加约束 | PASS | 对 `idea_event` 执行 `UPDATE` / `DELETE` 均被触发器拒绝（"只追加表（审计留痕），不允许…"） |
| `updated_at` 自动前移 | PASS | 跨事务 `UPDATE` 后 `14:35:33.072 → 14:35:33.315` |
| 队列 `SKIP LOCKED` | PASS | 两个 worker 并发取任务，分别拿到 `smoke_j1` / `smoke_j2`，不存在重复领取 |
| pg_trgm 模糊检索 | PASS | `similarity()` 可解析（0.450），词相似度 `'momentum' <% title` 命中 |
| pgvector 距离检索 | PASS | 1024 维，自身距离 0.0000，HNSW 索引 `knowledge_chunk_vec_idx` 生效 |
| 数据零残留 | PASS | 测试行清理后为 0 |

### 8.4 实测暴露并已修复的三个缺陷

这三个都是**只有真连上库才会暴露**的问题，SQL 静态校验一个都发现不了：

1. **`similar` 是 PostgreSQL 保留字**。`factopia.idea` 里有个列叫 `similar`，建表直接语法错误。已改名为 **`similar_to`**（语义更清楚，顺带避开关键字）。
2. **`updated_at` 触发器从未挂载**。`tg_touch_updated_at()` 函数定义了，但全文没有任何 `CREATE TRIGGER` 引用它——10 张表的 `updated_at` 会永远停在 INSERT 那一刻，而这正是"最后更新时间"类查询的依据。已在 DDL 增加 11.1 节，按系统目录**动态**给所有带 `updated_at` 的表挂触发器，以后新增表不用再改这段。
3. **`pg_trgm` 不能 `SET SCHEMA`**。阿里云 RDS 会把 `CREATE EXTENSION` 建出的对象 owner 记为 `alicloud_rds_admin`（扩展 owner 才是 `team12`），因此 `ALTER EXTENSION pg_trgm SET SCHEMA public` 必然报 `must be owner of function set_limit`。结论：**扩展就让它留在 `factopia`**，别去移动；代价是应用连接必须显式带 `search_path`（见 8.6）。

### 8.5 实测新发现的两条硬约束（必须写进设计）

1. **`daily_bar` 没有 DEFAULT 分区**：只接受 `market ∈ {hk, uk, fr}`。新增市场（比如 `us`）时 `INSERT` 会**直接报错**（`no partition of relation "daily_bar" found for row`），不是静默丢弃。两种处理：加一个 `DEFAULT` 分区兜底，或在写入前做市场准入校验（推荐后者，脏数据会显形）。
2. **只追加触发器与 `ON DELETE CASCADE` 互斥**：`idea_event` 等子表禁止 `DELETE`，所以删除父行会被连带拦下——**整条链路无法物理删除**。这不是 bug 是设计后果（留痕不可改），但意味着：业务只能做状态软删；迁移脚本清理脏数据时也必须绕过父行删除。

### 8.6 应用连接必须设置 `search_path`

`pg_trgm` 在 `factopia` 而 `vector` 在 `public`，所以连接串必须两处都能看到：

```
host=127.0.0.1 port=15432 dbname=team12 user=team12
options=-c search_path=factopia,public
```

或在连接池初始化时执行 `SET search_path = factopia, public`。漏了这一步的表现是：表能查，但 `similarity()` / `gin_trgm_ops` 报 `function does not exist`。

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **哈希链迁移后校验失败** | 合规留痕失效，审计证据包不可信 | 复用原序列化函数；P2 阶段逐提交校验；失败即停 |
| **冻结件被"顺手规范化"** | 审计无法回答"当时是什么" | 定死规则：冻结件只进 JSONB，禁止拆分；评审清单逐条对照 |
| **误把逐股打分入库** | 3 GB+ 配额被吃光，且查询性能差 | 已在 2.2 明确排除；容量预算表作为门槛 |
| **`daily_bar` 切换期双源不一致** | 回测结果不可复现 | 切换时强制"PG → parquet"单向派生，不允许反向写 |
| **本机无法直连导致联调困难** | 开发效率低 | ✅ 已解决：`scripts/db/pg_tunnel.py` 三种模式，端口 15432 约定 |
| **8 GB 配额被临时排序打满** | 写失败 | `statement_timeout` + 分批 COPY（每批 5 万行）+ 监控告警 |
| **连接池打满** | API 5xx | 池上限 10；`idle_in_transaction_session_timeout`；慢查询治理 |
| **单点（一个 RDS）** | 故障即停服 | 比赛场景可接受；RDS 自动备份 + 恢复演练兜底 |
| **JSONB 膨胀（materials 累积）** | 表变大、备份变慢 | 300 KB × 200 次 = 60 MB，可控；超 500 次启动归档策略 |
| **时间时区偏差** | 时间线错乱 | 迁移脚本强制 UTC；会话 `TimeZone=UTC`；校验断言抽样比对 |
| **新增市场无分区可落**（实测） | 写入直接报错，整批任务失败 | 写入前做市场准入校验；或给 `daily_bar` 加 `DEFAULT` 分区兜底（8.5） |
| **留痕表禁止物理删除**（实测） | 脏数据清不掉、父行删不掉 | repo 层只提供软删；迁移工具的清理逻辑按业务键 `UPDATE` 而非 `DELETE`（8.5） |
| **连接漏设 `search_path`**（实测） | `similarity()` / `gin_trgm_ops` 报不存在 | 连接串固化 `options=-c search_path=factopia,public`，写进 DSN 模板（8.6） |

---

## 10. 工作量与里程碑（2 人）

| 阶段 | 内容 | 人日 |
|---|---|---|
| P0 | 隧道、连通性、容量基线、卷备份 | 1 |
| P1 | schema 评审与执行、`db/` 包、迁移工具骨架 | 4 |
| P2 | 9 个域的解析器 + 校验脚本（工作量主体） | 8 |
| P3 | repo 层实现 + 旧 store 转薄壳 + 30 处散点改造 | 10 |
| P4 | artifact 登记、`daily_bar` 派生链路、pgvector 接入 | 4 |
| P5 | 灰度切流、比对、回滚演练、文档 | 5 |
| **合计** | | **32 人日（约 3–4 周）** |

关键路径是 P2 的解析器与 P3 的 repo 层——两者可以并行：一人做历史回填，一人做运行时仓储层，接口以 4.3 的映射表对齐。

**建议的里程碑**：

1. **第 1 周末**：schema 上线，`jobs` 域在 `dual` 模式下双写，差异为 0。
2. **第 2 周末**：全部历史数据入库，校验清单全绿，影子读比对启动。
3. **第 3 周末**：api / runner 读全部切到 DB，文件停写，观察期开始。
4. **第 4 周**：pgvector 检索上线，文档收尾，卷归档。

---

## 11. 附：迁移后能直接做、但现在做不到的事

这些是判断"这次迁移值不值"的标准：

- **任务队列不再丢更新**：行锁 + `SKIP LOCKED`，多 runner 实例可并行。
- **信号目录 O(1)**：一次索引查询替代全目录扫描，页面加载不再随数据量变慢。
- **跨域一条 SQL 追溯**：线索 → 研究版本 → 运行 → 审批 → 信号 → 模拟盘，全链路 join 可查（目前靠文件名约定 + 人工拼）。
- **api 水平扩展**：不再受单卷约束。
- **语义检索召回率提升**：pgvector hybrid 检索，且保留命中词可解释性。
- **合规查询**：按规则编号统计"哪些研究版本对哪些规则做过例外"，现在是遍历所有 `submission.json` 才可能算出来。
- **数据质量趋势**：`data_quality` 按日入库，可直接画缺失率/异常跳变的时间序列。

---

## 12. 代码改造实施记录（research + jobs 域已落地）

方案原本是 9 个域、32 人日。这里先落地**基础设施 + 前两个域**（方案 P3 的第一步就是 `jobs`：
单写者、高频、竞态明确，切完收益立刻可见）。其余域按同一模式复制即可，不需要再设计。

### 12.1 已落地的东西

| 文件 | 作用 |
|---|---|
| `factopia/db/__init__.py` | 连接池（默认 1–5 条）、`tx()` 事务、`mode()/is_primary()/mirrors()` 灰度开关、`mirror()` 补写、`health()` 体检。DSN 里已固化 `search_path=factopia,public`、`statement_timeout`、`TimeZone=UTC` |
| `factopia/repo/__init__.py` | 类型适配：`iso()` 统一 UTC 秒级字符串、`as_dt()`、`fnum()`、`json_safe()`（NaN→None，否则 JSONB 写入报非法 JSON） |
| `factopia/repo/jobs.py` | 任务队列：列拆分 + `payload`、`upsert_job/load_job/list_jobs`、**原子 `claim_job`**、`recover_running`、`job_live`、暂停/取消、`job_resume` 同步 |
| `factopia/repo/research.py` | 研究版本：`versions/load_version/save_version/research_ids/termination/terminate/version_meta`，spec 整体进 JSONB + 规范化 sha256 |
| `factopia/research/store.py` | 原文件实现改名 `_file_*`，新增分发层；签名与返回形状**零变化**；新增 `ensure_version_file()`（给外部进程物化 YAML）、`claim_job()`、`recover_running()` |
| `factopia/runner.py` | `next_job()` 改为走 `store.claim_job()`；`recover()` 改为 `store.recover_running()`；子进程拿到的规格路径改为 `ensure_version_file()` |
| `tools/migrate_files.py` | 回填工具：按域幂等回填 + `migration_file` 书签断点续传 + `--dry-run` + `--verify` 比对 |
| `scripts/db/smoke_storage.py` | 21 项验收（三模式 + 并发领取 + 同市场串行 + 迁移幂等 + 物化） |

### 12.2 灰度开关

```bash
FACTOPIA_STORAGE=files        # 缺省，完全等同改造前，用于回滚
FACTOPIA_STORAGE=dual         # 写双份：文件为准 + 库里补写；读走文件
FACTOPIA_STORAGE=db           # 读写都走数据库
FACTOPIA_STORAGE_JOBS=db      # 按域覆盖（域名为大写模块名）
FACTOPIA_STORAGE_RESEARCH=db
```

推荐上线顺序：`files` → 只开 `FACTOPIA_STORAGE_JOBS=dual` 观察差异 → `JOBS=db` → 再切 `research`。

### 12.3 实测结果（21/21 通过，0 残留）

`python scripts/db/smoke_storage.py`（需隧道在 15432）：

| 组 | 断言 |
|---|---|
| files | 写文件、库无记录、`list_jobs` 读文件 —— 老路径不回归 |
| dual | 文件写入 + 库里补写、读仍走文件、任务双写 |
| db | 版本递增、**不再产生新文件**、读回内容与类型一致（UTC ISO 字符串） |
| 并发 | 三个 worker 领到三个互不相同的任务；同市场占用时不再领取 |
| 取消/暂停 | 请求可读、暂停可写可恢复 |
| 物化 | db 模式下外部进程仍能拿到 YAML 文件 |
| 迁移 | 回填 5 个文件 → 二次执行全部书签命中 → `--verify` 全部一致 |

另外 `pytest tests/` 从 20 个失败降到 **45 passed / 3 skipped / 1 failed**：剩下的 1 个是既有问题
（测试硬编码容器路径 `/workspace/configs/access/regions.yaml`），20 个原始失败是 `os.geteuid()`
在 Windows 不存在，已加 `hasattr` 守卫修掉。

### 12.4 实施中踩到的坑（都会在其余域复现，务必注意）

1. **`FOR UPDATE SKIP LOCKED` 的锁范围**。候选查询写 `… ORDER BY created_at LIMIT 20 FOR UPDATE
   SKIP LOCKED` 会把返回的**每一行**都锁住，三个 worker 并发时后到者候选集被前两者全 SKIP 掉，
   表现为"三个 worker 只领到两个"。正确做法：候选查询不加锁，用
   `UPDATE … WHERE job_id = ? AND state = 'queued'` 的条件更新做原子夺取（rowcount=0 即被抢）。
2. **`kind='research'` 的缺省语义**。表里 `kind` 有缺省值 `'research'`，但文件时代的任务 JSON
   里**没有 `kind` 键**，而 `resume_info()` 用 `if job.get("kind")` 判断"是不是特殊任务"。
   若把 `'research'` 原样带出，**所有 RD-Agent 研究任务的断点续跑会被静默禁用**。
   已在读取时还原：`kind == 'research'` 视为没有 kind。
3. **外键顺序**。`research_version` 有指向 `research` 的外键（RESTRICT），必须先写主表再写版本，
   否则第一次 `save_version` 就 FK 违例。
4. **书签路径的平台差异**。Windows 下 `str(path)` 是反斜杠，`LIKE '<posix 前缀>%'` 永远匹配不到，
   导致清理/重跑逻辑失效。统一用 `Path.as_posix()` 作为书签键。
5. **外部进程只认文件路径**。RD-Agent 通过 `FACTOPIA_RESEARCH` 拿 YAML 路径，db 模式下必须物化，
   否则子进程拿到一个不存在的路径。物化件是**派生物**，删掉会自动重建。

### 12.5 还没做（按优先级）

1. ~~**其余 7 个域**~~：已完成，见第 13 节。
   套路完全一样：`repo/<域>.py` + store 里加分发层 + 迁移工具加一个 `migrate_*_file`；
   其中 `approval`/`signals`/`ideas` 要处理哈希链（`seq/prev_hash/hash`）与只追加表。
2. **API 散点**：`factopia/api/main.py` 仍有约 33 处直接 `read_text`/`read_parquet`/`json.load`，
   切到 `db` 模式前必须逐个改成 store 调用，否则页面会读到旧文件。
3. **artifact 与大体量域**：`daily_bar`、`backtest_*`、`run_*`、`knowledge_chunk`（向量）。
   这些涉及 parquet 列映射与 COPY 批量写入，建议单独一轮做。
4. **`verify_migration.py`**：目前 `--verify` 只覆盖 research/jobs，扩到其余域后才有全量对账。

---

## 13. 代码改造实施记录（其余 7 个域已落地）

第 12 节落地了基础设施与 `research`/`jobs`。本节是**剩余 7 个域**：`approval`、`signals`、
`ideas`、`agents`、`portfolio`、`paper`、`access/llm_cost`。套路与第 12 节完全一致——
`repo/<域>.py` 写列映射与 SQL，原文件实现改名 `_file_*`，domain 模块加 files/dual/db 三分发，
**对外签名与返回形状零变化**，所以 API、runner、exporter 的调用面基本不用动。

### 13.1 新增与改动

| 文件 | 作用 |
|---|---|
| `migrations/002_payload_columns.sql` | 给 `idea`/`agent`/`agent_round`/`paper_enrollment` 补 `payload jsonb`，兜住 001 没建列的字段（`dedupe_key`、`document`、`draft_action`、`price_dataset` …）。已在真实 RDS 执行：表 54→54、索引 101→101、触发器 129→129（纯加列） |
| `factopia/repo/__init__.py::chain_hash()` | 三条链（审批 / 信号 / 线索）在文件时代各写了一遍同样的三行；合并成一份实现，**迁移时靠它证明"库里的历史没被改写"** |
| `factopia/repo/approval.py` | 提交 + 步骤 + 决定 + 事件；`save_full()` 迁移专用（`seq` 按文件顺序，`hash` 原样） |
| `factopia/repo/signals.py` | 全局链（`bigserial` seq + `advisory_xact_lock` 串行）、`save_monitor`、`latest_monitors` |
| `factopia/repo/ideas.py` | 每条线索一条链；`idea_source` 主键判重 → `DuplicateIdea`；`save_full()` 迁移专用 |
| `factopia/repo/agents.py` | 状态 + 轮次（5 个正列 + `payload`）+ 记忆 + 留痕 + 报告列 |
| `factopia/repo/portfolio.py` | `summary` 整体 JSONB（五列只是查询投影）+ 逐日曲线 + 逐股权重 + 反馈 |
| `factopia/repo/paper.py` | 登记 / 当前态 / 订单（一单一行）/ 净值 / 持仓快照 / 运行报告 |
| `factopia/repo/ops.py` | 访问留痕全局链 + 大模型费用流水与汇总 |
| `factopia/{approval,signals,ideas}/…` | `_file_*` 改名 + `_persist/_persist_sources/save_monitor` 三分发 |
| `factopia/{agents/store.py,portfolio/__init__.py,paper/engine.py,access.py,llm_cost.py}` | 同上；`paper/report.py`、`agents/report.py`、`api/main.py` 改为走访问器，不再自己拼路径 |
| `tools/migrate_files.py` | 从 2 个域扩到 **9 个域**，新增 7 个 `iter_*`/`migrate_*`，`--verify` 覆盖全部域 |
| `scripts/db/smoke_domains.py` | 新增 29 项验收（见 13.5） |

### 13.2 三条哈希链：结构不同，处理方式也不同

| 链 | 结构 | 并发控制 | 迁移 |
|---|---|---|---|
| `approval_event` | **每份提交一条**（`PRIMARY KEY(submission_id, seq)`） | `SELECT … FOR UPDATE` 锁提交行，`MAX(seq)+1` | `save_full()`：`seq` = 文件里的第 i 条 |
| `idea_event` | **每条线索一条** | 同上，锁 `idea` 行 | 同上 |
| `signal_event` | **全局一条**（`bigserial`） | `pg_advisory_xact_lock(hashtext('factopia.signal_event'))`——链只有一个头，只能串行 | 按文件顺序逐条追加 |
| `access_event` | **全局一条**（`bigserial`） | 同上 | 同上 |

三条链共同的三条铁律：

1. **`at / prev / hash` 逐字照抄，绝不重算**。`exact=` 参数就是干这个的：dual 模式下把
   *文件算好的那条事件*原样补写，两份存储的 hash 逐字相同，切流时链条不断。
2. **只追加表不许改**。`tg_forbid_mutation()` 触发器对 `UPDATE`/`DELETE` 直接抛异常（错误文案
   是"请追加一条纠正事件，不要改动历史记录"），三个域都已实测拦住。
3. **读回必须能重算出同一个 hash**。列名与文件字段不是一一对应的，读回时要反向映射，
   否则 `chain_hash` 结果会变，审计会认为证据被动过：

   | 文件字段 | 列 | 备注 |
   |---|---|---|
   | `by` | `actor` | 审批 / 线索 |
   | `role` | `view` | 访问留痕 |
   | `role_name` | `actor` | 访问留痕 |
   | `denied` | `resource` | 访问留痕 |
   | `market` | `ref_id` | 访问留痕 |

   另外 `NULL` 与"键不存在"必须区分：`market`/`step`/`rule` 为空时读回**不输出该键**
   （`_merge_event` 里的 `if value is not None`），否则多一个 `"market": null` 就会改变哈希。

### 13.3 只在"有库"时才成立的建模决定

- **`portfolio.summary` 整体进 JSONB**。它是冻结件（审批要引用当时的组合），规范化会破坏
  "当时就是这么写的"；`built_at/base_currency/period/metrics/attribution/fx_split/constraints`
  六列只是查询投影，正文永远是 `summary`。
- **`paper_order` 一单一行，`orders.jsonl` 的事件流在这里被折叠**。`placed` 建行，
  `filled`/`cancelled` 改行；`order_events()` 再还原成事件流给报告用。
  **没有单号的"空单"（券商没返回单号 / 按手数取整后为 0）落成 `status='void'`**：
  它既不是挂单（不该进 `pending`，否则每次读当前态都多一张幽灵挂单），也不是成交
  （不该进成交笔数），但必须留一行——否则报告里"下了多少单"永远对不上。
- **`state.pending` 不单独存表**，由 `paper_order.status='pending'` 推导，避免两份真相。
- **`agent_round` 五个正列 + `payload`**：`facts/diagnosis/plan/action/evaluation` 进列，
  其余轮次文件（`draft_action`、`plan_critique`、`external`、`next_version_draft`、`approval`）
  进同一行的 `payload`，一行一轮。
- **`idea_source.dedupe_key` 是主键**，"同源只允许一张卡片"从"读-改-写"变成了一次插入：
  并发第二个写入者拿到 `DuplicateIdea`（携带先到者编号），调用方读回先到者即可。

### 13.4 迁移工具的变化

```bash
python tools/migrate_files.py --domain all --dry-run
python tools/migrate_files.py --domain approval|signals|ideas|agents|portfolio|paper|ops
python tools/migrate_files.py --verify
```

- **一个目录算一份**：`agents/<aid>/` 与 `paper/<sid>/` 有多份文件，书签挂在目录上，
  指纹是"相对路径 + 每个文件 sha256"的树指纹（`tree_digest`）。只盯主文件会漏掉轮次或快照的改动。
- **幂等靠三重兜底**：书签（sha256 没变就跳过）→ 事件表的 `ON CONFLICT DO NOTHING` /
  `WHERE NOT EXISTS`（迁移半途中断重跑不会重复）→ 主表 UPSERT。第二轮实测 100% 书签命中。
- **`--verify` 会重算整条链**，并与文件逐条比 hash 前缀：`dual`/`db` 模式下库会比文件新
  （切换后的事件只进库），所以要求"文件是库的前缀"，而不是逐条相等。

### 13.5 实测结果（29/29 通过，0 残留）

`python scripts/db/smoke_domains.py`（需隧道在 15432；自带夹具与清理）：

| 组 | 断言 |
|---|---|
| 回填 | 七域入库：审批 3 条事件、信号 2 条、线索 1 条、访问留痕 2 条、Agent/组合/模拟盘各 1 |
| 幂等 | 二次执行全部"书签命中"，没有重复写入 |
| 对账 | `--verify` 全部一致（含条数、状态、指纹、曲线行数、成交笔数、费用金额） |
| 哈希链 | 审批/信号/线索/访问四条链**逐条重算 hash 与原文件一致**，首条 `prev` 为 64 个 0 |
| 只追加 | `approval_event`/`signal_event`/`idea_event` 的 `UPDATE` 与 `DELETE` 全部被拒 |
| 续链 | db 模式追加的下一条 `prev` == 上一条 `hash`；库内 `verify_chain()` 自洽 |
| 去重 | 两个线程抢同一个 `dedupe_key`：1 个成功、1 个 `DuplicateIdea`，且能读到先到者编号 |
| 其它 | 空单不进 `pending`、净值天数一致、轮次/报告可读写、逐日曲线列齐、权重按市场归一、费用汇总口径不变 |

同时：

- `pytest tests/` → **46 passed / 3 skipped**（Windows 下需带 `FACTOPIA_CONFIGS_DIR` 与
  `FACTOPIA_RUNTIME` 才跑得起来，是既有环境依赖，不是本轮引入）。
- 仓库里的 demo 数据（`runtime/` 下的 e2e 一次运行）回填后 `--verify` **全部一致**。

### 13.6 本轮踩到的坑（都会在别的域复现）

1. **外键顺序写反就炸**。`idea_source.idea_id` 指向 `idea`，先占唯一键再插线索必 FK 违例。
   正确顺序：**先落线索、后占唯一键**；占位失败时抛异常让整个事务回滚，刚才那条线索也跟着消失，
   不会留下孤儿卡片。
2. **`DataFrame.reset_index()` 会凭空多一列**。`build()` 的 `daily` 以 `date` 为索引，
   而 parquet 里 `date` 是普通列；无条件 `reset_index()` 会把 `index` 混进 `payload`，
   读回来就多一个莫名其妙的列。改成 `if daily.index.name == "date"` 才展开。
3. **"空单"不能当挂单**。落成 `pending` 会让 `load_state()` 每次返回一张幽灵挂单
   （文件时代的 `pending` 里没有它）。用 `status='void'` 单独标记，并在 `pending` 查询里排除。
4. **留痕表是只追加的，测试清理会被拦**。`DELETE` 一律被触发器拒，连 `ON DELETE CASCADE`
   也会被拦。验收脚本用 `DISABLE TRIGGER → DELETE → ENABLE TRIGGER` 显式绕开，
   **且只删本脚本造的行**（按编号前缀）——第一版图省事清了整张事件表，把别的批次的留痕也删了，
   只能从文件重放恢复。生产路径永远不该这么干。
5. **`access_event` 的文件字段里没有 `allowed`**。多写一个键，读回时就少一个键，
   重算 hash 立刻对不上。列可以多（`allowed` 由"只记拒绝"推导），但**写进哈希的键必须与文件一致**。
6. **幂等要为"重跑"设计，不能只为"正常路径"设计**。`append_trace` / `record_cost` 原本是纯追加，
   迁移重跑会翻倍；现在带 `WHERE NOT EXISTS` 的判重（费用那条只在 `dedupe=True` 时开启，
   因为运行时同一秒内两次同长度调用是两笔钱，不能合并）。

### 13.7 还剩什么

1. ~~**API 散点收尾**~~：已完成，见第 14 节。
2. **大体量域**：`daily_bar`（含 hk/uk/fr 分区子表）、`backtest_*`、`run_*`、`knowledge_chunk`（向量）。
   涉及 parquet 列映射与 COPY 批量写入，单独一轮做。
3. **`exporter`**：导出证据包时仍有文件直读（`evidence_zip` 已改为从事件重建，其余待查）。
4. **灰度推进**：建议顺序 `files` → `FACTOPIA_STORAGE_SIGNALS=dual` 观察 → `db`；
   留痕类域风险最低（只追加、低频），`paper` 风险最高（有状态机与资金口径），最后切。

## 14. 代码改造实施记录（API 直读清零）

### 14.1 为什么这件事优先级高于"再迁一个大表"

切换 `FACTOPIA_STORAGE=db` 时唯一不会报错的故障，是**页面照常有数字，但那是上一次运行留下的文件**。
`api/main.py` 里原本有 25 处 `read_text` / `read_parquet` / `json.load` 自己拼路径，
再算上 `run.json` / `rounds.json` / `events.json` 这类散落的字符串拼接，实际有 30 多处。
每多一处，切流的可信度就少一分——因为你没法用"跑起来没报错"证明它真的读库了。

这一轮的目标不是"把所有东西塞进 PG"，而是让 **API 不再知道数据在哪**：
路径知识集中在 `runstore` / `blobstore` 两个入口里，API 只调函数。

### 14.2 三类直读，三种处理

| 类别 | 判据 | 处理 | 例子 |
|---|---|---|---|
| **A. 已有第二份真相** | 库里/表里有同一份数据 | 必须读库，**db 模式不回落文件** | `run.json`、`rounds.json`、`events.json`、`validation/*.json`、`quantile/*.json`、`explain_v*.json`、组合 `daily.parquet`、生成的 HTML 报告、`pit_status.json`、refdata 摘要 |
| **B. 尚未入库，文件是唯一真相** | 没有第二份副本 → 不存在"读到旧的" | 仍读文件，但只经一处入口 | `backtest/signal/score/positions` 逐日 parquet、`alpha158/*.json`、refdata 的 `securities/relations/corporate_actions`、文本数据集、任务日志 |
| **C. 静态配置** | 跟代码走，不是运行时数据 | 读文件，包一层 `config_*` | `configs/research/*.yaml`、`configs/rdagent/context/*.md`、`configs/refdata/*.yaml`、`configs/universe/**` |

**关键判断在 B 类**：只有当同一份数据存在两个位置时，"读到旧的"才可能发生。
`backtest/*.parquet` 由 RD-Agent 直接写盘，没有第二份，读文件就是读真相；
把它拆列存进 `backtest_daily` 反而会做出一份"列猜错就丢数据"的副本
（列结构随 RD-Agent 版本变化）。所以这些**留在文件侧，但访问入口唯一化**。

### 14.3 新增模块

| 文件 | 职责 |
|---|---|
| `factopia/repo/runs.py` | `run` / `run_round` / `run_factor` / `run_event` / `run_validation` / `run_quantile` / `run_diagnostic` / `run_report` 的读写 |
| `factopia/runstore.py` | 运行产物的唯一入口；`files/dual/db` 三分支；**db 模式不回落文件** |
| `factopia/repo/misc.py` | `app_state` 单例键值（pit、质量报告、refdata 摘要、数据商导入）+ `report_file` |
| `factopia/blobstore.py` | 零散产物的唯一入口：快照 / HTML 报告 / 尚未入库的派生表 / 静态配置 |
| `migrations/003_api_scatter.sql` | 补 `research_version.explain`、`run.payload`、`run_round.payload`、`run_report`、`run_diagnostic`、`report_file` |

`runstore` 的 db 分支长这样，是这一节的核心决定：

```python
def load(run_id):
    if db.is_primary(DOMAIN):
        got = repo_runs.load(run_id)
        if got is not None or db.mode(DOMAIN) == "db":
            return got          # db 模式：没有就是没有，绝不偷偷读文件
    return _file_json(run_dir(run_id) / "run.json")
```

`dual` 会回落（灰度期两边同步写，回落是安全的），`db` 不回落——**让"没迁移"立刻以 404 暴露，
而不是以"数字有点旧"潜伏**。

### 14.4 形状保真

`run.payload` / `run_round.payload` 沿用 002 的思路：随 RD-Agent 版本增删的键整体进 JSONB，
读回时「列值 + payload」合并，`research` 由 `research_id`/`version` 还原成嵌套字典。
`rounds.json` 里的 `factors` 拆到 `run_factor` 后读回时重新挂上，因此
`/api/runs/{id}` 的返回与文件时代逐字一致（这一条由验收脚本逐端点比对保证）。

### 14.5 实测

`scripts/db/smoke_api_runs.py`——同一批只读端点，三个阶段：

1. `files` 模式采样（基线）；
2. `db` 模式采样，与基线**逐字节**比对：13/13 一致；
3. `db` 模式 + **把 `RUNS_DIR` / `REPORTS` 指向空目录**，只比运行类端点：8/8 一致。

第 3 阶段是"切了就是切了"的硬证据：文件全不在，页面仍拿到同一份数据。
任何一处偷偷回落文件，这一阶段必然对不上。

同时 `pytest tests/` 46 passed / 3 skipped，`scripts/db/smoke_domains.py` 29/29 通过（含审批域改用
`runstore.rounds()` 后的回归）。

### 14.6 坑

1. **`ON CONFLICT ... DO UPDATE SET` 在"只有一个列"时会拼出 `SET , payload = ...`**。
   `run.json` 极简时（只有 `run_id/market/horizon/settings`）会触发；把 `payload` 放进同一个列表再 `join` 才稳。
2. **psycopg 不会自动把 `dict` 适配成 `jsonb`**，`settings`/`baseline`/`metrics`/`sota_*`/`feedback`/`steps`
   都必须显式 `Jsonb(...)`，否则报 `cannot adapt type 'dict'`。
3. **`run_diagnostic` 的键是 `baseline` 而不是数字**——诊断目录里除了轮次号还有 `baseline.json`，
   主键定成 `(run_id, key text)` 而不是 `loop int`。
4. **`_read_json` 要容忍文件不存在**：`runs/<id>/` 里 `events.json` / `rounds.json` 经常没有，
   直接 `read_text` 会让整个域迁移失败在一个"本来就没这个文件"上。
5. **迁移与校验不能走分发层**。回填时若环境是 `files`，`runstore.save()` 会什么都不做却照样记书签，
   造成"标记已完成、实际零行"。回填/校验一律直连 `repo_*`。
6. **报告双写要按 mode 分支**：`files` 落盘、`dual` 两边、`db` 只入列。
   早写成 `if not db.is_primary()` 会把 `dual` 的落盘也跳掉。

### 14.7 还剩什么

> 本节列的 4 项已在第 15 节全部处理完，保留原文以对照当时的判断。

1. **`agents/loop.py`、`agents/observe.py`、`children.py`** 仍直接读 `runs/<id>/run.json` 等。
   它们跑在**产出侧进程**里（文件刚写完，不存在"旧"的问题），且改动面涉及 Agent 决策链，
   单独一轮处理更安全。切 `db` 前后都不影响页面正确性。
2. **大体量域**：`daily_bar`（含分区子表）、`backtest_*` 的逐日表、`knowledge_chunk`（向量）。
   B 类里的 parquet 若要入库，就是这一轮的工作。
3. **`refdata` 域**（`security_master` / `index_membership` / `corporate_action`）尚未接入，
   目前 `blobstore.refdata_table()` 仍读 parquet；P3 改为从表派生时只换这一个实现。
4. **`exporter`** 导出证据包时仍有文件直读（`evidence_zip` 已从事件重建，其余待查）。


## 15. 收尾：参考数据、产出侧、导出器与行情入库

第 14.7 节列的 4 件事全部落地。这一轮的顺序是刻意的：先补两类"有第二份真相但还在读文件"的
（refdata、产出侧 run.json），再管"只有一份真相"的（exporter 的中间产物），最后才碰大表。

| 第 14.7 节遗留 | 状态 | 落地方式 |
|---|---|---|
| `agents/{loop,observe,children}` 直读 run.json | 完成 | 全部改走 `runstore`；诊断产物补 `run_diagnostic` 双写 |
| `daily_bar` / `backtest_*` / `knowledge_chunk` | 部分完成 | `daily_bar` + `fx_rate` 入库（COPY）；`backtest_*` / `knowledge_chunk` **决定不入库**，理由见 15.4 |
| `refdata` 域接表 | 完成 | 新增 `security_relation` 表；`security_master` / `index_membership` / `corporate_action` 补列；`blobstore.refdata_table()` 改为从表派生 |
| `exporter` 剩余直读 | 完成 | `run.json` / `rounds.json` / `quality_report` 改走 `runstore` / `blobstore`；量化结果补写 `run_quantile` |

### 15.1 refdata：四张表，四个"读回必须一致"的约束

原状是 `refdata/*.parquet` 与 `configs/universe/membership/*.csv` 为唯一真相，API 直接 `read_parquet`。
接入后有 4 张表对应这 4 份文件，**读回必须与文件逐值一致**——否则页面上的证券/公司行动会静默变样。

| 文件 | 表 | 幂等方式 |
|---|---|---|
| `securities.parquet` | `security_master` | `ON CONFLICT (symbol)`，全量重建时按缺失 `prune` |
| `relations.parquet` | `security_relation`（**新表**） | `ON CONFLICT (issuer_id, venue, ticker)` |
| `corporate_actions.parquet` | `corporate_action` | `ON CONFLICT (symbol, ex_date, action)` |
| `universe/membership/<m>.csv` | `index_membership` | 按 `index_code` 整块替换 |

三个设计决定：

1. **`payload` 优先，列只补缺失键**。这几份表由开放数据源（Yahoo / OpenFIGI / GLEIF / AMF）拼出来，
   列会随数据源改版增删。所以列只放"要查、要排、要约束"的字段，其余整行原样进 `payload`；
   读回时先取 `payload`，列值只 `setdefault`。这样新增字段不用改表，也不会因为列截断而丢值。
2. **原值为 `None` 也要留在 `payload` 里**。`issuer_id=None` / `ticker=None` 落成列会变 `''`，
   读回就分不清"未知"与"空"。凡是 null 都保留在 `payload`，读回才还原得回去。
3. **`ord` 列记住原文件行序**。表本身无序，而 `securities` 的顺序就是股票池顺序、
   `relations` 的顺序是生成顺序。少了这一列，`GET /api/refdata/securities` 在两种模式下
   返回的列表顺序不同（值一样，但逐字节比对会失败，前端表格首屏顺序也会变）。

`migrations/004_refdata_tables.sql` 新增 `security_relation` 表、给三张表补 `long_name` /
`fiscal_year_end_month` / `issuer_id` / `note` / `ord` 列，并给 `corporate_action` 加
`(symbol, ex_date, action)` 唯一索引（让"重导同一份数据商文件"变成 UPSERT）。
真实库执行后：表 57→58，索引 105→110。

### 15.2 产出侧：Agent 读的不是"文件"，是"运行"

`agents/loop.py`、`agents/observe.py` 原来自己拼 `results/runs/<id>/run.json`。
这看起来无害（文件是刚写的），但只要切换过 `FACTOPIA_STORAGE_RUNS=db`，
Agent 的决策证据就来自另一条路径——**页面读库、Agent 读文件**，两边对同一个运行的判断可能不同。

改成 `runstore` 之后，`observability` 里 5 处直读（rounds / diagnostics / quantile / validation / 逐日打分）
全部归一。逐日打分仍然读 parquet（B 类），但入口是 `runstore.signal()`，将来入库只改这一个函数。

顺带修了一个真 bug：`diagnostics.diagnose_run()` 只写文件、不入库。
db 模式下它跑完，页面永远看不到新诊断（`runstore.diagnostics()` 只读库）。
现在按 mode 分支：`files/dual` 落盘，`dual/db` 写 `run_diagnostic`。

### 15.3 导出器：中间产物留文件，结构化结果入库

| 位置 | 处理 |
|---|---|
| `export/rdagent_run.py` 写 `run.json` / `rounds.json` / `events.json` | 文件照写（backtests 要按目录找 parquet），同时 `runstore.save()` 入库 |
| `export/backtests.py` 读 `run.json` / `rounds.json` | 改 `runstore.load()` / `runstore.rounds()`；`quantile/*.json` 补写 `run_quantile` |
| `export/backtests.py` 的 `main()` | 运行清单改 `runstore.all_runs()`（db 模式下 `run.json` 可能不在文件里） |
| `export/alpha158.py` 读 `quality_report.json` | 改 `blobstore.quality()`（A 类：库里有第二份，必须读库） |

### 15.4 行情入库，以及两张**决定不入库**的表

`daily_bar`（LIST 分区，hk/uk/fr 三张子表）+ `fx_rate` 是本轮唯一的大表。
写入路径只有一条：**DataFrame → 临时表（COPY 文本）→ `INSERT … ON CONFLICT DO UPDATE`**。
逐行 `executemany` 在三百万行规模上是不可用的（每行一次往返），而 COPY 不支持 UPSERT，
所以必须借临时表中转。读侧是 `factopia/barstore.py`：按市场 + 日期区间 + 股票集合裁剪返回列。

新增 `factopia/repo/bars.py` / `factopia/barstore.py`，并把 8 处直接读
`data/standardized/<market>.parquet` 的调用点（`sources` / `paper.engine` / `refdata.actions` /
`refdata.membership` / `refdata.vendor` / `data.to_qlib` / `text.signal`）全部改走 `barstore`。

**与别的域不同的一点**：`barstore.save_bars()` **始终**落一份 parquet。
理由是行情的第二类用法是"要一个文件喂外部工具"（`data.to_qlib` 生成 qlib 二进制），
而 parquet 是纯派生物（删了能从库重建）。所以写永远双份，**读在 db 模式只读库**。

两张表**刻意不迁**，写进 `migrations/005_reserved_tables.sql` 的表注释里：

* `backtest_daily` / `backtest_position`：RD-Agent 每轮的逐日产物，文件是唯一真相，
  列结构随 RD-Agent 版本变化。拆成固定列 = 用猜的 schema 复制一份，猜错就静默丢数据。
  页面与计算经 `runstore.backtest()` 读文件，不存在"两个位置不一致"。
* `knowledge_chunk`：hybrid 检索要用 pgvector，但当前 `text.search` 的召回由概念标注决定，
  不依赖向量。迁进去只是"同一份文本换个地方存"，检索不会变好；接之前得先定 embedding 模型
  与批量重算策略（P4）。表与 HNSW 索引已就绪。

判断依据仍是第 14.2 节那一条：**只有同一份数据存在两个位置时，"读到旧的"才可能发生。**

### 15.5 五个体量/顺序上的坑

1. **分区键大小写**。`standardize` 产出的 parquet 里 `market` 是 `HK`，而 `daily_bar` 是 LIST 分区、
   只接受 `hk`/`uk`/`fr`。写库前必须 `lower()`，否则不是"少几行"而是直接
   `no partition of relation found`。（这条靠 `smoke_bars.py` 里一条"表里没有大写分区键"的断言守住。）
2. **`psycopg3` 的 `copy()` 在 Cursor 上，不在 Connection 上**。`db.tx()` 给的是 Connection，
   要 `cur.cursor().copy(...)`，否则 `AttributeError: 'Connection' object has no attribute 'copy'`。
3. **COPY 里空串 ≠ NULL**。只有 `\N` 是 NULL；`dq_flag=''` 表示"没有标记"，写错就变成"未知"。
   所以没有用 `to_csv`（空串与 null 都是空字段），而是自己按 PG 文本格式转义。
4. **局部重建不能按缺失删除**。`master.build(symbols=[...])` 是局部刷新，
   若 `prune` 按"文件里没有就删"执行，会把没参与本次重建的标的全清掉。
   `prune` 只在 `symbols is None`（全量重建）时为真。
5. **验收夹具必须用假代码、且不能"整块替换"真实范围**。第一版 `smoke_refdata.py` 用真实代码
   `0005.HK` / `HSBA.L` 当夹具，清理时按主键删；成分股那步更危险——`save_membership("hk", 2 行夹具)`
   会把真实 85 行整块替换掉。现在夹具一律 `T` 前缀 + 假市场 `zz`，清理按精确主键，
   并在最后断言"真实 hk 名单仍在"。

### 15.6 实测

| 验收 | 结果 |
|---|---|
| `scripts/db/smoke_refdata.py` | **16/16**（往返逐值、幂等、空 refdata 目录、三个接口、零残留） |
| `scripts/db/smoke_bars.py` | **13/13**（往返、分区键、时间戳、空串、幂等、空 standardized 目录、汇率） |
| `scripts/db/smoke_api_runs.py` | **13/13 端点两种模式一致**；结果卷清空后 8/8 |
| `scripts/db/smoke_domains.py` | 29/29 |
| `scripts/db/smoke_storage.py` | 21/21 |
| `pytest tests/` | 46 passed / 3 skipped |
| `tools/migrate_files.py --verify` | 12 个域全部一致（含 refdata 四张表、`daily_bar` 5300 行末日抽样） |

`dual` 模式的一个遗留缺口也顺手补了：`runstore` / `blobstore` 的 `save_*` 原来只在 `db` 模式写库，
`dual` 只写文件——库里永远追不上文件，等于 dual 什么也没验证。现在统一走
`db.write_db(domain, fn, what)`：`db` 直写并抛错、`dual` 补写且只记日志。

### 15.7 还剩什么

1. **`knowledge_chunk`**（P4）：定 embedding 模型 → 批量索引 `text` 数据集段落 → `text.search` 改用
   `lexemes && query` 召回 + HNSW 近似检索。表、GIN 索引、HNSW 索引都已就绪，缺的是索引作业。
2. **`artifact`**（P3）：给留在文件系统的 parquet / qlib 二进制 / 证据包登记 sha256 与字节数，
   用于"迁移后逐字节校验"和清理归档。现在这些文件没有户口，只能靠路径约定。
3. **灰度推进**：`files` → `FACTOPIA_STORAGE_<域>=dual` 观察一周（`--verify` 每日跑）→ `db`。
   建议顺序：`refdata` / `bars`（只读、可重建）→ `runs` / `misc` → `approval` / `signals` / `ideas`（只追加）
   → `paper`（有状态机与资金口径，最后）。
4. **`daily_bar` 的生产体量**：本机只验证了 5300 行（demo 数据）。三个市场全量（约数百万行）
   需要实测 COPY 吞吐与库容（8 GB 配额）。建议先在 ECS 上统计三份 parquet 的行数与字节数
（`wc -l` 不行，用 `du -sh` + `pd.read_parquet(columns=["symbol"]).shape`），再按 1 行约 80 字节估算。
