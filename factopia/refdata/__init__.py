"""参考数据与时间点一致性（业务需求 BR-13、16、17、18、36；Spec 第 14.10 节）。

- master：证券主数据——发行人、法律实体（GLEIF LEI）、证券标识（OpenFIGI）、行业、财务年度、报告币种、会计口径、
  主要上市地、跨市场挂牌关系（同一股份多地上市、存托凭证、同一发行人的其他股份类别如 A 股 / H 股）
- actions：公司行动——分红、拆股（Yahoo Finance）、代码变更（AMF 历史记录）、疑似停牌（行情缺失与零成交）
- membership：股票池成分股的起止日期（时间点一致的股票池），Qlib 股票池按此生成
- pit：各数据域的三个时间与时间点一致性状态，供所有结果页面标注

数据在 /workspace/runtime/refdata/；运行：python -m factopia.refdata --help
"""
