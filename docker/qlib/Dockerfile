# RD-Agent 回测镜像（替代官方只有 x86 版本的 pytorch/CUDA 镜像）。
# 多架构：Apple 芯片原生运行，x86 云服务器同样可用。不含 GPU / PyTorch（fin_factor 回测使用 LightGBM）。
FROM python:3.11-slim

# Qlib 源码地址。默认 GitHub；直连 GitHub 不稳定时可改用镜像（如 Gitee）：
#   docker compose --profile build-only build \
#     --build-arg QLIB_GIT_URL=https://gitee.com/mirrors/qlib.git qlib-runner
ARG QLIB_GIT_URL=https://github.com/microsoft/qlib.git

RUN apt-get update && apt-get install -y --no-install-recommends build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

# 走代理克隆 GitHub 仓库时 HTTP/2 多路复用流易被中断（curl 92），强制 HTTP/1.1 提高稳定性
RUN git config --global http.version HTTP/1.1

# 与 RD-Agent 官方镜像使用相同的 Qlib 提交（代理链路不稳定，失败自动重试最多 5 次）
RUN ok=0; for i in $(seq 1 12); do echo "clone attempt $i"; rm -rf /workspace/qlib; if git clone ${QLIB_GIT_URL} /workspace/qlib; then ok=1; break; fi; sleep 25; done; [ "$ok" = 1 ] \
    && cd /workspace/qlib && git reset --hard 2fb9380b342556ddb50a4b24e4fe8655d548b2b8
WORKDIR /workspace/qlib
RUN pip install --no-cache-dir --upgrade cython \
    && pip install --no-cache-dir -e . "mlflow<3.13" \
    && pip install --no-cache-dir catboost xgboost tables

# 把 Qlib 的 MLflow 指标写入改为同步，规避异步写入的竞态（见 sitecustomize.py）
COPY sitecustomize.py /usr/local/lib/python3.11/site-packages/sitecustomize.py
