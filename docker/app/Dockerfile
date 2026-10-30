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

# 走代理克隆 GitHub 仓库时 HTTP/2 多路复用流易被中断（curl 92），强制 HTTP/1.1 提高稳定性
RUN git config --global http.version HTTP/1.1

WORKDIR /workspace

# 先只复制依赖声明，依赖不变时可复用缓存层
COPY pyproject.toml uv.lock* ./
# 代理链路不稳定时 uv sync（含 Git 依赖克隆）失败自动重试（12 次 × 25 秒，可覆盖约 10 分钟网络坏窗口）
RUN ok=0; for i in $(seq 1 12); do if uv sync --no-install-project; then ok=1; break; fi; echo "uv sync attempt $i failed, retrying"; sleep 25; done; [ "$ok" = 1 ]

# 源码在本地开发时通过挂载提供（compose.local.yml），云端部署时再 COPY
CMD ["python", "-c", "print('factopia app ready')"]
