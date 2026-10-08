"""RD-Agent 扩展（在 rdagent 容器中运行，只依赖 RD-Agent 自身环境中已有的库）。

- scenario.py：QlibFactorScenario 子类，向 AI 提供场景信息（Spec MINE-07）
- prepare_run.py：按市场和预测周期准备一次运行（Spec MINE-09）

均通过环境变量接入 RD-Agent，不修改其源码。
"""
