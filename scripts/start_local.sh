#!/usr/bin/env bash
# Factopia 本地一键启动（Windows Git Bash 下运行）。
# 前置条件：Docker Desktop 已打开（桌面手动启动，命令行方式可能被拦截）。
#
# 用法：
#   bash scripts/start_local.sh          # 启动全部（隧道 + app/api/runner/exporter/dind + web）
#   bash scripts/start_local.sh check    # 只检查当前状态，不启动
set -euo pipefail
cd "$(dirname "$0")/.."

# ── 环境变量（含 RDS/ECS 口令、存储模式）────────────────────────────────
set -a; source .workbuddy/env.sh; set +a

COMPOSE="docker compose -f docker-compose.yml -f compose.local.yml"

# 宿主脚本优先用项目虚拟环境（sshtunnel / pandas / psycopg 都装在这里，
# 系统 python 没有），没有 venv 时回落到系统 python。
PY=".venv/Scripts/python.exe"; [ -x "$PY" ] || PY=".venv/bin/python"; [ -x "$PY" ] || PY="python"

# ── 1. 数据库隧道：本机 15432 → ECS → RDS 内网 ─────────────────────────
if ! (exec 3<>/dev/tcp/127.0.0.1/15432) 2>/dev/null; then
  echo "→ 启动 SSH 隧道 (127.0.0.1:15432 → ECS → RDS)..."
  "$PY" scripts/db/pg_tunnel.py keepalive \
    --local-port 15432 --rds-host "$FACTOPIA_RDS_HOST" &
  sleep 3
fi
(exec 3<>/dev/tcp/127.0.0.1/15432) 2>/dev/null \
  && echo "✓ 隧道 15432 在线" || { echo "✗ 隧道 15432 未通，检查 ECS 口令/网络"; exit 1; }

# ── 2. 容器栈 ──────────────────────────────────────────────────────────
# 注意：buildx 0.37.2 在 Windows 上的 fs 权限检查 bug，需设 BUILDX_BAKE_ENTITLEMENTS_FS=0
if [ "${1:-up}" = "check" ]; then
  $COMPOSE ps
  exit 0
fi
echo "→ 启动容器栈 (app/api/runner/exporter/dind/web)..."
BUILDX_BAKE_ENTITLEMENTS_FS=0 $COMPOSE --profile platform up -d 2>&1 | grep -v "^ "

# ── 3. 健康检查 ────────────────────────────────────────────────────────
echo "→ 等待 api/web 就绪..."
for i in $(seq 1 30); do
  st=$($COMPOSE ps --format json 2>/dev/null | grep -c '"Health":"healthy"' || true)
  [ "$st" -ge 2 ] && break
  sleep 3
done
$COMPOSE ps

echo
echo "✓ 启动完成："
echo "  页面      http://127.0.0.1:18080/          (web → nginx → api)"
echo "  健康总览  curl http://127.0.0.1:18080/api/health"
echo "  数据库    curl http://127.0.0.1:18080/api/health/db"
echo "  改前端后：cd web && npm run build && docker restart factopia-web-1"
