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

三个页面：研究立项 → 审核 → 结果（3D 图表）。由两个可独立部署的服务组成：`web`（nginx + 静态页面）与 `api`（FastAPI，只读结果文件）。

```bash
# 1. 导出结果（每次运行 RD-Agent 或重新评估后执行）
DC="docker compose -f docker-compose.yml -f compose.local.yml"
$DC run --rm --entrypoint "" rdagent sh -c 'cd /workspace/factopia && /opt/conda/envs/rdagent/bin/python -m factopia.export.rdagent_run'
$DC run --rm app python -m factopia.export.backtests
$DC run --rm app python -m factopia.export.alpha158

# 2a. 部署方式（与云服务器相同的镜像）→ http://localhost:18080
docker compose --profile platform up -d --build api web

# 2b. 前端开发（热更新）→ http://localhost:5173
$DC --profile dev up -d api web-dev
```

**部署到云服务器：** 在服务器上 `git clone` 后执行同样的命令（`--profile platform`）。`api` 只读挂载 `runtime` 卷，结果由服务器上的数据与挖掘流程生成；`web` 的字体和图表库已打包进镜像，不访问任何外部 CDN。
默认只监听本机；对外开放前**必须**在前面加 HTTPS 反向代理与登录（目前接口没有身份验证），再在 `.env` 中设置 `FACTOPIA_WEB_BIND=0.0.0.0`、`FACTOPIA_WEB_PORT`。

## 架构

| 服务 | 说明 |
|---|---|
| `app` | 主应用（Python 3.12）：数据、因子评估、组合 |
| `rdagent` | RD-Agent 主程序（conda / Python 3.11，源码固定 v1.0.0，位于 `/opt/rdagent-src`） |
| `dind` | 独立 Docker 引擎；RD-Agent 启动的回测容器运行在这里 |
| `rdagent-ui` | RD-Agent 日志界面（profile `ui`） |
| `qlib-runner` | 回测镜像，只构建（profile `build-only`），载入 dind 后由 RD-Agent 按需启动 |
| `api` | Factopia 后端接口（FastAPI，profile `platform`），只读结果文件，镜像不含 Qlib / RD-Agent |
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
