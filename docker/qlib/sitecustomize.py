"""Qlib 回测镜像的启动钩子（不修改 Qlib 源码文件，在模块导入后打补丁）。

1. MLflowRecorder 的指标写入改为同步（qlib.workflow.recorder）。
   Qlib（含 RD-Agent 固定的提交 2fb9380）在 MLflowRecorder.start_run 中创建 AsyncCaller，
   log_metrics / log_params 在后台线程写文件；同一进程随后读取运行信息时，可能读到尚未写完的指标文件，
   报错 "Metric 'Rank ICIR' is malformed. No data found." 或 "Failed to download artifacts"。
   Qlib 的 AsyncCaller.async_dec 在 async_log 为 None 时同步执行，因此这里在 start_run 之后
   等待已排队的写入完成，并把 async_log 置为 None。

2. TopkDropoutStrategy 中打分相同的股票按确定的顺序选（qlib.contrib.strategy.signal_strategy；Spec 第 8 章 #21、BR-37）。
   模型打分大量并列时，Qlib 在并列股票中买哪几只取决于每个进程随机的顺序，同一模型的回测收益可相差十几个百分点，
   回测不可复现。这里在策略每天读取打分时，对并列的股票加上一个小于最小打分间隔一半的偏移，使打分严格有序：
   - 默认：按股票代码排序（结果可复现）；
   - 环境变量 FACTOPIA_TIE_SEED=<整数>：按该种子对股票代码做固定的随机排列，用于衡量结果对并列顺序的敏感程度
     （可信度检查，factopia/validate）。
   不并列的股票之间的先后不受影响；模型打分文件（pred.pkl）、IC 与分组收益不变。
"""
import importlib.abc
import importlib.util
import sys


def _patch_recorder(module) -> None:
    original = module.MLflowRecorder.start_run

    def start_run(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.async_log is not None:
            self.async_log.wait()   # 写完已排队的 log_params，并停止后台线程
        self.async_log = None
        return result

    module.MLflowRecorder.start_run = start_run


def strict_order(score, seed: str | None = None):
    """给并列的打分加上确定的微小偏移，使同一天的打分严格有序。"""
    import hashlib

    import numpy as np
    import pandas as pd

    if score is None:
        return score
    frame = isinstance(score, pd.DataFrame)
    s = (score.iloc[:, 0] if frame else score).astype(float)
    vals = np.unique(s.dropna().to_numpy())
    gaps = np.diff(vals)
    gap = float(gaps[gaps > 0].min()) if (gaps > 0).any() else 1.0
    codes = s.index.get_level_values("instrument") if isinstance(s.index, pd.MultiIndex) else s.index
    codes = [str(c) for c in codes]
    if seed:
        keys = [int(hashlib.md5(f"{seed}:{c}".encode()).hexdigest()[:12], 16) for c in codes]
    else:
        keys = codes
    rank = pd.Series(keys).rank(method="first").to_numpy() - 1      # 0 = 并列时最优先
    out = s - rank / max(len(rank), 1) * gap * 0.5
    if frame:
        score = score.copy()
        score.iloc[:, 0] = out.to_numpy()
        return score
    return out


def _patch_strategy(module) -> None:
    import os

    cls = module.TopkDropoutStrategy
    original = cls.generate_trade_decision
    seed = os.environ.get("FACTOPIA_TIE_SEED") or None

    def generate_trade_decision(self, execute_result=None):
        signal = self.signal
        get = signal.get_signal
        signal.get_signal = lambda *a, **k: strict_order(get(*a, **k), seed)
        try:
            return original(self, execute_result)
        finally:
            del signal.get_signal                 # 恢复类上的方法

    cls.generate_trade_decision = generate_trade_decision


_PATCHES = {"qlib.workflow.recorder": _patch_recorder, "qlib.contrib.strategy.signal_strategy": _patch_strategy}


class _PostImportHook(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        patch = _PATCHES.get(name)
        if patch is None:
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        _PATCHES.pop(name)
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            patch(module)

        spec.loader.exec_module = exec_and_patch
        return spec


sys.meta_path.insert(0, _PostImportHook())
