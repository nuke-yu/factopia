-- =====================================================================================
-- Factopia · PostgreSQL 16 初始化 schema（第一阶段：结构与约束，不含数据）
--
-- 目标库：team12 @ pgm-2ze9t2uh39o1115k.rwlb.rds.aliyuncs.com:5432（PostgreSQL 16 / pgvector 0.8.0.2）
-- 执行方式（RDS 只对 ECS 内网开放，本机须先建 SSH 隧道，见方案第 7.1 节）：
--     export PYTHONPATH=/tmp/dbtools            # 或正式装 psycopg 到项目环境
--     export FACTOPIA_SSH_PASSWORD=... FACTOPIA_PGPASSWORD=...
--     python scripts/db/pg_tunnel.py apply migrations/001_init_schema.sql
--   有本地 psql 时可等价执行：
--     psql "host=127.0.0.1 port=15432 dbname=team12 user=team12" -v ON_ERROR_STOP=1 \
--          -f migrations/001_init_schema.sql
--
-- 实测状态（2026-11-15）：已在真实 RDS 执行两次，51 张逻辑表 / 4 视图 / 13 显式触发器，
--   7 项行为断言全绿，二次执行零变化。详见方案第 8 节与 docs/db-verify-report.json。
--
-- 设计约定
--   1. 独立 schema `factopia`，不污染 public；所有对象显式带 schema 前缀。
--   2. id 沿用现有字符串编号（research_id / job_id / run_id / agent_id / signal_id / sid），
--      迁移时不做 id 重编号——留痕、报告、URL 里全是这些字符串，改号等于断链。
--   3. 时间统一 timestamptz（现状是 UTC ISO 字符串）；日期用 date（交易日 / 业务日）。
--   4. 金额 numeric(14,6)，收益率与指标 double precision（允许 NaN / Infinity，与 Python 侧一致）。
--   5. 「冻结件」用 jsonb 整体保存，不做规范化：审批材料、研究规格、报告摘要要求
--      「当时是什么就是什么」，规范化会把审计语义拆散（详见方案第 5.3 节）。
--   6. 大体量、只整块读写的二进制不建表，由 factopia.artifact 登记路径与 sha256。
--   7. 留痕表（*_event）只追加：触发器禁止 UPDATE / DELETE，哈希链字段 prev_hash + hash。
--
-- 幂等：全部 IF NOT EXISTS / CREATE OR REPLACE，可重复执行。
-- =====================================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS factopia;
SET LOCAL search_path = factopia, public;

-- 扩展说明（已在本实例实测确认）：
--   vector 由平台预装在 public，IF NOT EXISTS 直接跳过。
--   pg_trgm 此前不存在，由本脚本创建。注意不要写 SCHEMA public：
--   阿里云 RDS 会把 CREATE EXTENSION 建出的对象归到 alicloud_rds_admin 名下
--   （扩展 owner 是 team12，但函数 owner 不是），因此 team12 无法执行
--   ALTER EXTENSION ... SET SCHEMA，写了反而会报 InsufficientPrivilege。
--   扩展随 search_path 首个 schema 落在 factopia，业务连接需显式设置
--   search_path=factopia,public 才能解析 similarity() / gin_trgm_ops。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- =====================================================================================
-- 0. 通用：只追加表保护 + 更新时间戳触发器
-- =====================================================================================

CREATE OR REPLACE FUNCTION factopia.tg_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% 是只追加表（审计留痕），不允许 % 操作', TG_TABLE_NAME, TG_OP
        USING HINT = '如需更正，请追加一条纠正事件，不要改动历史记录';
END $$;

