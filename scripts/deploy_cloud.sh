#!/bin/sh
# 阿里云 ECS 一键部署（在服务器仓库根目录执行：bash scripts/deploy_cloud.sh）
#
# 前提：
#   1) 仓库代码在 /opt/factopia；.env 已按 .env.cloud 填好（含 LLM 密钥）
#   2) docker/htpasswd 已生成（printf 'user:%s\n' "$(openssl passwd -apr1 '密码')" > docker/htpasswd）
#   3) Docker 已配 registry-mirrors（Docker Hub 在国内 ECS 上不通）
#
# 幂等：可以重复执行；已构建的镜像层会复用缓存。
set -e
cd "$(dirname "$0")/.."

DC="docker compose -f docker-compose.yml -f compose.cloud.yml --profile platform --profile research"

echo "== 1/6 修正数据目录属主"
# api 镜像以 uid 10001 运行；runtime 是从本地打包上传的，属主是打包机的 uid（Windows 上是 197609）。
# Linux 上 ext4 会真的按 uid 判权限，不修就是“研究存储目录尚未初始化：请先启动后台运行器”。
# 默认 ACL 让 runner（root）之后新建的文件 api 也能改。
chown -R 10001:10001 runtime
find runtime -type d -exec chmod 2775 {} +
if command -v setfacl >/dev/null 2>&1; then
  find runtime -type d -exec setfacl -d -m u:10001:rwx,u::rwx,g::rwx,o::rx {} +
else
  echo "   未装 acl（apt-get install -y acl），跳过默认 ACL：runner 新建的文件 api 可能改不了"
fi

echo "== 2/6 构建镜像（api / web / app / qlib-runner / rdagent）"
$DC build api web app
docker compose -f docker-compose.yml -f compose.cloud.yml --profile build-only build qlib-runner
$DC build rdagent

echo "== 3/6 启动 dind 并载入回测镜像"
$DC up -d dind
for i in $(seq 1 30); do
  docker compose -f docker-compose.yml -f compose.cloud.yml exec -T dind docker info >/dev/null 2>&1 && break
  sleep 2
done
docker save factopia-qlib:dev | $DC exec -T dind docker load

echo "== 4/6 RD-Agent 各市场 qlib 数据集（已存在则跳过，各市场约 1-3 分钟）"
if [ ! -d runtime/rdagent_markets/hk/qlib_data/cn_data ]; then
  $DC run --rm app python -m factopia.data.to_qlib --rdagent
else
  echo "已存在，跳过"
fi

echo "== 5/6 文本信号数据集（香港/英国为模拟数据，术语表口径不调用大模型）"
if [ ! -f runtime/text/demo_hk/summary.json ]; then
  $DC run --rm app python -m factopia.text demo
  $DC run --rm app python -m factopia.text all --lexicon
else
  echo "已存在，跳过（想换成大模型标注：python -m factopia.text all --yes --budget 3，每个数据集约 \$2.3）"
fi

echo "== 6/6 启动平台与研究服务"
$DC up -d

echo
echo "== 状态"
$DC ps
echo
echo "访问：http://$(curl -s -m 3 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || echo '<公网IP>'):${FACTOPIA_WEB_PORT:-80}/  （Basic Auth 见 docker/htpasswd）"
