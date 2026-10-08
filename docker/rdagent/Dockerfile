# RD-Agent 主程序镜像（多架构）。
# - RD-Agent 执行因子代码时要求在 conda 环境中运行（CondaConf.conda_env_name 为必填）
# - Python 3.11：RD-Agent README 注明其 CI 在 3.10 / 3.11 上测试
# - 源码克隆到 /opt/rdagent-src 并以可编辑方式安装：RD-Agent 会把自身目录下的模板文件夹挂载给回测容器，
#   该目录通过命名卷与 dind 共享，保证所有路径都是容器内路径（见全局设计 7.4）
ARG RDAGENT_VERSION=v1.0.0

# ---- 构建阶段：编译 RD-Agent 自带的 Web UI（Vue + Vite，需要 Node 22）----
# 由 `rdagent server_ui` 提供服务，含 Playground：选择场景、上传资料、启动与控制运行、实时查看过程
FROM node:22-slim AS web
ARG RDAGENT_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${RDAGENT_VERSION} https://github.com/microsoft/RD-Agent.git /src
WORKDIR /src/web
# 上游 v1.0.0 的 package.json 中 vite@8 与 @vitejs/plugin-vue@5（声明支持 vite 5/6）版本声明冲突，npm 严格校验会拒绝安装
RUN npm ci --legacy-peer-deps && npm run build:flask   # 输出到 /src/git_ignore_folder/static
# 上游 index.html 在 <head> 中同步加载 3 个公共 CDN 文件（snap.svg、KaTeX 样式、highlight.js 样式）；
# 网络较慢时页面会一直空白。构建时从 npm 下载同版本文件放到本地，并改为本地引用（也满足离线演示要求）
RUN mkdir -p /tmp/vendor && cd /tmp/vendor && npm init -y >/dev/null \
    && npm install --no-save snapsvg@0.5.1 katex@0.16.10 highlight.js@11.11.1 \
    && V=/src/git_ignore_folder/static/vendor && mkdir -p $V/katex \
    && cp node_modules/snapsvg/dist/snap.svg-min.js $V/ \
    && cp -r node_modules/katex/dist/katex.min.css node_modules/katex/dist/fonts $V/katex/ \
    && cp node_modules/highlight.js/styles/vs.min.css $V/ \
    && sed -i -e 's#https://cdnjs.cloudflare.com/ajax/libs/snap.svg/0.5.1/snap.svg-min.js#./vendor/snap.svg-min.js#' \
              -e 's#https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css#./vendor/katex/katex.min.css#' \
              -e 's#https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/vs.min.css#./vendor/vs.min.css#' \
              /src/git_ignore_folder/static/index.html \
    && ! grep -qE 'cdnjs|jsdelivr' /src/git_ignore_folder/static/index.html

# ---- 运行阶段 ----
# Miniforge：只使用免费的 conda-forge 源，不涉及 Anaconda 默认源的服务条款与商业许可
FROM condaforge/miniforge3:latest

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN conda create -y -n rdagent python=3.11 && conda clean -afy
ENV CONDA_DEFAULT_ENV=rdagent \
    PATH=/opt/conda/envs/rdagent/bin:$PATH

ARG RDAGENT_VERSION
RUN git clone --depth 1 --branch ${RDAGENT_VERSION} https://github.com/microsoft/RD-Agent.git /opt/rdagent-src \
    && pip install --no-cache-dir -e /opt/rdagent-src tables \
    && echo ${RDAGENT_VERSION} > /opt/rdagent-src/.factopia_version \
    && cp -a /opt/rdagent-src /opt/rdagent-src-image

# Web UI 静态文件（由 UI_STATIC_PATH 指向）
COPY --from=web /src/git_ignore_folder/static /opt/rdagent-web

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
