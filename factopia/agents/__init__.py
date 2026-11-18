"""研究迭代 Agent（WF-02，Spec 第 18 章）。

以一次已完成的研究版本为输入，按 OBSERVE → DIAGNOSE → PLAN → GATE → ACT → EVALUATE
的显式状态机运行；RD-Agent 只是它调用的工具，通过现有 runner 任务执行。

P0 只实现事实包、诊断、规划、new_version 草稿与报告；不接 API / 前端 / runner。
"""
__all__ = ["store", "policy", "observe", "tools", "llm", "loop", "report", "brief", "dig"]
