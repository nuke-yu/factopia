# Factopia 后端接口：独立部署的轻量镜像（不含 Qlib / RD-Agent）。读取导出的结果文件；
# 研究规格的起草（调用大模型）、版本与运行任务写入 runtime 卷，由 runner 服务执行运行。
# 构建上下文为仓库根目录：docker compose build api
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    FACTOPIA_RESULTS_DIR=/workspace/runtime/results \
    FACTOPIA_CONFIGS_DIR=/workspace/configs

WORKDIR /workspace/factopia

COPY docker/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 代码与配置随镜像发布，云端部署无需挂载源码（本地开发时由 compose.local.yml 挂载覆盖）
COPY factopia/__init__.py factopia/__init__.py
COPY factopia/api factopia/api
COPY factopia/research factopia/research
COPY factopia/rules factopia/rules
COPY factopia/portfolio factopia/portfolio
COPY factopia/approval factopia/approval
COPY factopia/signals factopia/signals
COPY factopia/factors/__init__.py factopia/factors/stats.py factopia/factors/families.py factopia/factors/
COPY factopia/validate/__init__.py factopia/validate/metrics.py factopia/validate/
COPY factopia/text/__init__.py factopia/text/common.py factopia/text/review.py factopia/text/understand.py factopia/text/search.py factopia/text/
COPY configs/text /workspace/configs/text
COPY configs/refdata /workspace/configs/refdata
COPY configs/universe /workspace/configs/universe
COPY configs/research /workspace/configs/research
COPY configs/mandates /workspace/configs/mandates
COPY configs/rules /workspace/configs/rules
COPY configs/rdagent/context /workspace/configs/rdagent/context

RUN useradd --uid 10001 --no-create-home factopia
USER factopia

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "factopia.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
