-- =====================================================================================
-- 004：参考数据域（refdata）接表
--
-- 现状：`runtime/refdata/{securities,relations,corporate_actions}.parquet` 与
--      `configs/universe/membership/<market>.csv` 是唯一真相，API 直接 read_parquet。
-- 目标：这三份 parquet + 成分股名单进表，parquet / CSV 降级为派生物化产物。
--
-- 保真原则（与 002/003 一致）：
--   * 要查询、要排序、要加约束的字段拆成列；
--   * 其余整行进 `payload jsonb`，读回时「payload + 列」合并，列只补 payload 缺失的键；
--   * 列值可以是"投影"（例如文件里的 `type` 落成 `action`），**payload 才是逐字真相**。
-- 这样 `GET /api/refdata/securities` 在文件与库两种模式下才能逐字节一致。
--
-- 幂等：全部 IF NOT EXISTS，可重复执行。
-- =====================================================================================

BEGIN;

SET search_path = factopia, public;

-- ── 1. security_master：补三个"要按列查"的字段 ──────────────────────────────────────
--   `fiscal_year_end text` 保留给数据商给完整日期（如 2026-12-31）；我们现在只有月份，
--   另开 smallint 列，免得把 "12" 这种含义不明的字符串塞进日期列。
ALTER TABLE factopia.security_master ADD COLUMN IF NOT EXISTS long_name text;
ALTER TABLE factopia.security_master ADD COLUMN IF NOT EXISTS fiscal_year_end_month smallint;
ALTER TABLE factopia.security_master ADD COLUMN IF NOT EXISTS issuer_id text;

COMMENT ON COLUMN factopia.security_master.long_name IS 'Yahoo Finance 法定全称；name 是股票池里的简称';
COMMENT ON COLUMN factopia.security_master.issuer_id IS '发行人归并键：优先 LEI，否则 FIGI:<share_class_figi>，再退化为 FIGI:<symbol>';
COMMENT ON COLUMN factopia.security_master.fiscal_year_end_month IS '财政年度结束月（1-12）；完整日期留给 fiscal_year_end';

-- 跨市场挂牌、同一发行人多市场等关系，按 issuer_id 查
CREATE INDEX IF NOT EXISTS security_master_issuer_idx ON factopia.security_master (issuer_id);

-- ── 2. security_relation：跨市场挂牌关系（原 relations.parquet，无对应表）──────────────
--   一个发行人可在多个交易所挂牌（汇丰 0005.HK / HSBA.L），同一股份类别也可能有存托凭证。
--   这不是"指数成分"，所以不能塞进 index_membership；单独建表。
CREATE TABLE IF NOT EXISTS factopia.security_relation (
    issuer_id   text NOT NULL,       -- 空串代表"人工补录、发行人未识别"，原值在 payload
    venue       text NOT NULL,       -- OpenFIGI exchCode：HK / LN / FP / US …
    ticker      text NOT NULL DEFAULT '',
    relation    text,                -- same_share_class / depositary_receipt / other_share_class
    in_universe boolean,             -- 该挂牌是否本身就在本平台股票池里
    payload     jsonb,               -- 整行原样（含 relation_zh / status / note / figi …）
    PRIMARY KEY (issuer_id, venue, ticker)
);

CREATE INDEX IF NOT EXISTS security_relation_issuer_idx ON factopia.security_relation (issuer_id);
CREATE INDEX IF NOT EXISTS security_relation_ticker_idx ON factopia.security_relation (ticker);

COMMENT ON TABLE factopia.security_relation IS
    '发行人↔挂牌关系（原 refdata/relations.parquet）；主键 (issuer_id, venue, ticker) 保证重跑幂等';

-- ── 3. index_membership：补 CSV 里的 note ─────────────────────────────────────────────
ALTER TABLE factopia.index_membership ADD COLUMN IF NOT EXISTS note text;

COMMENT ON COLUMN factopia.index_membership.note IS
    '成分股来源说明（原 configs/universe/membership/<market>.csv 的 note 列）';

-- ── 4. corporate_action：数据商口径的行要能按 symbol+ex_date+action 判重 ────────────────
--   免费来源（Yahoo/AMF/行情推断）与数据商同类型同日期的记录以数据商为准，
--   `uniq` 让"重复导入同一份数据商文件"变成 UPSERT 而不是追加。
CREATE UNIQUE INDEX IF NOT EXISTS corporate_action_natural_key_idx
    ON factopia.corporate_action (symbol, ex_date, action);

-- ── 5. 行序 ───────────────────────────────────────────────────────────────────────────
--   文件里的行序是有含义的（`securities` 按股票池顺序、`relations` 按生成顺序），
--   而表本身无序。`ord` 记录原文件行号，读回时按它排序 —— 少了这一列，
--   `GET /api/refdata/securities` 在文件/库两种模式下返回的列表顺序会不一样。
ALTER TABLE factopia.security_master    ADD COLUMN IF NOT EXISTS ord integer;
ALTER TABLE factopia.security_relation  ADD COLUMN IF NOT EXISTS ord integer;
ALTER TABLE factopia.corporate_action   ADD COLUMN IF NOT EXISTS ord integer;
ALTER TABLE factopia.index_membership   ADD COLUMN IF NOT EXISTS ord integer;

COMMENT ON COLUMN factopia.security_master.ord IS '原 parquet 行序；读回时按它排序以保持接口返回顺序';
COMMENT ON COLUMN factopia.security_relation.ord IS '原 parquet 行序';
COMMENT ON COLUMN factopia.corporate_action.ord IS '原 parquet 行序（文件本身按 symbol,date 排序）';
COMMENT ON COLUMN factopia.index_membership.ord IS '原 CSV 行序';

INSERT INTO factopia.migration_file (path, domain, sha256, status)
VALUES ('migrations/004_refdata_tables.sql', 'schema', '', 'done')
ON CONFLICT (path) DO NOTHING;

COMMIT;
