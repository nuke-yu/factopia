# Qlib 回测镜像（替代官方只有 x86 版本的 pytorch/CUDA 镜像）。
# 多架构：Apple 芯片原生运行，x86 云服务器同样可用。不含 GPU / PyTorch（fin_factor 回测使用 LightGBM）。
# 源码来自仓库内 qlibs/（Qlib commit 2fb9380 快照，与 RD-Agent 官方镜像固定版本一致），不再从 GitHub clone。

# pip 源可通过 --build-arg PIP_INDEX_URL=... 覆盖（默认官方 PyPI；国内网络可传镜像加速）
ARG PIP_INDEX_URL=https://pypi.org/simple

FROM python:3.11-slim

ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# APT_MIRROR：可选的镜像（如 https://mirrors.aliyun.com），国内直连 deb.debian.org 不通时由构建参数传入
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

COPY qlibs /workspace/qlib
WORKDIR /workspace/qlib
RUN pip install --no-cache-dir --upgrade cython \
    && pip install --no-cache-dir -e . "mlflow<3.13" \
    && pip install --no-cache-dir catboost xgboost tables

# 把 Qlib 的 MLflow 指标写入改为同步，规避异步写入的竞态（见 sitecustomize.py）
COPY docker/qlib/sitecustomize.py /usr/local/lib/python3.11/site-packages/sitecustomize.py
