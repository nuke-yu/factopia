# rdagents/

RD-Agent v1.0.0（github.com/microsoft/RD-Agent）的源码副本，已并入本仓库统一管理。

- 用途：`runner` 容器通过 `rdagent fin_factor` 调用它执行因子挖掘循环（AI 生成因子 → Qlib 容器回测 → 评估 → 迭代）
- 版本：`.factopia_version` 文件记录当前对应的上游版本
- 构建：`docker/rdagent/Dockerfile` 构建时将源码安装到镜像 `/opt/rdagent-src`；本地开发时由 `compose.local.yml` 挂载覆盖配置模板
- 扩展：不改此目录代码，Factopia 的定制通过 `factopia/mining/rdagent_ext/`（PYTHONPATH 注入）实现
- 许可：见 `rdagents/LICENSE`（MIT，Copyright (c) Microsoft Corporation）；全文亦见仓库根 `THIRD_PARTY_NOTICES.md`

核心代码入口：

| 路径 | 内容 |
|---|---|
| `rdagent/app/` | CLI 入口（`rdagent fin_factor` 等场景命令） |
| `rdagent/scenarios/qlib/` | 量化因子挖掘场景：实验循环、因子/模型/数据处理模板 |
| `rdagent/oai/` | LLM 调用后端（LiteLLM） |
| `web/` | RD-Agent 自带 Web UI（Vue，本项目的 `rdagent-web` 服务用它，未默认启用） |
| `docs/` | 上游技术文档 |