CREATE OR REPLACE FUNCTION factopia.tg_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- =====================================================================================
-- 1. 研究规格：research / research_version / app_state
--    现状：runtime/research/<id>/v<k>.yaml（只新建、不覆盖）+ terminated.json
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.research (
    research_id     text PRIMARY KEY,
    title_zh        text NOT NULL,
    markets         text[] NOT NULL DEFAULT '{}',
    latest_version  integer,
    -- 人工终止记录（现状 terminated.json）：{"version","at","by","reason"}
    terminated      jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN factopia.research.terminated IS '人工终止标记；非空即该研究任何版本都不得再启动 Agent';

CREATE TABLE IF NOT EXISTS factopia.research_version (
    research_id text    NOT NULL REFERENCES factopia.research(research_id) ON DELETE RESTRICT,
    version     integer NOT NULL CHECK (version >= 1),
    -- 完整规格文档（原 v<k>.yaml 的全部字段，含 ai_draft / data_needs / signal_source 等扩展键）
    spec        jsonb   NOT NULL,
    -- 规格文档规范化 JSON 的 sha256：用于「提交时冻结的规格」与当前版本一致性校验
    sha256      text    NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    locked_at   timestamptz NOT NULL,
    created_by  text,
    PRIMARY KEY (research_id, version)
);

COMMENT ON TABLE factopia.research_version IS '锁定的研究规格版本；只新建不覆盖（替代 v<k>.yaml 的独占创建语义）';

CREATE TABLE IF NOT EXISTS factopia.app_state (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE factopia.app_state IS '单例/全局状态：排队暂停(.paused)、当日数据更新标记(.daily_update)、调度水位等';

-- =====================================================================================
-- 2. 任务队列：job / job_resume / job_live
--    现状：runtime/jobs/<job_id>.json / .cancel / .live.json / .log / .paused
--    迁移要点：runner 与 api 同时 read-modify-write 同一文件 → 改为行锁 + SKIP LOCKED
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.job (
    job_id      text PRIMARY KEY,
    batch_id    text NOT NULL,
    kind        text NOT NULL DEFAULT 'research',
    research_id text,
    version     integer,
    market      text NOT NULL,
    horizon     integer NOT NULL DEFAULT 0,
    loops       integer NOT NULL DEFAULT 0,
    budget_usd  numeric(14,6) NOT NULL DEFAULT 0,
    state       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    ended_at    timestamptz,
    run_id      text,
    cost_usd    numeric(14,6) NOT NULL DEFAULT 0,
    rounds      integer NOT NULL DEFAULT 0,
    error       text,
    -- 各 kind 专有字段：target_run / loop / placebo_n / target_signal / agent_id /
    -- child_jobs / agent_state / resume / log_dir / enqueued_by 等
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT job_state_chk CHECK (state IN ('queued','running','done','failed','cancelled','budget_stopped')),
    CONSTRAINT job_kind_chk  CHECK (kind IN ('research','text','validate','monitor','paper','agent'))
);

CREATE INDEX IF NOT EXISTS job_queue_idx   ON factopia.job (created_at) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS job_batch_idx   ON factopia.job (batch_id, created_at);
CREATE INDEX IF NOT EXISTS job_research_idx ON factopia.job (research_id, version);
CREATE INDEX IF NOT EXISTS job_state_idx   ON factopia.job (state, started_at DESC);
CREATE INDEX IF NOT EXISTS job_payload_gin ON factopia.job USING gin (payload jsonb_path_ops);

COMMENT ON TABLE factopia.job IS '运行任务队列（研究/文本/可信度/监控/模拟盘/Agent 共用一个队列）';
COMMENT ON INDEX factopia.job_queue_idx IS '取任务：SELECT ... WHERE state=''queued'' ORDER BY created_at FOR UPDATE SKIP LOCKED';

CREATE TABLE IF NOT EXISTS factopia.job_resume (
    job_id     text    NOT NULL REFERENCES factopia.job(job_id) ON DELETE CASCADE,
    seq        integer NOT NULL,
    at         timestamptz NOT NULL,
    from_state text,
    error      text,
    rounds     integer,
    cost_usd   numeric(14,6),
    checkpoint text,
    remaining  integer,
    model      text,
    PRIMARY KEY (job_id, seq)
);

COMMENT ON TABLE factopia.job_resume IS '断点续跑记录（现状 job["resumes"] 数组），一次续跑一行';

CREATE TABLE IF NOT EXISTS factopia.job_live (
    job_id     text PRIMARY KEY REFERENCES factopia.job(job_id) ON DELETE CASCADE,
    state      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE factopia.job_live IS '运行中实时状态（现状 .live.json，运行器每 3–15 秒覆盖）；高频单行 UPSERT，与 job 分表避免争锁';

-- =====================================================================================
-- 3. 运行结果：run / run_round / run_factor / run_event / 回测与 IC 时序
--    现状：runtime/results/runs/<run_id>/（run.json、rounds.json、events.json、
--          backtest|positions|signal|score/*.parquet、quantile/*.json、validation/*.json）
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.run (
    run_id       text PRIMARY KEY,
    market       text NOT NULL,
    horizon      integer NOT NULL,
    research_id  text,
    version      integer,
    status       text,
    rounds       integer,
    adopted      integer,
    cost_usd     numeric(14,6),
    started_at   timestamptz,
    ended_at     timestamptz,
    duration_s   numeric(12,1),
    log_dir      text,
    -- 发给 AI 的场景信息全文（研究上下文；审计要看「当时告诉模型什么」）
    context      text,
    context_leak boolean,
    settings     jsonb,
    baseline     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_market_idx   ON factopia.run (market, horizon, started_at DESC);
CREATE INDEX IF NOT EXISTS run_research_idx ON factopia.run (research_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS run_id_uq ON factopia.run (run_id);

CREATE TABLE IF NOT EXISTS factopia.run_round (
    run_id            text    NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    loop              integer NOT NULL,
    status            text,
    hypothesis        text,
    reason            text,
    coding_attempts   integer,
    metrics           jsonb,   -- ic / rank_ic / annual_return_* / max_drawdown / information_ratio
    sota_before       jsonb,
    decision          boolean, -- 本轮是否被采纳
    feedback          jsonb,   -- observations / evaluation / new_hypothesis / reason
    steps             jsonb,   -- 各阶段耗时
    workspace         text,    -- 回测产物目录（留在文件系统）
    cost_usd          numeric(14,6),
    prompt_tokens     bigint,
    completion_tokens bigint,
    started_at        timestamptz,
    ended_at          timestamptz,
    PRIMARY KEY (run_id, loop)
);

CREATE TABLE IF NOT EXISTS factopia.run_factor (
    run_id      text    NOT NULL,
    loop        integer NOT NULL,
    idx         integer NOT NULL,
    name        text    NOT NULL,
    description text,
    formulation text,
    variables   jsonb,
    code        text,     -- 因子代码（冻结后进审批材料；此处保留全量便于检索）
    kind        text,     -- price_volume / text
    dataset     text,
    source      text,
    simulated   boolean,
    PRIMARY KEY (run_id, loop, idx),
    FOREIGN KEY (run_id, loop) REFERENCES factopia.run_round(run_id, loop) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS run_factor_name_idx ON factopia.run_factor (name);
CREATE INDEX IF NOT EXISTS run_factor_trgm_idx ON factopia.run_factor USING gin (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS factopia.run_event (
    run_id  text    NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    seq     integer NOT NULL,
    t       numeric(14,2) NOT NULL,   -- 相对运行起点的秒数（现状 events.json 的 "t"）
    loop    integer,
    step    text,
    kind    text NOT NULL,            -- hypothesis/factors/llm/coded/backtest/decision/cost/step
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, seq)
);

CREATE INDEX IF NOT EXISTS run_event_kind_idx ON factopia.run_event (run_id, kind, seq);

COMMENT ON TABLE factopia.run_event IS '过程事件时间线；/api/runs/{id}/events/{index}/prompt 按 (run_id, seq) 直取，不必解析整份 JSON';

-- 回测逐日（baseline + 每轮一份）
CREATE TABLE IF NOT EXISTS factopia.backtest_daily (
    run_id    text NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    key       text NOT NULL,              -- 'baseline' 或轮次号 '0','1',...
    date      date NOT NULL,
    ret       double precision,
    cost      double precision,
    bench     double precision,
    turnover  double precision,
    excess    double precision,
    nav       double precision,
    nav_bench double precision,
    drawdown  double precision,
    PRIMARY KEY (run_id, key, date)
);

CREATE INDEX IF NOT EXISTS backtest_daily_date_idx ON factopia.backtest_daily (date);

-- 逐日持仓权重
CREATE TABLE IF NOT EXISTS factopia.backtest_position (
    run_id     text NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    key        text NOT NULL,
    date       date NOT NULL,
    instrument text NOT NULL,
    weight     double precision,
    PRIMARY KEY (run_id, key, date, instrument)
);

CREATE INDEX IF NOT EXISTS backtest_position_inst_idx ON factopia.backtest_position (instrument, date);

-- 逐日 IC
CREATE TABLE IF NOT EXISTS factopia.run_ic_daily (
    run_id  text NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    key     text NOT NULL,
    date    date NOT NULL,
    ic      double precision,
    rank_ic double precision,
    PRIMARY KEY (run_id, key, date)
);

-- 分组收益（按打分 5 组）
CREATE TABLE IF NOT EXISTS factopia.run_quantile (
    run_id           text NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    key              text NOT NULL,
    horizon          integer,
    groups           integer,
    days             integer,
    mean             jsonb,   -- 各组日均收益
    universe_mean    double precision,
    size             jsonb,   -- 各组平均股票数
    empty_days       jsonb,
    spread_mean      double precision,
    spread_positive_share double precision,
    tie_share        double precision,
    dates            jsonb,
    nav              jsonb,   -- 各组累计净值曲线
    nav_universe     jsonb,
    PRIMARY KEY (run_id, key)
);

-- 可信度检查（validate）
CREATE TABLE IF NOT EXISTS factopia.run_validation (
    run_id      text    NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    loop        integer NOT NULL,
    state       text,
    -- 2024 年后样本外、随机因子对照、多种子并列排序的完整结果
    payload     jsonb   NOT NULL,
    credibility text,
    computed_at timestamptz,
    PRIMARY KEY (run_id, loop)
);

-- =====================================================================================
-- 4. 授权组合：portfolio / portfolio_daily / portfolio_weight / portfolio_feedback
--    现状：runtime/portfolio/<id>_v<k>/{summary.json, daily.parquet, weights.parquet, feedback.json}
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.portfolio (
    research_id   text    NOT NULL,
    version       integer NOT NULL,
    built_at      timestamptz,
    base_currency text,
    period        jsonb,
    metrics       jsonb,
    attribution   jsonb,
    fx_split      jsonb,
    constraints   jsonb,   -- 约束逐条注明来源
    summary       jsonb   NOT NULL,   -- summary.json 全文（冻结进审批材料的就是它）
    PRIMARY KEY (research_id, version)
);

CREATE TABLE IF NOT EXISTS factopia.portfolio_daily (
    research_id text    NOT NULL,
    version     integer NOT NULL,
    date        date    NOT NULL,
    -- 组合与基准日收益（便于 SQL 直接画曲线、算回撤）
    port        double precision,
    bench       double precision,
    -- 分市场明细：{m}_rl/_bl/_rb/_bb/_w/_cost/_rf（本地/基准币口径、权重、成本、汇率）
    payload     jsonb,
    PRIMARY KEY (research_id, version, date),
    FOREIGN KEY (research_id, version) REFERENCES factopia.portfolio(research_id, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS factopia.portfolio_weight (
    research_id text    NOT NULL,
    version     integer NOT NULL,
    date        date    NOT NULL,
    market      text    NOT NULL,
    instrument  text    NOT NULL,
    weight      double precision,
    PRIMARY KEY (research_id, version, date, instrument),
    FOREIGN KEY (research_id, version) REFERENCES factopia.portfolio(research_id, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS factopia.portfolio_feedback (
    id          bigserial PRIMARY KEY,
    research_id text    NOT NULL,
    version     integer NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text,
    note        text,
    payload     jsonb,
    FOREIGN KEY (research_id, version) REFERENCES factopia.portfolio(research_id, version) ON DELETE CASCADE
);

-- =====================================================================================
-- 5. 审批：approval_submission / approval_step / approval_event / approval_rule_decision
--    现状：runtime/approvals/<sid>/{submission.json, events.jsonl, materials/**}
--    关键：events.jsonl 是哈希链（prev 指向上一事件 hash）→ 必须保留链语义，不能拆散
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.approval_submission (
    id               text PRIMARY KEY,
    research_id      text NOT NULL,
    version          integer NOT NULL,
    markets          text[] NOT NULL,
    approved_markets text[],
    status           text NOT NULL,     -- in_review/returned/rejected/approved/approved_restricted
    current_step     integer NOT NULL DEFAULT 0,
    fingerprint      text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- 冻结材料全文：假设、经济逻辑、数据快照指纹、三市场结果、两次预检、组合摘要、
    -- 报告文件指纹、提示词与模型版本、规则包指纹、代码指纹、冻结翻译……
    materials        jsonb NOT NULL,
    conditions       jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- 获批锁定记录（部署范围、监控阈值、适用性标签）
    lock             jsonb,
    CONSTRAINT approval_status_chk CHECK (status IN ('in_review','returned','rejected','approved','approved_restricted'))
);

CREATE INDEX IF NOT EXISTS approval_research_idx ON factopia.approval_submission (research_id, version, created_at DESC);
CREATE INDEX IF NOT EXISTS approval_status_idx   ON factopia.approval_submission (status, updated_at DESC);

COMMENT ON COLUMN factopia.approval_submission.materials IS '冻结提交材料（materials.json 原文）；不做规范化——审计要求整体保真';

CREATE TABLE IF NOT EXISTS factopia.approval_step (
    submission_id text    NOT NULL REFERENCES factopia.approval_submission(id) ON DELETE CASCADE,
    idx           integer NOT NULL,
    role          text    NOT NULL,
    market        text,
    duty          text,
    state         text NOT NULL DEFAULT 'pending',  -- pending/approved/return/reject
    hits          text[] NOT NULL DEFAULT '{}',     -- 本角色须处理的规则命中 规则编号@市场
    model_exceptions text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (submission_id, idx)
);

CREATE TABLE IF NOT EXISTS factopia.approval_event (
    submission_id text    NOT NULL REFERENCES factopia.approval_submission(id) ON DELETE CASCADE,
    seq           integer NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    action        text    NOT NULL,
    role          text    NOT NULL,
    actor         text    NOT NULL,
    note          text,
    step          integer,
    rule          text,
    markets       text[],
    thresholds    jsonb,
    labels        text,
    payload       jsonb   NOT NULL DEFAULT '{}'::jsonb,
    prev_hash     text    NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash          text    NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (submission_id, seq),
    CONSTRAINT approval_event_hash_uq UNIQUE (submission_id, hash)
);

CREATE INDEX IF NOT EXISTS approval_event_at_idx ON factopia.approval_event (at);

COMMENT ON TABLE factopia.approval_event IS '审批留痕，哈希链：hash = sha256(除 hash 外全部字段)，prev_hash = 上一条 hash；只追加';
DROP TRIGGER IF EXISTS approval_event_append_only ON factopia.approval_event;
CREATE TRIGGER approval_event_append_only
    BEFORE UPDATE OR DELETE ON factopia.approval_event
    FOR EACH ROW EXECUTE FUNCTION factopia.tg_forbid_mutation();

CREATE TABLE IF NOT EXISTS factopia.approval_rule_decision (
    submission_id text NOT NULL REFERENCES factopia.approval_submission(id) ON DELETE CASCADE,
    role          text NOT NULL,
    rule          text NOT NULL,       -- 规则编号@市场，或 MODEL@市场
    action        text NOT NULL,       -- exception / acknowledge
    actor         text,
    note          text,
    at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (submission_id, role, rule)
);

COMMENT ON TABLE factopia.approval_rule_decision IS '现状 submission.json 的 rule_decisions（键为 "角色|规则"）；拆表后可直接按规则统计例外情况';

-- =====================================================================================
-- 6. 信号与模拟盘：signal_event / signal_monitor / paper_*
--    现状：runtime/signals/events.jsonl（全局哈希链）、signals/<sid>/{monitor.json,paper.json,score.parquet}
--          runtime/paper/<sid>/{enrolled.json,state.json,nav.csv,positions.csv,orders.jsonl}
--    注意：信号目录本身是派生视图（审批+事件+监控实时聚合），不建表，见方案 3.3.4
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.signal_event (
    seq       bigserial PRIMARY KEY,
    signal_id text NOT NULL,
    market    text,
    action    text NOT NULL,     -- pause / resume / retire
    role      text NOT NULL,
    actor     text NOT NULL,
    note      text NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    prev_hash text NOT NULL,
    hash      text NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS signal_event_sid_idx ON factopia.signal_event (signal_id, seq);

DROP TRIGGER IF EXISTS signal_event_append_only ON factopia.signal_event;
CREATE TRIGGER signal_event_append_only
    BEFORE UPDATE OR DELETE ON factopia.signal_event
    FOR EACH ROW EXECUTE FUNCTION factopia.tg_forbid_mutation();

CREATE TABLE IF NOT EXISTS factopia.signal_monitor (
    signal_id     text NOT NULL,
    computed_at   timestamptz NOT NULL,
    suggestion    text,
    suggestion_zh text,
    alerts        jsonb NOT NULL DEFAULT '[]'::jsonb,
    metrics       jsonb,
    payload       jsonb NOT NULL,       -- monitor.json 全文
    PRIMARY KEY (signal_id, computed_at)
);

COMMENT ON TABLE factopia.signal_monitor IS '每次监控计算一行；目录取每个 signal 的最新一行（LATERAL / DISTINCT ON），不再全目录扫文件';

-- 模拟盘接入（接入时固化撮合模式，运行期不可改）
CREATE TABLE IF NOT EXISTS factopia.paper_enrollment (
    signal_id     text PRIMARY KEY,
    market        text NOT NULL,
    mode          text NOT NULL CHECK (mode IN ('shadow','longport')),
    capital       numeric(18,2) NOT NULL,
    horizon       integer,
    thresholds    jsonb,
    ref_start     date,
    ref_end       date,
    base_currency text,
    price_dataset text,
    enrolled_at   timestamptz NOT NULL DEFAULT now(),
    active        boolean NOT NULL DEFAULT true,
    stopped_at    timestamptz,
    stop_reason   text
);

CREATE TABLE IF NOT EXISTS factopia.paper_state (
    signal_id       text PRIMARY KEY REFERENCES factopia.paper_enrollment(signal_id) ON DELETE CASCADE,
    last_cycle      date,
    last_rebalance  date,
    cash            numeric(18,2) NOT NULL DEFAULT 0,
    nav             numeric(18,2) NOT NULL DEFAULT 0,
    positions_value numeric(18,2) NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE factopia.paper_state IS '模拟盘当前态（现状 state.json）；持仓与挂单已拆表，此处只留账户级聚合值';

CREATE TABLE IF NOT EXISTS factopia.paper_position (
    signal_id  text NOT NULL REFERENCES factopia.paper_enrollment(signal_id) ON DELETE CASCADE,
    symbol     text NOT NULL,
    qty        numeric(20,6) NOT NULL,
    avg_cost   numeric(20,6),
    opened_on  date,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id, symbol)
);

CREATE TABLE IF NOT EXISTS factopia.paper_order (
    order_id   text PRIMARY KEY,
    signal_id  text NOT NULL REFERENCES factopia.paper_enrollment(signal_id) ON DELETE CASCADE,
    symbol     text NOT NULL,
    side       text NOT NULL CHECK (side IN ('buy','sell')),
    qty        numeric(20,6) NOT NULL,
    placed_on  date NOT NULL,
    mode       text NOT NULL,
    status     text NOT NULL DEFAULT 'pending',  -- pending/filled/cancelled/expired/rejected
    filled_qty numeric(20,6) NOT NULL DEFAULT 0,
    filled_px  numeric(20,6),
    filled_on  date,
    cost       numeric(18,6),
    broker_ref text,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS paper_order_pending_idx ON factopia.paper_order (signal_id, status, placed_on);

COMMENT ON TABLE factopia.paper_order IS '挂单与成交（现状 orders.jsonl 只追加 + state.pending 数组）；一个订单一行、状态原地更新';

-- 逐日持仓快照（现状 positions.csv，同日重跑覆盖旧行）
CREATE TABLE IF NOT EXISTS factopia.paper_position_daily (
    signal_id text NOT NULL REFERENCES factopia.paper_enrollment(signal_id) ON DELETE CASCADE,
    date      date NOT NULL,
    symbol    text NOT NULL,
    qty       numeric(20,6),
    price     numeric(20,6),
    value     numeric(20,2),
    PRIMARY KEY (signal_id, date, symbol)
);

CREATE TABLE IF NOT EXISTS factopia.paper_nav (
    signal_id       text NOT NULL REFERENCES factopia.paper_enrollment(signal_id) ON DELETE CASCADE,
    date            date NOT NULL,
    nav             numeric(18,2),
    cash            numeric(18,2),
    positions_value numeric(18,2),
    benchmark       numeric(18,2),
    PRIMARY KEY (signal_id, date)
);

COMMENT ON TABLE factopia.paper_nav IS '净值序列（现状 nav.csv）；同日重跑覆盖（UPSERT），与引擎的幂等语义一致';

CREATE TABLE IF NOT EXISTS factopia.paper_report (
    signal_id   text NOT NULL REFERENCES factopia.paper_enrollment(signal_id) ON DELETE CASCADE,
    computed_at timestamptz NOT NULL,
    -- paper.json 全文快照：报告口径会随规则包与基准变化，留快照才能回答「当时怎么说」
    payload     jsonb NOT NULL,
    suggestion  text,
    PRIMARY KEY (signal_id, computed_at)
);

-- =====================================================================================
-- 7. 线索池：idea / idea_event / idea_document / idea_source
--    现状：runtime/ideas/<id>/{idea.json, events.jsonl, files/} + ideas/sources.json
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.idea (
    idea_id      text PRIMARY KEY,
    channel      text NOT NULL,
    title        text NOT NULL,
    body         text,
    language     text,
    markets      text[] NOT NULL DEFAULT '{}',
    submitted_by text,
    role         text,
    source_ref   text,
    status       text NOT NULL DEFAULT 'new',
    pii_checked  boolean NOT NULL DEFAULT false,
    links        jsonb NOT NULL DEFAULT '{}'::jsonb,
    fields       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- 论文抽取的候选因子等
    similar_to   jsonb,                                 -- 去重复核结果（similar 是 PG 保留字，不能裸用）
    converted    jsonb,                                 -- 转立项后的 research_id / version / combo
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idea_status_idx  ON factopia.idea (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idea_channel_idx ON factopia.idea (channel, created_at DESC);
CREATE INDEX IF NOT EXISTS idea_markets_idx ON factopia.idea USING gin (markets);
CREATE INDEX IF NOT EXISTS idea_title_trgm  ON factopia.idea USING gin (title gin_trgm_ops);

CREATE TABLE IF NOT EXISTS factopia.idea_event (
    idea_id   text    NOT NULL REFERENCES factopia.idea(idea_id) ON DELETE CASCADE,
    seq       integer NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    action    text    NOT NULL,
    actor     text    NOT NULL,
    note      text,
    payload   jsonb   NOT NULL DEFAULT '{}'::jsonb,
    prev_hash text    NOT NULL,
    hash      text    NOT NULL,
    PRIMARY KEY (idea_id, seq),
    CONSTRAINT idea_event_hash_uq UNIQUE (idea_id, hash)
);

DROP TRIGGER IF EXISTS idea_event_append_only ON factopia.idea_event;
CREATE TRIGGER idea_event_append_only
    BEFORE UPDATE OR DELETE ON factopia.idea_event
    FOR EACH ROW EXECUTE FUNCTION factopia.tg_forbid_mutation();

CREATE TABLE IF NOT EXISTS factopia.idea_document (
    idea_id   text NOT NULL REFERENCES factopia.idea(idea_id) ON DELETE CASCADE,
    filename  text NOT NULL,
    mime      text,
    bytes     bigint,
    sha256    text NOT NULL,
    artifact_id bigint,          -- 指向 factopia.artifact（文件本体在对象存储 / 数据盘）
    extracted_chars integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (idea_id, filename)
);

CREATE TABLE IF NOT EXISTS factopia.idea_source (
    dedupe_key text PRIMARY KEY,   -- channel|title|text|markets|ref 的哈希
    idea_id    text NOT NULL REFERENCES factopia.idea(idea_id) ON DELETE CASCADE,
    channel    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE factopia.idea_source IS '自动回流去重索引（现状 ideas/sources.json），加唯一键即可杜绝并发重复插入';

-- =====================================================================================
-- 8. 研究 Agent：agent / agent_round / agent_memory / agent_trace
--    现状：runtime/agents/<agent_id>/{agent.json, rounds/<n>/*.json, memory.json, trace.jsonl, report.md}
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.agent (
    agent_id       text PRIMARY KEY,
    kind           text NOT NULL DEFAULT 'iteration',
    state          text NOT NULL,
    research_id    text NOT NULL,
    root_version   integer NOT NULL,
    markets        text[] NOT NULL DEFAULT '{}',
    horizon        integer,
    policy         jsonb NOT NULL,
    current_round  integer NOT NULL DEFAULT 0,
    current_version integer,
    parent_version integer,
    child_version  integer,
    round_limit    integer NOT NULL DEFAULT 5,
    dry_run        boolean NOT NULL DEFAULT false,
    web_search     boolean NOT NULL DEFAULT false,
    deep_think     boolean NOT NULL DEFAULT false,
    auto_approve   boolean NOT NULL DEFAULT false,
    approved       jsonb,
    approved_run   boolean NOT NULL DEFAULT true,
    cost_usd       numeric(14,6) NOT NULL DEFAULT 0,
    usage          jsonb NOT NULL DEFAULT '{}'::jsonb,
    child_jobs     text[] NOT NULL DEFAULT '{}',
    last_action    text,
    stop_reason    text,
    last_error     text,
    report         text,                 -- 迭代报告 markdown（现状 report.md）
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_state_idx ON factopia.agent (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS agent_research_idx ON factopia.agent (research_id, root_version);

CREATE TABLE IF NOT EXISTS factopia.agent_round (
    agent_id     text    NOT NULL REFERENCES factopia.agent(agent_id) ON DELETE CASCADE,
    n            integer NOT NULL,
    facts        jsonb,
    diagnosis    jsonb,
    plan         jsonb,
    action       jsonb,
    evaluation   jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, n)
);

COMMENT ON TABLE factopia.agent_round IS '一轮 5 个 JSON（facts/diagnosis/plan/action/evaluation）合并为一行 5 列：总是整轮一起读，拆表无收益';

CREATE TABLE IF NOT EXISTS factopia.agent_memory (
    agent_id   text PRIMARY KEY REFERENCES factopia.agent(agent_id) ON DELETE CASCADE,
    attempts   jsonb NOT NULL DEFAULT '[]'::jsonb,
    exclusions jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factopia.agent_trace (
    agent_id text    NOT NULL REFERENCES factopia.agent(agent_id) ON DELETE CASCADE,
    seq      bigserial,
    at       timestamptz NOT NULL DEFAULT now(),
    kind     text,               -- llm / tool / state
    payload  jsonb NOT NULL,
    PRIMARY KEY (agent_id, seq)
);

CREATE INDEX IF NOT EXISTS agent_trace_kind_idx ON factopia.agent_trace (agent_id, kind, at DESC);

COMMENT ON TABLE factopia.agent_trace IS '现状 trace.jsonl；改为表后可按住址检索、按时间截断，且与 agent 同一事务';

-- =====================================================================================
-- 9. 行情与参考数据：daily_bar / fx_rate / data_quality / security_master
--                     / index_membership / corporate_action
--    现状：runtime/data/{raw,standardized}/*.parquet、runtime/refdata/*.parquet
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.daily_bar (
    market     text NOT NULL,
    symbol     text NOT NULL,
    date       date NOT NULL,
    open       double precision,
    high       double precision,
    low        double precision,
    close      double precision,
    volume     double precision,
    currency   text,
    quote_currency text,
    ts_utc     timestamptz,       -- 收盘时刻（正式发布）
    available_at timestamptz,     -- 实际可获取时间（收盘 + 日终延迟）——时间点一致性的依据
    dq_flag    text,
    PRIMARY KEY (market, symbol, date)
) PARTITION BY LIST (market);

CREATE TABLE IF NOT EXISTS factopia.daily_bar_hk PARTITION OF factopia.daily_bar FOR VALUES IN ('hk');
CREATE TABLE IF NOT EXISTS factopia.daily_bar_uk PARTITION OF factopia.daily_bar FOR VALUES IN ('uk');
CREATE TABLE IF NOT EXISTS factopia.daily_bar_fr PARTITION OF factopia.daily_bar FOR VALUES IN ('fr');

-- 截面查询（某日全市场）
CREATE INDEX IF NOT EXISTS daily_bar_date_idx   ON factopia.daily_bar (date, market);
-- 时序查询（某只股票的历史）
CREATE INDEX IF NOT EXISTS daily_bar_symbol_idx ON factopia.daily_bar (symbol, date);

COMMENT ON TABLE factopia.daily_bar IS '标准化行情（单一事实来源）；qlib 二进制与 standardized.parquet 降级为派生物化产物';

CREATE TABLE IF NOT EXISTS factopia.fx_rate (
    pair  text NOT NULL,
    date  date NOT NULL,
    close double precision,
    PRIMARY KEY (pair, date)
);

CREATE TABLE IF NOT EXISTS factopia.data_quality (
    market     text NOT NULL,
    date       date NOT NULL,
    sha256     text NOT NULL,
    report     jsonb NOT NULL,     -- quality_report.json 该市场段全文（含被排除股票与原因）
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market, date)
);

COMMENT ON TABLE factopia.data_quality IS '数据指纹 + 质量报告；审批 submit 时按 (market,date) 取 sha256 冻结，不再扫文件';

CREATE TABLE IF NOT EXISTS factopia.security_master (
    symbol          text PRIMARY KEY,
    name            text,
    market          text,
    currency        text,
    lei             text,
    figi            text,
    primary_listing text,
    industry        text,
    fiscal_year_end text,
    accounting_std  text,
    is_depositary   boolean,
    payload         jsonb
);

CREATE TABLE IF NOT EXISTS factopia.index_membership (
    index_code text NOT NULL,
    symbol     text NOT NULL,
    start_date date NOT NULL,
    end_date   date,              -- NULL = 仍在指数内
    source     text,
    PRIMARY KEY (index_code, symbol, start_date)
);

CREATE INDEX IF NOT EXISTS index_membership_date_idx ON factopia.index_membership (index_code, start_date, end_date);

CREATE TABLE IF NOT EXISTS factopia.corporate_action (
    id         bigserial PRIMARY KEY,
    symbol     text NOT NULL,
    action     text NOT NULL,     -- split / dividend / merger / delisting / rights
    ex_date    date NOT NULL,
    ratio      numeric(20,8),
    amount     numeric(20,8),
    currency   text,
    source     text,
    payload    jsonb
);

CREATE INDEX IF NOT EXISTS corporate_action_symbol_idx ON factopia.corporate_action (symbol, ex_date);

-- =====================================================================================
-- 10. 知识检索（pgvector）+ 二进制登记：knowledge_chunk / artifact
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.knowledge_chunk (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,       -- factor / research / field / filing / idea
    ref_table  text NOT NULL,
    ref_id     text NOT NULL,
    lang       text,
    title      text,
    content    text NOT NULL,
    -- 关键词检索词元（沿用 semantic.py 的双字切分 + 去重音规则，Python 侧生成）
    lexemes    text[] NOT NULL DEFAULT '{}',
    embedding  vector(1024),        -- 维度随所选 embedding 模型；bge-m3 / text-embedding-v3 均为 1024
    source_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ref_table, ref_id, lang, title)
);

CREATE INDEX IF NOT EXISTS knowledge_chunk_lex_idx ON factopia.knowledge_chunk USING gin (lexemes);
CREATE INDEX IF NOT EXISTS knowledge_chunk_vec_idx ON factopia.knowledge_chunk
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS knowledge_chunk_kind_idx ON factopia.knowledge_chunk (kind);

COMMENT ON TABLE factopia.knowledge_chunk IS 'hybrid 检索：lexemes 走 GIN（BM25 排序仍在 Python），embedding 走 HNSW 召回';

-- 二进制产物登记：留在文件系统 / OSS 的文件都在这张表里有「户口」
CREATE TABLE IF NOT EXISTS factopia.artifact (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,      -- qlib_bin / parquet / report / pdf / evidence_zip / log / workspace
    rel_path   text NOT NULL,
    sha256     text NOT NULL,
    bytes      bigint NOT NULL,
    mime       text,
    ref_table  text,
    ref_id     text,
    storage    text NOT NULL DEFAULT 'fs',   -- fs / oss
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rel_path, sha256)
);

CREATE INDEX IF NOT EXISTS artifact_ref_idx ON factopia.artifact (ref_table, ref_id);
CREATE INDEX IF NOT EXISTS artifact_kind_idx ON factopia.artifact (kind, created_at DESC);

COMMENT ON TABLE factopia.artifact IS '所有未入库文件的内容寻址清单；sha256 可在迁移后逐字节校验，也是清理/归档的依据';

-- =====================================================================================
-- 11. 运维：llm_cost / access_event / migration_file
-- =====================================================================================

CREATE TABLE IF NOT EXISTS factopia.llm_cost (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    model       text,
    purpose     text,
    prompt_tokens     bigint,
    completion_tokens bigint,
    usd         numeric(14,6),
    ref_table   text,
    ref_id      text
);

CREATE INDEX IF NOT EXISTS llm_cost_at_idx ON factopia.llm_cost (at DESC);
CREATE INDEX IF NOT EXISTS llm_cost_ref_idx ON factopia.llm_cost (ref_table, ref_id);

CREATE TABLE IF NOT EXISTS factopia.access_event (
    seq       bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    view      text,               -- 地区视角
    resource  text,
    ref_id    text,
    allowed   boolean NOT NULL,
    actor     text,
    prev_hash text,
    hash      text
);

COMMENT ON TABLE factopia.access_event IS '越权访问留痕（现状 runtime/access/events.jsonl，哈希链）';

CREATE TABLE IF NOT EXISTS factopia.migration_file (
    path        text PRIMARY KEY,   -- 相对 runtime/ 的路径
    domain      text NOT NULL,
    sha256      text NOT NULL,
    bytes       bigint,
    rows        integer,
    status      text NOT NULL DEFAULT 'done',   -- done / skipped / failed
    error       text,
    migrated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE factopia.migration_file IS '迁移工具的书签：同一文件 sha256 未变则跳过，实现幂等与断点续传';

-- =====================================================================================
-- 11.1 updated_at 自动维护
--   tg_touch_updated_at() 在第 0 节已定义，但触发器必须等表全部建好之后才能挂。
--   这里按系统目录动态挂载：凡是带 updated_at 列的 factopia 表全部覆盖，
--   以后新增表也不用回来改这一段（幂等，重复执行不会重复创建）。
--   早期版本漏了这一段，导致 10 张表的 updated_at 永远停在 INSERT 时刻。
-- =====================================================================================

DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'factopia'
          AND c.relkind IN ('r', 'p')
          AND EXISTS (
              SELECT 1 FROM information_schema.columns col
              WHERE col.table_schema = 'factopia'
                AND col.table_name = c.relname
                AND col.column_name = 'updated_at'
          )
        ORDER BY c.relname
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = format('factopia.%I', r.relname)::regclass
              AND tgname = r.relname || '_touch_updated_at'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE UPDATE ON factopia.%I '
                'FOR EACH ROW EXECUTE FUNCTION factopia.tg_touch_updated_at()',
                r.relname || '_touch_updated_at', r.relname
            );
        END IF;
    END LOOP;
END $$;

-- =====================================================================================
-- 12. 派生视图（供 API 直接查询，替代原先的文件式聚合）
-- =====================================================================================

-- 每个信号的最新监控结果
CREATE OR REPLACE VIEW factopia.v_signal_monitor_latest AS
SELECT DISTINCT ON (signal_id) *
FROM factopia.signal_monitor
ORDER BY signal_id, computed_at DESC;

-- 任务队列概览（/api/jobs）
CREATE OR REPLACE VIEW factopia.v_job_overview AS
SELECT j.*, l.state AS live_state, l.updated_at AS live_updated_at
FROM factopia.job j
LEFT JOIN factopia.job_live l ON l.job_id = j.job_id;

-- 最新获批提交（信号目录的数据源之一）
CREATE OR REPLACE VIEW factopia.v_submission_latest AS
SELECT DISTINCT ON (research_id, version) *
FROM factopia.approval_submission
ORDER BY research_id, version, created_at DESC;

-- 模拟盘当前持仓估值
CREATE OR REPLACE VIEW factopia.v_paper_position_value AS
SELECT p.signal_id, p.symbol, p.qty, p.avg_cost,
       NULL::numeric(20,6) AS price, NULL::numeric(20,2) AS value
FROM factopia.paper_position p;

COMMIT;

-- =====================================================================================
-- 应用数据库账号的最小权限（创建后由 DBA 执行；team12 为所有者账号，无需重复授权）
--   GRANT USAGE ON SCHEMA factopia TO factopia_app;
--   GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA factopia TO factopia_app;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA factopia TO factopia_app;
--   REVOKE UPDATE, DELETE ON factopia.approval_event, factopia.signal_event,
--          factopia.idea_event, factopia.access_event FROM factopia_app;
-- =====================================================================================
