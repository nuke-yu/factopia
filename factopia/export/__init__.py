"""结果导出：把分散在各处的运行结果整理为统一格式，供 Web 界面读取（Spec MINE-04、05，UI-07）。

- rdagent_run.py：读取 RD-Agent 日志（在 rdagent 容器中运行，日志对象需要 RD-Agent 环境才能读取）
- backtests.py：读取 Qlib 回测结果与持仓（在 app 容器中运行，需要 Qlib）
- alpha158.py：汇总 Alpha158 评估结果与市场概况（在 app 容器中运行）

输出目录 /workspace/runtime/results/（容器内路径）：
    runs/<运行编号>/run.json、rounds.json、backtest/、positions/
    alpha158/h<N>/cube.json、surface.json
    overview.json
"""
