# Factopia

AI 驱动的跨境量化股票策略研究平台（香港 / 英国 / 法国）——最小 POC。

- 需求规格：[docs/Factopia-最小POC-Spec.md](docs/Factopia-最小POC-Spec.md)
- 全局设计：[docs/Factopia-全局设计与最小POC.md](docs/Factopia-全局设计与最小POC.md)（开发环境见第 7 节）

## 快速开始

所有运行都在 Docker 中进行，**配置中只使用容器内路径**，数据放在 Docker 命名卷中（挂载点 `/workspace/runtime`）。
Windows 同事请在 WSL2（Ubuntu）中操作。

```bash
cp .env.example .env          # 填写 API key
docker compose build app rdagent
docker compose --profile build-only build qlib-runner

# 启动独立的 Docker 引擎（dind），并把回测镜像载入其中（回测镜像更新后需重新执行）
docker compose up -d dind
docker save factopia-qlib:dev | docker compose exec -T dind docker load
```

### 常用命令

```bash
# 三个市场的行情、汇率、交易日历
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python spikes/01_data_sources.py

# 数据：下载 → 标准化 → 转为 Qlib 数据集（含 RD-Agent 用的截至 2023-12-31 版本）→ 验证
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.universe
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.download
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.standardize
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.to_qlib
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.to_qlib --rdagent
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python spikes/03_verify_qlib_data.py

# Alpha158 三市场评估与迁移性分类 → /workspace/runtime/results/alpha158/
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.factors.evaluate

# RD-Agent 各市场模板的持股数按股票池比例设置（股票池变化后重新运行）
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.mining.topk

# RD-Agent：环境检查 / 在指定市场（hk / uk / fr）跑一轮因子挖掘
docker compose run --rm rdagent rdagent health_check
docker compose -f docker-compose.yml -f compose.local.yml run --rm -e FACTOPIA_MARKET=hk rdagent rdagent fin_factor --loop-n 1

# RD-Agent 日志界面 → http://localhost:19899（RDAGENT_UI_MARKET 选择市场，默认 hk）
RDAGENT_UI_MARKET=hk docker compose -f docker-compose.yml -f compose.local.yml --profile ui up -d rdagent-ui

# 查看命名卷中的文件
docker compose run --rm rdagent ls /workspace/runtime
```

## Factopia Web 界面

五个页面：研究项目 → 研究立项 → 审核 → 运行中 → 结果。页面上完成整个研究闭环：AI 起草研究规格、审核修改、锁定版本、发起运行、实时查看过程、查看结果（Spec 第 16 章）。

| 服务 | 作用 |
|---|---|
| `web` | nginx + 静态页面，转发 `/api` |
| `api` | 后端接口：读结果；AI 起草（调用 `.env` 中的大模型）；版本与任务写入 `runtime/research`、`runtime/jobs` |
| `runner` | 后台运行器：并行执行任务（最多 `FACTOPIA_MAX_PARALLEL` 个，默认 3；每个任务使用独立的 RD-Agent 源码副本 `runtime/rdagent_src/<任务>` 与工作目录 `runtime/rdagent/<市场>/h<N>/jobs/<任务>`），启动 RD-Agent，每 15 秒导出一次进度，超出费用上限或收到停止请求时只中止该任务；研究完成后自动排入可信度检查（在 dind 的回测镜像中运行，无网络，约 15 分钟，不调用大模型） |
| `exporter` | 运行结束后导出逐日回测、持仓、IC 与分组收益（需要 Qlib） |

```bash
# 启动全部服务（本机；源码挂载，修改代码后 api 自动重载）→ http://localhost:18080
docker compose -f docker-compose.yml -f compose.local.yml --profile platform --profile research up -d --build

# 查看后台运行器与导出日志
docker compose -f docker-compose.yml -f compose.local.yml --profile research logs -f runner exporter

# 前端开发（热更新）→ http://localhost:5173
docker compose -f docker-compose.yml -f compose.local.yml --profile dev up -d api web-dev

# 手动导出（命令行启动的运行、重新评估 Alpha158 后）
DC="docker compose -f docker-compose.yml -f compose.local.yml"
$DC run --rm --entrypoint "" rdagent sh -c 'cd /workspace/factopia && /opt/conda/envs/rdagent/bin/python -m factopia.export.rdagent_run'
$DC run --rm app python -m factopia.export.backtests
$DC run --rm app python -m factopia.export.alpha158
```

- 任务按顺序执行、不并行：RD-Agent 每次运行都会改写共享源码卷中的配置模板，并使用按市场与周期固定的工作目录。
- 命令行方式（`FACTOPIA_RESEARCH=... rdagent fin_factor`）仍可用，与页面走同一个启动脚本；锁定的版本在 `/workspace/runtime/research/<编号>/v<k>.yaml`。
- 提示词预览与 RD-Agent 实际收到的内容由同一个函数生成（`factopia/research/context.py`）。

**部署到云服务器：** `web` 与 `api` 镜像可独立部署（`--profile platform`）；`runner`、`exporter` 需要与 `dind` 同机，并使用挂载的源码（`compose.local.yml`），打包进镜像是后续工作。
默认只监听本机；对外开放前**必须**在前面加 HTTPS 反向代理与登录（目前接口没有身份验证），再在 `.env` 中设置 `FACTOPIA_WEB_BIND=0.0.0.0`、`FACTOPIA_WEB_PORT`。

## 架构

