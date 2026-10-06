# RD-Agent 回测镜像（替代官方只有 x86 版本的 pytorch/CUDA 镜像）。
# 多架构：Apple 芯片原生运行，x86 云服务器同样可用。不含 GPU / PyTorch（fin_factor 回测使用 LightGBM）。
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends build-essential git curl \
    && rm -rf /var/lib/apt/lists/*

# 与 RD-Agent 官方镜像使用相同的 Qlib 提交
RUN git clone https://github.com/microsoft/qlib.git /workspace/qlib \
    && cd /workspace/qlib && git reset --hard 2fb9380b342556ddb50a4b24e4fe8655d548b2b8
WORKDIR /workspace/qlib
RUN pip install --no-cache-dir --upgrade cython \
    && pip install --no-cache-dir -e . "mlflow<3.13" \
    && pip install --no-cache-dir catboost xgboost tables

# 把 Qlib 的 MLflow 指标写入改为同步，规避异步写入的竞态（见 sitecustomize.py）
COPY sitecustomize.py /usr/local/lib/python3.11/site-packages/sitecustomize.py
