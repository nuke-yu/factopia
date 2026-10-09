# Factopia 前端：独立部署的镜像。构建阶段编译静态页面，运行阶段由 nginx 提供页面并转发 /api。
# 构建上下文为仓库根目录：docker compose build web
# 字体与图表库全部打包进镜像，运行时不访问任何境外 CDN（第 14.4 节 #17 的教训）。
FROM node:22-slim AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginx:1.27-alpine
# nginx 官方镜像启动时用环境变量渲染 templates 目录：API_UPSTREAM 为后端地址
ENV API_UPSTREAM=http://api:8000
COPY docker/web/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