| 服务 | 说明 |
|---|---|
| `app` | 主应用（Python 3.12）：数据、因子评估、组合 |
| `rdagent` | RD-Agent 主程序（conda / Python 3.11，源码固定 v1.0.0，位于 `/opt/rdagent-src`） |
| `dind` | 独立 Docker 引擎；RD-Agent 启动的回测容器运行在这里 |
| `rdagent-ui` | RD-Agent 日志界面（profile `ui`） |
| `qlib-runner` | 回测镜像，只构建（profile `build-only`），载入 dind 后由 RD-Agent 按需启动；启动钩子 `docker/qlib/sitecustomize.py` 让打分相同的股票按股票代码确定排序，回测可复现（修改后需重新构建并载入 dind） |
| `api` | Factopia 后端接口（FastAPI，profile `platform`）：读结果、AI 起草、版本与任务；镜像不含 Qlib / RD-Agent |
| `runner` | 后台运行器（rdagent 镜像，profile `research`）：按顺序执行页面发起的研究任务 |
| `exporter` | 回测结果导出（app 镜像，profile `research`） |
| `web` | Factopia 前端（nginx，profile `platform`），转发 `/api` 到 `api` |
| `web-dev` | 前端开发服务器（仅 compose.local.yml，profile `dev`） |

| 命名卷 | 内容 |
|---|---|
| `runtime` → `/workspace/runtime` | Qlib 数据（`qlib_home/`）、RD-Agent 工作目录 / 日志 / 签名密钥（`rdagent/`）、运行日志（`logs/`） |
| `rdagent_src` → `/opt/rdagent-src` | RD-Agent 源码，rdagent 与 dind 共享（RD-Agent 会把自身目录下的模板挂载给回测容器） |
| `dind_data` | dind 中的镜像 |

## 目录

| 路径 | 内容 |
|---|---|
| `docker/` | 各镜像的 Dockerfile（均为多架构） |
| `factopia/export/` | 结果导出：RD-Agent 日志、回测与持仓、Alpha158 汇总 → `/workspace/runtime/results/` |
| `factopia/research/` | 研究规格：授权解析、证据包、AI 起草、提示词生成、版本与任务存储 |
| `factopia/runner.py` | 后台运行器 |
| `factopia/validate/` | 可信度检查：AI 未见过的 2024 年以后数据、随机因子对照、多种子回测（Spec 第 14.7 节） |
| `factopia/text/` | 多语言公告与文本信号：法国 AMF 真实公告、香港 / 英国模拟数据，概念标签 / 译文 / 置信度、语言专家复核、按实际可获取时间生成信号、三市场检验、带引用的检索与 AI 摘要（Spec 第 14.9 节）；`python -m factopia.text all`，页面「情报台」 |
| `configs/text/glossary.yaml` | 四类风险概念与中 / 英 / 法术语表（带版本） |
| `factopia/refdata/` | 参考数据与时间点一致性：证券主数据（GLEIF 法律实体、OpenFIGI 标识、跨市场挂牌与存托凭证、主要上市地、行业、财务年度、会计口径）、公司行动、成分股起止日期、各数据域的时间点状态（Spec 第 14.10 节）；`python -m factopia.refdata all`，页面「数据台」 |
| `factopia/diagnostics/` | 研究结果诊断：成本与换手、流动性与容量、汇率影响、风险暴露、中性化与定义稳定性、经济意义（Spec 第 14.11 节）；导出后自动运行，`python -m factopia.diagnostics` |
| `factopia/rules/` | 地区规则包与预检：通用 / 香港 / 英国 / 法国规则包与数据来源登记表（`configs/rules/`），立项锁定前与组合生成后两次预检、建议审批路径；成本与税费从规则包读取（Spec 第 14.12 节）；`python -m factopia.rules`，页面「风控台」 |
| `factopia/portfolio/` | 授权组合与研究报告：月度只做多组合（约束逐条注明来源）、多币种回测与汇率拆分、Brinson 归因、反馈闭环；中文主报告 + 英文 / 法文证据附件（Spec 第 14.13 节）；页面「组合台」 |
| `factopia/approval/` | 跨地区审批与审计：提交前置检查、冻结提交材料与指纹、六类角色按序审批、例外处理、获批锁定、哈希链留痕、审计证据包（Spec 第 14.14 节）；页面「审批台」 |
| `factopia/signals/` | 信号目录与上线后监控：按研究版本 × 市场分版本、标签、逐市场暂停 / 恢复 / 停用；冻结因子代码重算上线期信号，11 类监控指标与预警（Spec 第 14.15 节）；页面「信号目录」「总览」 |
| `configs/refdata/`、`configs/universe/membership/` | 市场规则、数据字典、人工确认；成分股起止日期（目前为当前名单） |
| `configs/mandates/` | 模拟客户授权（不含真实客户信息） |
| `factopia/api/` | 后端接口 |
| `web/` | 前端（React 19、Vite、Tailwind v4、ECharts GL、React Three Fiber） |
| `spikes/` | 跑通验证脚本 |
| `docs/` | 需求规格与全局设计 |
| `vendor/` | RD-Agent、Qlib 源码，仅供阅读（不入库，不参与运行） |

## 版本约定

- Qlib 从源码编译，固定到提交 `2fb9380`（与 RD-Agent 官方镜像一致）。PyPI 上的 pyqlib 没有 Linux ARM 安装包。
- `mlflow<3.13`：Qlib 的实验记录依赖 MLflow 文件存储，3.13 起默认关闭。
- 依赖由 `uv.lock` 锁定。
- RD-Agent 镜像基于 Miniforge（conda-forge 源），不使用需要接受服务条款的 Anaconda 默认源。
