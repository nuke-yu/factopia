"""按市场与预测周期准备一次 RD-Agent 运行（Spec MINE-09、第 3.6 节）。由 rdagent 容器的启动脚本调用。

两种模式，均向标准输出打印 shell `export` 语句，由启动脚本 eval：

- `env`：读取研究场景文件（环境变量 FACTOPIA_RESEARCH），输出 FACTOPIA_MARKET、FACTOPIA_HORIZON；
  锁定的版本包含多个市场（markets），由调用方用 FACTOPIA_MARKET 指定本次运行的市场（后台运行器）；
- `apply`：在已覆盖为该市场的 RD-Agent 模板上，
    1. 把预测目标改为未来 N 个交易日收益；
    2. 设置最短持有天数 hold_thresh = N（按周期调仓）；
    3. 按该市场交易日历计算训练 / 验证 / 测试时间段，段与段之间间隔 N 个交易日；
  并输出时间段、工作目录（按市场和周期分开）以及场景子类的环境变量。
"""
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

import yaml

from factopia.research.periods import check_horizon, split_dates

SRC = Path(os.environ.get("RDAGENT_SRC", "/opt/rdagent-src"))
EXP = SRC / "rdagent/scenarios/qlib/experiment"
RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))

SCENARIO_CLASS = "factopia.mining.rdagent_ext.scenario.FactopiaFactorScenario"


def _export(**kv: object) -> None:
    for k, v in kv.items():
        print(f"export {k}={shlex.quote(str(v))}")


def _horizon(value: object) -> int:
    try:
        return check_horizon(value)
    except ValueError as e:
        raise SystemExit(str(e))


def mode_env() -> None:
    path = os.environ.get("FACTOPIA_RESEARCH")
    if not path:
        return
    research = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    markets = research.get("markets") or [research["market"]]
    market = os.environ.get("FACTOPIA_MARKET") or markets[0]
    if market not in markets:
        raise SystemExit(f"研究规格 {research.get('id')} 不包含市场 {market}（{markets}）")
    _export(FACTOPIA_MARKET=market, FACTOPIA_HORIZON=_horizon(research.get("horizon", 20)))


def _rewrite_templates(n: int) -> int:
    label = f"Ref($close, -{n + 1})/Ref($close, -1) - 1"
    changed = 0
    for f in sorted(EXP.glob("*_template/*.yaml")):
        s = f.read_text()
        s2 = re.sub(r'\["Ref\(\$close, -\d+\)/Ref\(\$close, -1\) - 1"\]', f'["{label}"]', s)
        # 最短持有天数：已有则替换，否则插在 n_drop 之后（同一缩进）
        if re.search(r"^\s*hold_thresh:\s*\d+", s2, flags=re.M):
            s2 = re.sub(r"(^\s*hold_thresh:\s*)\d+", rf"\g<1>{n}", s2, flags=re.M)
        else:
            s2 = re.sub(r"^(\s*)(n_drop:\s*\d+)$", rf"\1\2\n\1hold_thresh: {n}", s2, flags=re.M)
        if s2 != s:
            f.write_text(s2)
            changed += 1
    return changed


def mode_apply() -> None:
    market = os.environ["FACTOPIA_MARKET"]
    n = _horizon(os.environ.get("FACTOPIA_HORIZON", 20))
    changed = _rewrite_templates(n)
    try:
        dates = split_dates(market, n)
    except ValueError as e:
        raise SystemExit(str(e))
    workdir = RUNTIME / "rdagent" / market / f"h{n}"
    # 删除上次运行生成的数据目录，由 RD-Agent 按最新的数据生成脚本重新生成（含基准指数文件）
    for name in ("factor_implementation_source_data", "factor_implementation_source_data_debug"):
        shutil.rmtree(workdir / "git_ignore_folder" / name, ignore_errors=True)
    exports = {f"{p}_{k}": v for p in ("QLIB_FACTOR", "QLIB_MODEL", "QLIB_QUANT") for k, v in dates.items()}
    _export(**exports, FACTOPIA_HORIZON=n, QLIB_FACTOR_SCEN=SCENARIO_CLASS,
            FACTOPIA_WORKDIR=workdir)
    print(f"echo '[prepare_run] market={market} horizon={n} templates={changed} "
          f"train={dates['TRAIN_START']}~{dates['TRAIN_END']} valid={dates['VALID_START']}~{dates['VALID_END']} "
          f"test={dates['TEST_START']}~{dates['TEST_END']}'")


if __name__ == "__main__":
    {"env": mode_env, "apply": mode_apply}[sys.argv[1]]()
