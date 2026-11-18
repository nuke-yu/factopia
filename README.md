# Factopia

AI 驱动的跨境量化股票策略研究平台（香港 / 英国 / 法国）——最小 POC。

- 需求规格：[docs/Factopia-最小POC-Spec.md](docs/Factopia-最小POC-Spec.md)
- 全局设计：[docs/Factopia-全局设计与最小POC.md](docs/Factopia-全局设计与最小POC.md)（开发环境见第 7 节）
- 模拟盘规格：[docs/Factopia-模拟盘Spec.md](docs/Factopia-模拟盘Spec.md)（信号接入与两种撮合模式的语义）

## 快速开始

所有运行都在 Docker 中进行，**配置中只使用容器内路径**，数据放在 Docker 命名卷中（挂载点 `/workspace/runtime`）。
Windows 同事请在 WSL2（Ubuntu）中操作。

```bash
cp .env.example .env          # 填写 API key
docker compose build app rdagent
docker compose --profile build-only build qlib-runner
# 如果不成功，用国内镜像build qlib-runner
# docker compose --profile build-only build --build-arg QLIB_GIT_URL=https://gitee.com/mirrors/qlib.git qlib-runner


# 启动独立的 Docker 引擎（dind），并把回测镜像载入其中（回测镜像更新后需重新执行）
docker compose up -d dind
docker save factopia-qlib:dev | docker compose exec -T dind docker load
# if fails, then try below cmd
# cmd /c "docker save factopia-qlib:dev | docker compose exec -T dind docker load"

# dind 起不来时（日志出现 failed to start containerd ... PID ... still running）：删除并重建容器；
# dind_data 卷中的镜像不会丢。等约 20 秒后确认：
# docker compose rm -sf dind && docker compose up -d dind
# docker compose exec -T dind docker info
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

# 结果页的 Alpha158 迁移性立方体 / 时间曲面（读取上一步的逐日 IC）→ cube.json / surface.json
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.export.alpha158 --horizon 20

# RD-Agent 各市场模板的持股数按股票池比例设置（股票池变化后重新运行）
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.mining.topk

# RD-Agent：环境检查 / 在指定市场（hk / uk / fr）跑一轮因子挖掘
docker compose run --rm rdagent rdagent health_check
docker compose -f docker-compose.yml -f compose.local.yml run --rm -e FACTOPIA_MARKET=hk rdagent rdagent fin_factor --loop-n 1

# 研究迭代 Agent（P0/P1）：先生成 v2 草稿并停在人工闸门（awaiting_human）
DC="docker compose -f docker-compose.yml -f compose.local.yml"
$DC run --rm app python -m factopia.agents run --research <研究编号> --version <版本>
$DC run --rm app python -m factopia.agents show <agent_id>       # 查看事实/诊断/计划/next_version_draft.json
$DC run --rm app python -m factopia.agents approve <agent_id> --file <edited.json> --run   # 审改后批准并自动运行
$DC run --rm app python -m factopia.agents approve <agent_id> --no-run                      # 只保存 vN+1，不运行
$DC run --rm app python -m factopia.agents reject <agent_id> --reason "..."                  # 拒绝本轮
$DC run --rm app python -m unittest tests.test_agents tests.test_agents_p1 -v
# 页面：结果页「继续研究」→ 让 Agent 自动迭代；Agent 页 /agents（审改、批准、trace、报告）
# 演示脚本：docs/Agentic-workflow-结果-研究迭代.md（含规则预检、自动线索回流、因子库重叠、红队与讲解口径）
# runner 生命周期测试（rdagent 镜像；Windows 路径按实际仓库位置替换）
# MSYS_NO_PATHCONV=1 $DC run --rm -T -w /workspace -v "D:/sandbox/projects/factopia/tests:/workspace/tests:ro" --entrypoint "" runner python -m unittest tests.test_runner_agent -v

# RD-Agent 日志界面 → http://localhost:19899（RDAGENT_UI_MARKET 选择市场，默认 hk）
RDAGENT_UI_MARKET=hk docker compose -f docker-compose.yml -f compose.local.yml --profile ui up -d rdagent-ui

# 查看命名卷中的文件
docker compose run --rm rdagent ls /workspace/runtime

# 模拟盘：每日推进（结算挂单 → 重算信号 → 月度调仓 → 出报告），加参数 report 只重算报告（详见下文「模拟盘」一节）
docker compose -f docker-compose.yml -f compose.local.yml exec runner python -m factopia.paper
# 长桥模拟盘需在 .env 填 LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN（港股走长桥，UK/FR 自动影子撮合）

# 测试UT
docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m unittest tests.test_agents -v

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
docker compose -f docker-compose.yml -f compose.local.yml --profile platform --profile research up -d


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

**部署到云服务器：** 见下一节「部署到阿里云 ECS」。`web` 与 `api` 镜像可独立部署（`--profile platform`）；`runner`、`exporter` 需要与 `dind` 同机，并使用挂载的源码（`compose.cloud.yml`），打包进镜像是后续工作。
默认只监听本机；对外开放前**必须**在前面加 HTTPS 反向代理与登录（目前接口没有身份验证），再在 `.env` 中设置 `FACTOPIA_WEB_BIND=0.0.0.0`、`FACTOPIA_WEB_PORT`。

## 部署到阿里云 ECS

当前部署：`AI-Campaign-2026-Team12`（cn-beijing，2 核 / 7G / 79G 系统盘 / 固定带宽 3Mbps），公网 <http://101.201.239.22/>，代码在 `/opt/factopia`。

### 这台机器的三处硬约束

| 约束 | 影响 | 处理 |
|---|---|---|
| Docker Hub 直连不通（`registry-1.docker.io` 超时） | 基础镜像拉不下来 | `/etc/docker/daemon.json` 配 `registry-mirrors: docker.m.daocloud.io, docker.1ms.run`；dind 用 `.env` 的 `DIND_REGISTRY_MIRROR` |
| ghcr.io 通但被限速到 ~11KB/s | `app` 镜像的 `uv` 拷贝层要下 16.74MB，约 20 分钟 | 已下完并进缓存；换机器时先 `docker pull ghcr.m.daocloud.io/astral-sh/uv:0.6.11` 再 `docker tag` 回 `ghcr.io/astral-sh/uv:0.6.11` |
| 安全组只放行 22 / 80（18080 静默丢包） | 默认的 18080 公网访问不到 | `.env` 里 `FACTOPIA_WEB_PORT=80`，用根路径访问 |

内存 7G 偏紧：回测容器限 3G（`QLIB_DOCKER_MEM_LIMIT=3g`、`QLIB_DOCKER_SHM_SIZE=1g`），`FACTOPIA_MAX_PARALLEL=1` 一次只跑一个研究任务；另加了 4G swap 兜底。

首次构建耗时（2 核 / 3Mbps）：api 5 分钟、web 1 分钟、app 24 分钟（含 ghcr 限速）、qlib-runner 31 分钟（xgboost 3 还会拖 336MB 的 `nvidia-nccl-cu12`，CPU 机器上用不到，可考虑把 Dockerfile 里的 `xgboost` 改成 `"xgboost<3"`）、rdagent 13 分钟。

### 首次部署

```bash
# 本机：打包（排除 runtime 里的 rdagent_src、node_modules、.git、.venv）
tar -czf src.tgz --exclude=./runtime --exclude=./.git --exclude=./.venv \
    --exclude=./web/node_modules --exclude=./web/dist --exclude='*/__pycache__' .
