-- =====================================================================================
-- 005：给"暂不启用"的表补注释，避免以后有人对着空表猜"是不是漏迁了"
--
-- 这一轮把 daily_bar / fx_rate 真正接进了代码（写入走 COPY，读取走 barstore），
-- 但下面两组表**刻意**不迁，理由写在注释里，也写在方案文档第 15 节：
--
--   backtest_daily / backtest_position
--       RD-Agent 每轮的逐日回测产物。文件（`results/runs/<id>/backtest/*.parquet`）
--       是唯一真相，且列结构随 RD-Agent 版本变化。拆成固定的列 = 用"猜的 schema"
--       复制一份，猜错就静默丢数据。页面与计算统一经 `runstore.backtest()` 读文件，
--       不存在"两个位置不一致"的问题。表保留给将来"跨运行 SQL 比较"用。
--
--   knowledge_chunk
--       hybrid 检索（BM25 排序 + pgvector 召回）。当前 `text.search` 直接扫
--       `aligned.parquet` + 每篇的 labels/docs，召回质量由概念标注决定，不依赖向量。
--       迁进去需要先定 embedding 模型与批量重算策略（P4），否则只是把同一份文本
--       换个地方存，检索并不会变好。表与 HNSW 索引已就绪，接上即可用。
--
-- 幂等：COMMENT 可重复执行。
-- =====================================================================================

BEGIN;

SET search_path = factopia, public;

COMMENT ON TABLE factopia.backtest_daily IS
    '保留未启用（P3 决定）：逐日回测产物留在 results/runs/<id>/backtest/*.parquet，'
    '经 runstore.backtest() 读取；列结构随 RD-Agent 版本变化，拆列会静默丢数据';

COMMENT ON TABLE factopia.backtest_position IS
    '保留未启用（P3 决定）：逐日持仓留在 results/runs/<id>/positions/*.parquet，经 runstore.positions() 读取';

COMMENT ON TABLE factopia.knowledge_chunk IS
    '保留未启用（P4）：hybrid 检索（lexemes 走 GIN、embedding 走 HNSW）。'
    '当前 text.search 扫 aligned.parquet + labels/docs，召回由概念标注决定；'
    '接入需先定 embedding 模型与重算策略，表与索引已就绪';

COMMENT ON TABLE factopia.artifact IS
    '二进制产物登记表；P3 尚未启用（parquet/qlib 仍直接留文件系统）。'
    '启用后用于"按 sha256 校验 + 清理归档"，rel_path 相对 runtime/';

INSERT INTO factopia.migration_file (path, domain, sha256, status)
VALUES ('migrations/005_reserved_tables.sql', 'schema', '', 'done')
ON CONFLICT (path) DO NOTHING;

COMMIT;
