"""研究规格：起草、版本、提示词生成与运行任务（Spec 第 16 章）。

不依赖 Qlib 与 RD-Agent，供后端接口（api 容器）、RD-Agent 场景子类与后台运行器（rdagent 容器）共用：

- periods.py：交易日历与训练 / 验证 / 测试时间段
- context.py：发给 RD-Agent 的场景信息（市场介绍、预测周期、已有发现、研究规格）；预览与实际运行用同一段代码（RS-07）
- mandates.py：模拟客户授权的读取与解析（第 16.5 节）
- evidence.py：证据包（测试期之前的 Alpha158 发现、以往运行的假设与采纳结果）
- draft.py：大模型起草研究规格（RS-02）
- store.py：研究规格版本与运行任务的存储（RS-08、RS-09）
"""
