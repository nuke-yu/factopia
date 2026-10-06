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

# RD-Agent 环境检查 / 跑一轮因子挖掘
docker compose run --rm rdagent rdagent health_check
docker compose run --rm rdagent rdagent fin_factor --loop-n 1

# RD-Agent 日志界面 → http://localhost:19899
docker compose --profile ui up -d rdagent-ui

# 查看命名卷中的文件
docker compose run --rm rdagent ls /workspace/runtime
```

## 架构

| 服务 | 说明 |
|---|---|
| `app` | 主应用（Python 3.12）：数据、因子评估、组合 |
| `rdagent` | RD-Agent 主程序（conda / Python 3.11，源码固定 v1.0.0，位于 `/opt/rdagent-src`） |
| `dind` | 独立 Docker 引擎；RD-Agent 启动的回测容器运行在这里 |
| `rdagent-ui` | RD-Agent 日志界面（profile `ui`） |
| `qlib-runner` | 回测镜像，只构建（profile `build-only`），载入 dind 后由 RD-Agent 按需启动 |

| 命名卷 | 内容 |
|---|---|
| `runtime` → `/workspace/runtime` | Qlib 数据（`qlib_home/`）、RD-Agent 工作目录 / 日志 / 签名密钥（`rdagent/`）、运行日志（`logs/`） |
| `rdagent_src` → `/opt/rdagent-src` | RD-Agent 源码，rdagent 与 dind 共享（RD-Agent 会把自身目录下的模板挂载给回测容器） |
| `dind_data` | dind 中的镜像 |

## 目录

| 路径 | 内容 |
|---|---|
| `docker/` | 各镜像的 Dockerfile（均为多架构） |
| `spikes/` | 跑通验证脚本 |
| `docs/` | 需求规格与全局设计 |
| `vendor/` | RD-Agent、Qlib 源码，仅供阅读（不入库，不参与运行） |

## 版本约定

- Qlib 从源码编译，固定到提交 `2fb9380`（与 RD-Agent 官方镜像一致）。PyPI 上的 pyqlib 没有 Linux ARM 安装包。
- `mlflow<3.13`：Qlib 的实验记录依赖 MLflow 文件存储，3.13 起默认关闭。
- 依赖由 `uv.lock` 锁定。
- RD-Agent 镜像基于 Miniforge（conda-forge 源），不使用需要接受服务条款的 Anaconda 默认源。
