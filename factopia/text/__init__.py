"""多语言公告与文本信号（Spec 第 11 章；业务需求 BR-08 至 12、15、49）。

流程：① 采集（amf / demo）→ ② 理解（understand：概念标签、翻译、置信度、复核标记）→ 复核（review）
→ ③ 打分与 ④ 时间对齐（signal：与上一期比较、按实际可获取时间生效）→ ⑤ 三市场检验（signal.evaluate）。
检索（search）：任意语言提问 → 统一概念标签 → 原文与译文中带引用的段落。

数据集：
- fr：法国 AMF 开放数据（真实公告，Licence Ouverte 2.0）
- demo_hk、demo_uk：模拟数据（虚构公司，植入已知规律），仅用于流程演示，待取得授权数据后按相同格式替换

全部数据在 /workspace/runtime/text/<数据集>/ 下；运行：python -m factopia.text --help
"""
