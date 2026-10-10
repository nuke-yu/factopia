"""RD-Agent 场景子类：向 AI 提供场景信息（Spec MINE-07、第 2.3.1 节）。

在 RD-Agent 原有的通用背景之后追加：
- 市场介绍：configs/rdagent/context/<市场>.en.md（团队编写、人工审核）；
- 研究方向、投资假设与约束：研究场景文件（环境变量 FACTOPIA_RESEARCH，可选）；
- 预测周期说明；
- 已有发现：用回测测试期开始之前的数据，从 Alpha158 逐日 IC 重新计算的摘要（MINE-11，防止数据泄漏）。
内容由 factopia.research.context.build_context() 生成，后端的提示词预览调用同一个函数（RS-07）。
并把"实验设置"中写死的 CSI300 改为实际市场、时间段与预测周期。

通过环境变量 QLIB_FACTOR_SCEN 启用（由 prepare_run.py 设置），不修改 RD-Agent 源码。
"""
import os
import shutil
from pathlib import Path

import yaml
from rdagent.app.qlib_rd_loop.conf import FACTOR_PROP_SETTING
from rdagent.log import rdagent_logger as logger
from rdagent.components.coder.factor_coder.config import FACTOR_COSTEER_SETTINGS
from rdagent.scenarios.qlib.experiment import utils as qlib_exp_utils
from rdagent.scenarios.qlib.experiment.factor_experiment import QlibFactorScenario

from factopia.research.context import BENCHMARK_FILE, MARKETS, build_context


class FactopiaFactorScenario(QlibFactorScenario):
    def __init__(self) -> None:
        super().__init__()  # 首次运行时由 RD-Agent 生成数据目录（含模板目录中的 daily_benchmark.h5）
        self._install_benchmark()
        self.market = os.environ["FACTOPIA_MARKET"]
        self.horizon = int(os.environ.get("FACTOPIA_HORIZON", 20))
        research_path = os.environ.get("FACTOPIA_RESEARCH")
        self.research = yaml.safe_load(Path(research_path).read_text(encoding="utf-8")) if research_path else {}
        if self.research:  # 记录本次运行所属的研究规格与任务，供结果导出关联研究项目（factopia.export.rdagent_run）
            logger.log_object({"id": self.research.get("id"), "version": self.research.get("version"),
                               "path": research_path, "market": self.market, "horizon": self.horizon,
                               "job_id": os.environ.get("FACTOPIA_JOB_ID")},
                              tag="factopia.research")
        self._background = self._background + "\n\n" + self._context()
        self._experiment_setting = self._setting_table()

    def _install_benchmark(self) -> None:
        """把基准指数文件放入正式与调试数据目录，并刷新提供给 AI 的数据说明。"""
        src = Path(qlib_exp_utils.__file__).parent / "factor_data_template" / BENCHMARK_FILE
        for folder in (FACTOR_COSTEER_SETTINGS.data_folder, FACTOR_COSTEER_SETTINGS.data_folder_debug):
            target = Path(folder) / BENCHMARK_FILE
            if src.exists():
                shutil.copy(src, target)
            if not target.exists():
                raise FileNotFoundError(f"基准指数文件缺失：{target}（检查 generate.py 与 benchmark.txt）")
        self._source_data = qlib_exp_utils.get_data_folder_intro()

    def _context(self) -> str:
        return build_context(self.market, self.horizon, self.research, str(FACTOR_PROP_SETTING.test_start))

    def _setting_table(self) -> str:
        s = FACTOR_PROP_SETTING
        return (
            "| Dataset | Model | Factors | Horizon | Data Split |\n"
            "|---|---|---|---|---|\n"
            f"| {MARKETS[self.market]['name']} | LGBModel | Alpha158 Plus | {self.horizon} trading days | "
            f"Train: {s.train_start} to {s.train_end} <br> Valid: {s.valid_start} to {s.valid_end} <br> "
            f"Test: {s.test_start} to {s.test_end} |"
        )
