# Factopia 主应用镜像。基础镜像为多架构（ARM / x86），Mac、Windows、云服务器通用。
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.6.11 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# 编译 Qlib 的 Cython 扩展需要编译器；从 Git 安装需要 git
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# 先只复制依赖声明，依赖不变时可复用缓存层
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project

# 源码在本地开发时通过挂载提供（compose.local.yml），云端部署时再 COPY
CMD ["python", "-c", "print('factopia app ready')"]
