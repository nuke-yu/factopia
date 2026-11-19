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
# APT_MIRROR：可选的镜像（如 https://mirrors.aliyun.com）。国内直连 deb.debian.org 不通时
# 由 compose.local.yml 传入；按基础镜像发行版自动选择路径，留空则保持官方源，云端构建行为不变。
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then . /etc/os-release; \
      case "$ID" in \
        ubuntu) repo="$APT_MIRROR/ubuntu"; comps="main universe";; \
        *)      repo="$APT_MIRROR/debian"; comps="main";; \
      esac; \
      printf 'deb %s %s %s\n' "$repo" "$VERSION_CODENAME" "$comps" > /etc/apt/sources.list; \
      rm -f /etc/apt/sources.list.d/*.sources; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# 先复制依赖声明与本地 qlibs/ 源码（pyqlib 为路径依赖），依赖不变时可复用缓存层
COPY pyproject.toml uv.lock* ./
COPY qlibs ./qlibs
RUN uv sync --no-install-project

# uv 的 editable 安装不会把 Cython 扩展编译进源码树，而源码树会遮蔽 site-packages，
# 导致反序列化回测持仓时 `No module named 'qlib.data._libs.rolling'` —— 必须就地编译
RUN uv pip install --quiet cython && cd qlibs && python setup.py build_ext --inplace

# 源码在本地开发时通过挂载提供（compose.local.yml），云端部署时再 COPY
CMD ["python", "-c", "print('factopia app ready')"]
