#!/bin/sh
# /opt/rdagent-src 挂载的是与 dind 共享的命名卷。卷为空或版本与镜像不一致时，用镜像内的源码同步过去。
set -e
if ! cmp -s /opt/rdagent-src-image/.factopia_version /opt/rdagent-src/.factopia_version 2>/dev/null; then
  echo "[entrypoint] syncing RD-Agent source into shared volume"
  rm -rf /opt/rdagent-src/* /opt/rdagent-src/.[!.]* 2>/dev/null || true
  cp -a /opt/rdagent-src-image/. /opt/rdagent-src/
fi
exec "$@"
