# Factopia 后端接口：独立部署的轻量镜像（不含 Qlib / RD-Agent）。读取导出的结果文件；
# 研究规格的起草（调用大模型）、版本与运行任务写入 runtime 卷，由 runner 服务执行运行。
# 构建上下文为仓库根目录：docker compose build api
# pip 源可通过 --build-arg PIP_INDEX_URL=... 覆盖（默认官方 PyPI；国内构建走镜像站，见 compose.cloud.yml）
ARG PIP_INDEX_URL=https://pypi.org/simple

FROM python:3.12-slim

ARG PIP_INDEX_URL
ENV PYTHONUNBUFFERED=1 \
    FACTOPIA_RESULTS_DIR=/workspace/runtime/results \
    FACTOPIA_CONFIGS_DIR=/workspace/configs

WORKDIR /workspace/factopia

COPY docker/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt

# 代码与配置随镜像发布，云端部署无需挂载源码（本地开发时由 compose.local.yml 挂载覆盖）
# 整包复制：早先按模块白名单逐个 COPY，新增模块（如 factopia/lang.py）会漏掉，
# 镜像单独运行时报 ModuleNotFoundError；只靠源码挂载才不出问题，代价是镜像不自足。
COPY factopia ./factopia
COPY configs/text /workspace/configs/text
COPY configs/refdata /workspace/configs/refdata
COPY configs/universe /workspace/configs/universe
COPY configs/research /workspace/configs/research
COPY configs/mandates /workspace/configs/mandates
COPY configs/rules /workspace/configs/rules
COPY configs/acceptance /workspace/configs/acceptance
COPY configs/factor_library /workspace/configs/factor_library
COPY configs/access /workspace/configs/access
COPY configs/rdagent/context /workspace/configs/rdagent/context

RUN useradd --uid 10001 --no-create-home factopia
USER factopia

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "factopia.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
