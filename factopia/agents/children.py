"""子任务状态检查（runner supervisor 与 Agent loop 共用）。

只依赖 factopia.research.store 与文件系统，不 import RD-Agent / pandas，
因此 app 容器也能跑单元测试。
"""
from pathlib import Path

from factopia.research import store as research_store

RESULTS = research_store.RUNTIME / "results"


def load(job_id: str) -> dict | None:
    try:
        return research_store.load_job(job_id)
    except (FileNotFoundError, OSError, ValueError):
        return None


def jobs(agent_or_job: dict) -> list[dict]:
    ids = agent_or_job.get("child_jobs") or []
    return [j for j in (load(i) for i in ids) if j]


def all_final(child_jobs: list[dict]) -> bool:
    return bool(child_jobs) and all(j.get("state") in research_store.FINAL_STATES for j in child_jobs)


def run_ids(child_jobs: list[dict]) -> list[str]:
    return [j["run_id"] for j in child_jobs if j.get("run_id")]


def export_pending(child_jobs: list[dict]) -> list[str]:
    """exporter 还没处理完的 run（.backtest_pending 仍存在）。"""
    return [rid for rid in run_ids(child_jobs)
            if (RESULTS / "runs" / rid / ".backtest_pending").exists()]


def ready(agent_or_job: dict) -> bool:
    """所有子任务结束且导出完成；没有子任务时返回 False。"""
    js = jobs(agent_or_job)
    return all_final(js) and not export_pending(js)
