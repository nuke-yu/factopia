# 团队专有因子库接入（BR-35；Spec 第 14.18 节）

平台的内部因子库（`factopia/research/library.py`）由多个来源组成：Alpha158 公开基准、获批信号，以及本目录下的团队专有因子。
把专有因子库导出为本目录下的 YAML 文件（每个来源一个文件）即可接入；不需要改代码。因子库页面与语义检索会自动包含这些条目。

```yaml
source_name: team_core          # 来源名称（条目编号前缀）
version: 2026.10                # 版本，记录在每个条目的来源中
factors:
  - name: EARN_REV_3M           # 因子名称（同一来源内唯一）
    status: 生产中               # 例：生产中 / 研究中 / 已停用
    group: 盈利预期              # 因子组
    description: 过去 3 个月分析师盈利预测上调比例
    formulation: "\\frac{N_{up}-N_{down}}{N_{total}}"
    code: null                  # 可选：计算代码（与 RD-Agent 因子相同的格式）
    markets:                    # 可选：各市场统计（请注明时间段，避免泄漏测试期结果）
      hk: {rank_ic: 0.021, t: 2.4}
    label: 三市场通用            # 可选：迁移标签
    period_note: 2015–2021 样本内
```

注意：提供给 AI 起草研究规格时只使用名称与说明，不含任何数值。
