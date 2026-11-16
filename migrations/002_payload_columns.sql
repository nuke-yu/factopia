-- =====================================================================================
-- Factopia 迁移 002：payload 保真列
--
-- 001 建表时，三张表的文件字段没有落点：
--
--   idea               dedupe_key / document / leakage / similar / ai_suggested /
--                      ai_hypothesis / extraction / channel_zh
--   agent              costed_jobs（已计费的任务号，重启续跑时用来避免重复计费）
--   paper_enrollment   price_dataset（行情数据集，模拟数据集的信号不得接入）
--
-- 这些字段都不需要查询、排序或加约束，只是"整体读回时必须一模一样"。
-- 按 job 表的既有做法给一个 payload jsonb 兜住，而不是把它们逐个拆列——
-- 拆列会让表结构跟着业务字段漂移，而 payload 让"文件里什么形状，库里就什么形状"。
--
-- 幂等：ADD COLUMN IF NOT EXISTS，可重复执行。
-- =====================================================================================

BEGIN;

SET search_path = factopia, public;

ALTER TABLE factopia.idea
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN factopia.idea.payload IS
    '文件时代的其余字段（dedupe_key/document/leakage/similar/ai_suggested/ai_hypothesis/extraction/channel_zh）：整体保真，不拆列';

ALTER TABLE factopia.agent
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN factopia.agent.payload IS
    '文件时代的其余字段（costed_jobs 等）：整体保真，不拆列';

-- 一轮的 5 个正列（facts/diagnosis/plan/action/evaluation）之外，还有 draft_action、
-- plan_critique、external、next_version_draft、approval 五种轮次文件，落进 payload
ALTER TABLE factopia.agent_round
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN factopia.agent_round.payload IS
    '五个正列之外的轮次文件（draft_action/plan_critique/external/next_version_draft/approval）';

ALTER TABLE factopia.paper_enrollment
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN factopia.paper_enrollment.payload IS
    '接入时的其余字段（price_dataset 等）：模式与数据集在接入时固化，运行期不可改';

COMMIT;
