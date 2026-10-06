# RD-Agent 主程序镜像（多架构）。
# - RD-Agent 执行因子代码时要求在 conda 环境中运行（CondaConf.conda_env_name 为必填）
# - Python 3.11：RD-Agent README 注明其 CI 在 3.10 / 3.11 上测试
# - 源码克隆到 /opt/rdagent-src 并以可编辑方式安装：RD-Agent 会把自身目录下的模板文件夹挂载给回测容器，
#   该目录通过命名卷与 dind 共享，保证所有路径都是容器内路径（见全局设计 7.4）
# Miniforge：只使用免费的 conda-forge 源，不涉及 Anaconda 默认源的服务条款与商业许可
FROM condaforge/miniforge3:latest

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN conda create -y -n rdagent python=3.11 && conda clean -afy
ENV CONDA_DEFAULT_ENV=rdagent \
    PATH=/opt/conda/envs/rdagent/bin:$PATH

ARG RDAGENT_VERSION=v1.0.0
RUN git clone --depth 1 --branch ${RDAGENT_VERSION} https://github.com/microsoft/RD-Agent.git /opt/rdagent-src \
    && pip install --no-cache-dir -e /opt/rdagent-src tables \
    && echo ${RDAGENT_VERSION} > /opt/rdagent-src/.factopia_version \
    && cp -a /opt/rdagent-src /opt/rdagent-src-image

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
