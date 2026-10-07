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
rm -f "$SRC/$EXP/factor_data_template/"daily_pv_*.h5

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
  # 时间段：数据截止 2023-12-31，2024 年以后对 AI 完全不可见（Spec MINE-03）。
  # 测试期结束日须早于数据最后一天：Qlib 回测在最后一步要读取下一个交易日
  for p in QLIB_FACTOR QLIB_MODEL QLIB_QUANT; do
    export ${p}_TRAIN_START=2015-01-01 ${p}_TRAIN_END=2020-12-31
    export ${p}_VALID_START=2021-01-01 ${p}_VALID_END=2021-12-31
    export ${p}_TEST_START=2022-01-01 ${p}_TEST_END=2023-12-28
  done
  mkdir -p "/workspace/runtime/rdagent/$FACTOPIA_MARKET"
  cd "/workspace/runtime/rdagent/$FACTOPIA_MARKET"
  echo "[entrypoint] market=$FACTOPIA_MARKET data=$HOME_DIR workdir=$(pwd)"
fi

exec "$@"
