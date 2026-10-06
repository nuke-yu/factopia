"""Qlib 回测镜像的启动钩子：把 MLflowRecorder 的指标写入改为同步。

原因：Qlib（含 RD-Agent 固定的提交 2fb9380）在 MLflowRecorder.start_run 中创建 AsyncCaller，
log_metrics / log_params 在后台线程写文件；同一进程随后读取运行信息时，可能读到尚未写完的指标文件，
报错 "Metric 'Rank ICIR' is malformed. No data found." 或 "Failed to download artifacts"。
Qlib 的 AsyncCaller.async_dec 在 async_log 为 None 时同步执行，因此这里在 start_run 之后
等待已排队的写入完成，并把 async_log 置为 None。不修改 Qlib 源码文件。
"""
import importlib.abc
import importlib.util
import sys

_TARGET = "qlib.workflow.recorder"


def _patch(module) -> None:
    original = module.MLflowRecorder.start_run

    def start_run(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.async_log is not None:
            self.async_log.wait()   # 写完已排队的 log_params，并停止后台线程
        self.async_log = None
        return result

    module.MLflowRecorder.start_run = start_run


class _PostImportHook(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != _TARGET:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            _patch(module)

        spec.loader.exec_module = exec_and_patch
        return spec


sys.meta_path.insert(0, _PostImportHook())
