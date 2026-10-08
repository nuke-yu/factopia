#!/bin/sh
# RD-Agent 容器启动脚本。
# 1) /opt/rdagent-src 是与 dind 共享的命名卷：卷为空或版本与镜像不一致时，用镜像内源码同步。
# 2) 每次启动先恢复 RD-Agent 原版配置模板；若设置了 FACTOPIA_MARKET（hk / uk / fr），
#    再用 /workspace/configs/rdagent/<市场>/ 下的模板覆盖，并切换数据、时间段和工作目录。
set -e
SRC=/opt/rdagent-src
IMG=/opt/rdagent-src-image
EXP=rdagent/scenarios/qlib/experiment

if ! cmp -s "$IMG/.factopia_version" "$SRC/.factopia_version" 2>/dev/null; then
  echo "[entrypoint] syncing RD-Agent source into shared volume"
  rm -rf "$SRC"/* "$SRC"/.[!.]* 2>/dev/null || true
  cp -a "$IMG/." "$SRC/"
fi

for t in factor_template model_template; do
  cp "$IMG/$EXP/$t/"*.yaml "$SRC/$EXP/$t/"
done
# 数据生成脚本也先恢复原版；并删除上次运行残留的数据文件，避免生成失败时误用旧数据
cp "$IMG/$EXP/factor_data_template/generate.py" "$SRC/$EXP/factor_data_template/generate.py"
rm -f "$SRC/$EXP/factor_data_template/"daily_*.h5

# 研究场景文件（可选）：决定市场与预测周期（Spec MINE-07、MINE-09）
if [ -n "$FACTOPIA_RESEARCH" ]; then
  eval "$(python -m factopia.mining.rdagent_ext.prepare_run env)"
fi

if [ -n "$FACTOPIA_MARKET" ]; then
  CONF=/workspace/configs/rdagent/$FACTOPIA_MARKET
  HOME_DIR=/workspace/runtime/rdagent_markets/$FACTOPIA_MARKET
  [ -d "$CONF" ] || { echo "[entrypoint] 缺少市场模板目录 $CONF"; exit 1; }
  [ -d "$HOME_DIR/qlib_data/cn_data" ] || { echo "[entrypoint] 缺少市场数据 $HOME_DIR（先运行 python -m factopia.data.to_qlib --rdagent）"; exit 1; }
  for t in factor_template model_template; do
    cp "$CONF/$t/"*.yaml "$SRC/$EXP/$t/"
  done
  cp /workspace/configs/rdagent/common/generate.py "$SRC/$EXP/factor_data_template/generate.py"
  # 回测容器挂载该市场的数据（RD-Agent 在容器内固定读取 /root/.qlib/qlib_data/cn_data）
  export QLIB_DOCKER_EXTRA_VOLUMES="{\"$HOME_DIR\": {\"bind\": \"/root/.qlib/\", \"mode\": \"rw\"}}"
  # 预测周期（默认 20 个交易日）：改写标签与最短持有天数，按交易日历计算时间段（段间间隔 N 个交易日），
  # 启用场景子类，并按市场和周期使用独立工作目录。数据截止 2023-12-31，2024 年以后对 AI 不可见（MINE-03）
  export FACTOPIA_HORIZON="${FACTOPIA_HORIZON:-20}"
  eval "$(python -m factopia.mining.rdagent_ext.prepare_run apply)"
  mkdir -p "$FACTOPIA_WORKDIR"
  cd "$FACTOPIA_WORKDIR"
  echo "[entrypoint] market=$FACTOPIA_MARKET horizon=$FACTOPIA_HORIZON data=$HOME_DIR workdir=$(pwd)"
fi

exec "$@"
