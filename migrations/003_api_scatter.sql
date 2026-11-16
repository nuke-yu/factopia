-- =====================================================================================
-- Factopia 迁移 003：清掉 API 侧残留的文件直读所需的落点
--
-- 背景：001/002 之后，业务数据基本都有表了，但 `/api/*` 仍有一批端点自己拼路径读文件。
-- 这些点在 files 模式下没问题，一旦 `FACTOPIA_STORAGE=db` 就会读到「上一次运行留下的
-- 旧文件」——页面看起来正常，数字却是旧的。003 补齐缺的四类落点：
--
--   research_version.explain   AI 市场差异解释（原 runtime/research/<id>/explain_v<k>.json）
--   run.payload / run_round.payload
--                              002 的同一思路：run.json / rounds.json 里随 RD-Agent 版本
--                              增减的键不逐个拆列，整体进 payload，读回逐字还原
--   run_report                 report.md（运行报告，正文入列，便于按 run_id 直取）
--   run_diagnostic             diagnostics/<loop>.json（诊断明细，结构随版本变化 → jsonb）
--   report_file                生成的 HTML 研究报告（原 runtime/reports/<name>.html）
--
-- app_state 继续承载单例键值：refdata 摘要、pit_status、vendor_imports 都放这里，
-- 不另建表——它们本来就是「一份快照」，没有按行查询的需求。
--
-- 执行：psql -f migrations/003_api_scatter.sql（幂等，可重复跑）
-- =====================================================================================

BEGIN;

-- ── 1. 研究：AI 解释存档 ──────────────────────────────────────────────────────────────
ALTER TABLE factopia.research_version ADD COLUMN IF NOT EXISTS explain jsonb;

COMMENT ON COLUMN factopia.research_version.explain IS
    'AI 市场差异解释（原 explain_v<k>.json）；非空表示该版本已生成过解释，读接口直接取此列';

-- ── 2. 运行：payload 保真 + 报告 + 诊断 ──────────────────────────────────────────────
ALTER TABLE factopia.run       ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE factopia.run_round ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN factopia.run.payload IS
    'run.json 中未被拆列的其余键；读回时与列值合并，保证 /api/runs 返回与文件逐字一致';
COMMENT ON COLUMN factopia.run_round.payload IS
    'rounds.json 中未被拆列的其余键（如 metrics 之外的实验参数）；factors 另存 run_factor';

CREATE TABLE IF NOT EXISTS factopia.run_report (
    run_id     text PRIMARY KEY REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    markdown   text NOT NULL,
    bytes      bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE factopia.run_report IS '运行报告 report.md；正文短（几 KB），整篇入列，不再按路径读盘';

CREATE TABLE IF NOT EXISTS factopia.run_diagnostic (
    run_id      text    NOT NULL REFERENCES factopia.run(run_id) ON DELETE CASCADE,
    key         text    NOT NULL,       -- 轮次号 '0'/'1'… 或 'baseline'
    payload     jsonb   NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, key)
);

COMMENT ON TABLE factopia.run_diagnostic IS '诊断明细（原 diagnostics/<loop>.json）；只在研究结论页与 AI 解释里整份使用，不拆列';

-- ── 3. 生成的 HTML 报告 ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS factopia.report_file (
    name        text PRIMARY KEY,
    research_id text,
    version     integer,
    html        text NOT NULL,
    bytes       bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS report_file_ref_idx ON factopia.report_file (research_id, version, created_at DESC);

COMMENT ON TABLE factopia.report_file IS '单文件 HTML 研究报告；name 即对外 URL 的最后一段，/api/reports/<name> 直取 html 列';

-- 校验（执行后手工核对，期望表数 54 → 57）：
--   SELECT count(*) FROM information_schema.tables
--    WHERE table_schema = 'factopia' AND table_type = 'BASE TABLE';

COMMIT;
