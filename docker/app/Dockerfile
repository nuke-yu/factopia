# Factopia 主应用镜像。基础镜像为多架构（ARM / x86），Mac、Windows、云服务器通用。
# PyPI 源可通过 --build-arg UV_DEFAULT_INDEX=... 覆盖（默认官方；国内网络可传镜像加速）
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

FROM python:3.12-slim

ARG UV_DEFAULT_INDEX
ENV UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX}

COPY --from=ghcr.io/astral-sh/uv:0.6.11 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# 编译 Qlib 的 Cython 扩展需要编译器（pyqlib 来自本地 qlibs/ 源码，见 pyproject.toml）
# APT_MIRROR 构建参数可换 debian 源（国内网络传 mirrors.tuna.tsinghua.edu.cn 加速）
ARG APT_MIRROR=deb.debian.org
RUN sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# 先复制依赖声明与本地 qlibs 源码（pyqlib 为路径依赖），依赖不变时可复用缓存层
COPY pyproject.toml uv.lock* ./
COPY qlibs ./qlibs
RUN uv sync --no-install-project

# 源码在本地开发时通过挂载提供（compose.local.yml），云端部署时再 COPY
CMD ["python", "-c", "print('factopia app ready')"]