tar -czf runtime.tgz --exclude=runtime/rdagent_src runtime

scp src.tgz runtime.tgz root@101.201.239.22:/opt/factopia/
scp .env.cloud root@101.201.239.22:/opt/factopia/.env      # 云端 env：0.0.0.0:80、国内镜像源、内存下调

# 服务器
cd /opt/factopia && tar xzf src.tgz && tar xzf runtime.tgz && rm src.tgz runtime.tgz
printf 'factopia:%s\n' "$(openssl passwd -apr1 '<密码>')" > docker/htpasswd   # Basic Auth
bash scripts/deploy_cloud.sh      # 修属主 → 构建 → 起 dind 载入回测镜像 → 生成 qlib / 文本数据集 → 起服务
```

`deploy_cloud.sh` 幂等，改代码后重跑即可（镜像层会复用缓存）。只改 Python 代码时可跳过构建：`docker restart factopia-api-1 factopia-runner-1`（源码是挂载进容器的）。

**属主是必做的一步**：`api` 镜像以 `uid=10001(factopia)` 运行，而 `runtime` 是从本地打包上传的，里面文件的属主是打包机的 uid（Windows 上是 `197609`）。本地 Docker Desktop 的 9p 挂载不校验 uid，Linux ext4 会真的校验——漏了这步所有写操作都报 `PermissionError`，页面显示成「研究存储目录尚未初始化：请先启动后台运行器」。

```bash
chown -R 10001:10001 runtime
find runtime -type d -exec chmod 2775 {} +
apt-get install -y acl   # 可选：让 runner(root) 之后新建的文件 api 也能改
find runtime -type d -exec setfacl -d -m u:10001:rwx,u::rwx,g::rwx,o::rx {} +
```

### 与本地开发的差异

- `compose.cloud.yml` 取代 `compose.local.yml`：runtime 用绑定挂载 `./runtime`（Linux ext4，不需要本地 Windows 那套命名卷覆盖），不继承本地 Clash / IPv6 的 `extra_hosts`。
- 前端多一层 nginx Basic Auth（`docker/web/cloud.conf.template`，密码文件 `docker/htpasswd` 不入库），`/healthz` 豁免。
- 构建走国内源：`APT_MIRROR` / `UV_DEFAULT_INDEX` / `PIP_INDEX_URL`（阿里云）、`NPM_REGISTRY`（npmmirror）。

### 常见报错

| 页面提示 | 真实原因 | 处理 |
|---|---|---|
| 研究存储目录尚未初始化：请先启动后台运行器 | api 以 uid 10001 运行，runtime 文件属主是打包机 uid，写入被拒（`PermissionError` 被统一转成这条 503，与 runner 无关） | 见上一节的 `chown -R 10001:10001 runtime` |
| 该数据集尚未生成文本信号（先运行 python -m factopia.text all） | `runtime/text/<数据集>/summary.json` 不存在。`fr` 是真实 AMF 数据，要联网采集；`demo_hk`/`demo_uk` 是模拟数据 | `$DC run --rm app python -m factopia.text demo && $DC run --rm app python -m factopia.text all --lexicon`（不调用大模型）；`--yes --budget 3` 才走大模型标注，每数据集约 $2.3 |
| RD-Agent 退出码 1：cannot import name 'ExpandingGroupby' | 镜像里装成 pandas 3，`pandarallel` 导入即崩 | 已在 `docker/rdagent/Dockerfile` 锁定 `pandas<3`；老任务的日志是历史记录，不代表当前环境 |
| 运行器重启，运行中断 | runner 启动时会把它上次没跑完的任务标记成这个状态（单机单实例的预期行为） | 重新发起即可；历史失败任务已归档到 `runtime/jobs/_archive_local_debug/` |

`/api/text/overview` 里 `ready: false` 的数据集前端会自动隐藏，不会报错——所以没采集 `fr` 时页面只显示两个模拟数据集。

### 运维

```bash
cd /opt/factopia
DC="docker compose -f docker-compose.yml -f compose.cloud.yml"
$DC ps
$DC logs -f --tail=100 api runner          # 后端 / 研究运行器
$DC exec dind docker ps                    # 回测容器
du -sh runtime/*; df -h                    # 79G 系统盘，runtime 目前 95M
```

- 演示数据在 `runtime/`：9 个运行、三市场行情、审批链、模拟盘记录，页面打开即有内容。
- `runtime/rdagent_markets/<市场>/qlib_data` 是 RD-Agent 用的数据集（截止 2023-12-31），由 `$DC run --rm app python -m factopia.data.to_qlib --rdagent` 生成；缺了它发起研究会报「缺少市场数据」。
- 已知限制：无 HTTPS（Basic Auth 凭据明文传输）；3Mbps 上行，首屏 + 图表加载偏慢；单机 dind，回测与 Web 争 2 核。


## 模拟盘（信号接入与撮合）

获批信号接入模拟盘后逐日推进：结算挂单 → 重算信号（与监控同一口径）→ 月度调仓 → 估值与报告。报告写入 `runtime/signals/<signal_id>/paper.json`，页面「信号目录」的模拟盘面板读取同一文件，含组合视图：建仓信息、当前持仓（数量 / 平均成本 / 现价 / 市值 / 持股比例 / 浮动盈亏）与个股贡献（已实现 + 浮动盈亏 − 交易成本）；运行建议经"模拟盘反馈"回流线索池。完整需求语义见 [docs/Factopia-模拟盘Spec.md](docs/Factopia-模拟盘Spec.md)。

### 两种撮合模式（接入时固化，运行期不可改）

| 模式 | 判定条件 | 撮合语义 | 适用 |
|---|---|---|---|
| 影子撮合（默认） | 未配置长桥，或信号非港股 | 截面日 T 收盘后决策挂单，T+1 起以开盘价逐日成交（开盘缺失用收盘价兜底），按规则包成本扣减现金，允许碎股 | 全部市场，无需账户 |
| 长桥 LongPort | `.env` 中 `LONGPORT_APP_KEY` / `LONGPORT_APP_SECRET` / `LONGPORT_ACCESS_TOKEN` 全部非空，且信号为港股 | 券商市价单（当日有效），数量按券商每手股数向下取整；限流（429002）按提示等待重试、连接错误短暂重试；持仓与现金按成交回报本地记账，券商查询留作对账 | 仅港股 |

两种模式可在同一进程内共存：模式在接入时写入 `runtime/paper/<signal_id>/enrolled.json`，之后按该文件的 `mode` 推进，不随环境变量变化。

### 接入做法

1. **页面**：「信号目录」→ 信号详情的模拟盘面板：接入 / 重置（重复接入 = 清空旧状态从头开始）/ 停止 / 手动推进。
2. **接口**（接入与推进以任务形式由 runner 执行）：

   | 端点 | 作用 |
   |---|---|
   | `POST /api/signals/{signal_id}/paper` | 接入 / 重置；body 可带 `capital`（默认 `FACTOPIA_PAPER_CAPITAL`，100 万） |
   | `POST /api/signals/{signal_id}/paper/stop` | 停用：长桥在途挂单撤销、本地挂单留痕取消，历史保留 |
   | `POST /api/signals/{signal_id}/paper/advance` | 手动推进一轮（定时调度之外的补充） |
   | `POST /api/research/{id}/versions/{v}/paper-demo` | 研究线结果一键接入演示线（获批市场生成信号并接入） |

3. **命令行**：

   ```bash
   # 全部接入中的信号各推进一轮（加参数 report 只重算报告）
   docker compose -f docker-compose.yml -f compose.local.yml exec runner python -m factopia.paper
   ```

   例外做法：需要把某个港股信号强制接入为影子模式（例如长桥账户额度不足）时，在 runner 容器内用脚本先清空 `LONGPORT_*` 环境变量再调用 `engine.enroll(signal_id)`，模式即固化为 shadow。

### 推进、调仓与数据依赖

- runner 每日自动排队一轮推进：`FACTOPIA_PAPER_ADVANCE_UTC_HOUR`（默认 UTC 9 = 港股 17:00 收盘后），每信号每 UTC 日最多自动排一个任务；
- runner 每日自动推进行情：`FACTOPIA_DATA_UPDATE_UTC_HOUR`（默认 UTC 19 = 北京 03:00，三市场前一交易日数据齐备后）后台执行 `download → standardize`，当日只跑一次（成败都记 `runtime/data/.daily_update`，失败当日不再重试、次日自动再试）；推进时刻须晚于数据更新时刻；
- 每轮幂等：行情没有新日期时只结算挂单并重算报告，不重复调仓；
- 月度调仓：每月首个有新数据的交易日，取最新截面前 20% 等权（与监控口径一致）；与当前持仓差额小于 5%×目标市值时跳过该笔；
- 影子撮合的成交、报告估值都依赖标准化行情推进到新日期——自动更新之外也可手动补跑（`docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.data.download` 与 `.../standardize`），不更新则挂单一直挂到有新数据为止；
- 长桥挂单超过 7 个自然日未成交自动撤销；下月调仓时未成交的旧挂单按剩余持仓 / 可用现金封顶缩放。

### 已知限制

- 长桥通道使用共享模拟账户：HKD 购买力（现金 + 融资额度）不足时新买单会被券商以 `Insufficient Credit Limit` 拒绝——被拒的信号可按上文做法切回影子撮合；
- 接入 / 重置必须在 runner 容器内执行（root）：API 进程（uid 10001）无法删除 runner 写过的模拟盘目录；
- 推进时刻默认在收盘后；要改到交易时段内推进，调整 `FACTOPIA_PAPER_ADVANCE_UTC_HOUR`。

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
| `factopia/text/` | 多语言公告与文本信号：法国 AMF 真实公告、香港 / 英国模拟数据，概念标签 / 译文 / 置信度、语言专家复核、按实际可获取时间生成信号、三市场检验、带引用的检索与 AI 摘要（Spec 第 14.9 节）；`python -m factopia.text all`，页面「情报台」；`text/research.py` 为文本信号研究（研究规格信号来源选"文本"后由后台运行器执行，输出与 RD-Agent 相同格式的结果，第 14.17 节） |
| `factopia/research/regions.py`、`library.py`、`semantic.py` | 地区差异清单；内部因子库（Alpha158、获批信号、团队专有因子接入口 `configs/factor_library/`）；知识检索（大模型三语改写 + BM25）（Spec 第 14.18 节）；页面「因子库」 |
| `factopia/access.py` | 演示用地区视角（不是登录鉴权）：按 `configs/access/regions.yaml` 过滤数据与信号，越权留痕（Spec 第 14.20 节）；页面顶部"地区视角" |
| `factopia/ideas/` | 研究线索池（首页"00 线索池"）：投资假设、论文 / 研报、研究员经验、人工反馈，以及监控预警、组合归因、审批退回、模拟盘反馈自动回流；敏感信息拦截、论文许可与候选因子抽取、防泄漏提示、转研究立项并可追溯（Spec 第 17 章） |
| `factopia/refdata/vendor.py` | 数据商参考数据导入：历史成分股与合并 / 配股 / 退市等公司行动，统一 CSV 模板（`configs/refdata/vendor_templates/`），逐行校验，`python -m factopia.refdata import-membership / import-actions`（Spec 第 14.21 节） |
| `factopia/sources.py` | 行情来源：真实标准化行情与指数，或模拟文本数据集的模拟行情（等权基准）；运行结果记录来源，诊断、组合、监控按运行读取 |
| `configs/text/glossary.yaml` | 四类风险概念与中 / 英 / 法术语表（带版本） |
| `factopia/refdata/` | 参考数据与时间点一致性：证券主数据（GLEIF 法律实体、OpenFIGI 标识、跨市场挂牌与存托凭证、主要上市地、行业、财务年度、会计口径）、公司行动、成分股起止日期、各数据域的时间点状态（Spec 第 14.10 节）；`python -m factopia.refdata all`，页面「数据台」 |
| `factopia/diagnostics/` | 研究结果诊断：成本与换手、流动性与容量、汇率影响、风险暴露、中性化与定义稳定性、经济意义（Spec 第 14.11 节）；导出后自动运行，`python -m factopia.diagnostics` |
| `factopia/rules/` | 地区规则包与预检：通用 / 香港 / 英国 / 法国规则包与数据来源登记表（`configs/rules/`），立项锁定前与组合生成后两次预检、建议审批路径；成本与税费从规则包读取（Spec 第 14.12 节）；`python -m factopia.rules`，页面「风控台」 |
| `factopia/portfolio/` | 授权组合与研究报告：月度只做多组合（约束逐条注明来源）、多币种回测与汇率拆分、Brinson 归因、反馈闭环；中文主报告 + 英文 / 法文证据附件（Spec 第 14.13 节）；页面「组合台」 |
| `factopia/approval/` | 跨地区审批与审计：提交前置检查、冻结提交材料与指纹、六类角色按序审批、例外处理、获批锁定、哈希链留痕、审计证据包（Spec 第 14.14 节）；页面「审批台」 |
| `factopia/signals/` | 信号目录与上线后监控：按研究版本 × 市场分版本、标签、逐市场暂停 / 恢复 / 停用；冻结因子代码重算上线期信号，11 类监控指标与预警（Spec 第 14.15 节）；页面「信号目录」「总览」 |
| `factopia/paper/` | 模拟盘：默认影子撮合（T 日收盘截面决策、T+1 开盘成交、规则包成本），可选长桥 LongPort 模拟账户（仅港股，非港股信号自动影子撮合、可与长桥共存）；月度 TOP 20% 等权调仓，报告含缺口感知年化波动、对等权基准的超额、rank IC 复检与预警建议；运行建议经"模拟盘反馈"渠道回流线索池（同监控口径）；`python -m factopia.paper`，页面「信号目录」接入 / 停止 |
| `factopia/acceptance/` | 验收：8 条成功标准自动核对、4 个量化目标度量（人工基线 `configs/acceptance/baseline.yaml`）、17 项红队测试（Spec 第 14.16 节）；页面「验收」 |
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
