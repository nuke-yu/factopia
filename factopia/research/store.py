"""研究规格版本与运行任务的存储（Spec RS-08、RS-09，第 16.9 节）。最小 POC 用文件，阶段 2 改存数据库。

    /workspace/runtime/research/<编号>/v<k>.yaml   锁定的版本：只新建、不修改
    /workspace/runtime/jobs/<任务编号>.json         运行任务：后端新建，此后只由后台运行器更新状态
    /workspace/runtime/jobs/<任务编号>.cancel       停止请求（后端写入，运行器读取）
    /workspace/runtime/jobs/<任务编号>.log          RD-Agent 的控制台输出
    /workspace/runtime/jobs/<任务编号>.live.json    运行中的实时状态（运行器每 3 秒更新，factopia/research/live.py）
    /workspace/runtime/jobs/.paused                排队暂停（大模型余额不足、密钥无效等，处理后由页面恢复）

可信度检查任务（kind = "validate"）也在同一队列中：target_run 为被检查的运行，loop 为轮次；run_id 始终为空。

任务状态：queued 排队 → running 运行中 → done 完成 / failed 失败 / cancelled 已停止 / budget_stopped 超出预算停止。
"""
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RESEARCH_DIR = RUNTIME / "research"
JOBS_DIR = RUNTIME / "jobs"
FINAL_STATES = ("done", "failed", "cancelled", "budget_stopped")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ── 研究规格版本 ─────────────────────────────────────────────

def version_path(research_id: str, version: int) -> Path:
    return RESEARCH_DIR / research_id / f"v{version}.yaml"


def versions(research_id: str) -> list[int]:
    return sorted(int(p.stem[1:]) for p in (RESEARCH_DIR / research_id).glob("v*.yaml") if p.stem[1:].isdigit())


def load_version(research_id: str, version: int) -> dict:
    return yaml.safe_load(version_path(research_id, version).read_text(encoding="utf-8"))


def save_version(spec: dict) -> dict:
    """写入下一个版本号；文件以独占方式创建，已存在的版本不会被覆盖。"""
    existing = versions(spec["id"])
    spec = {**spec, "version": (existing[-1] + 1) if existing else 1, "locked_at": now()}
    path = version_path(spec["id"], spec["version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=120)
    return spec


def research_ids() -> list[str]:
    return sorted(p.name for p in RESEARCH_DIR.glob("*") if p.is_dir() and versions(p.name))


# ── 运行任务 ─────────────────────────────────────────────────

def create_jobs(research_id: str, version: int, markets: list[str], horizon: int, loops: int, budget_usd: float) -> dict:
    """一个版本在多个市场运行：每个市场一个任务，属于同一批次，按市场顺序排队。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    batch = f"{stamp}_{secrets.token_hex(2)}"
    jobs = []
    for i, m in enumerate(markets):
        job = {"job_id": f"{batch}_{i}_{m}", "batch_id": batch, "research_id": research_id, "version": version,
               "market": m, "horizon": horizon, "loops": loops, "budget_usd": budget_usd,
               "state": "queued", "created_at": now(), "started_at": None, "ended_at": None,
               "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None}
        _atomic_write(JOBS_DIR / f"{job['job_id']}.json", json.dumps(job, ensure_ascii=False, indent=2))
        jobs.append(job)
    return {"batch_id": batch, "jobs": jobs}


def create_validate_job(run: dict, loop: int, placebo_n: int = 20) -> dict:
    """可信度检查任务（factopia/validate）：与研究任务共用一个队列，按顺序执行，不调用大模型。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    research = run.get("research") or {}
    job = {"job_id": f"{stamp}_{secrets.token_hex(2)}_0_{run['market']}", "batch_id": f"{stamp}_v", "kind": "validate",
           "research_id": research.get("id"), "version": research.get("version"), "market": run["market"],
           "horizon": run["horizon"], "loops": 0, "budget_usd": 0.0, "target_run": run["run_id"], "loop": loop,
           "placebo_n": placebo_n, "state": "queued", "created_at": now(), "started_at": None, "ended_at": None,
           "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None}
    write_job(job)
    return job


def write_job(job: dict) -> None:
    _atomic_write(JOBS_DIR / f"{job['job_id']}.json", json.dumps(job, ensure_ascii=False, indent=2))


def load_job(job_id: str) -> dict:
    return json.loads((JOBS_DIR / f"{job_id}.json").read_text(encoding="utf-8"))


def list_jobs() -> list[dict]:
    jobs = []
    for p in JOBS_DIR.glob("*.json"):
        if p.name.endswith(".live.json"):  # 实时状态文件，不是任务
            continue
        try:
            jobs.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):  # 正在替换的文件，下次再读
            continue
    return sorted(jobs, key=lambda j: j["job_id"])


def request_cancel(job_id: str) -> None:
    (JOBS_DIR / f"{job_id}.cancel").write_text(now(), encoding="utf-8")


def cancel_requested(job_id: str) -> bool:
    return (JOBS_DIR / f"{job_id}.cancel").exists()


def log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def write_live(job_id: str, state: dict) -> None:
    _atomic_write(JOBS_DIR / f"{job_id}.live.json", json.dumps(state, ensure_ascii=False))


def load_live(job_id: str) -> dict | None:
    path = JOBS_DIR / f"{job_id}.live.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ── 排队暂停：大模型账户或密钥出错时，后面的任务不再启动 ───────────────────

PAUSE_FILE = JOBS_DIR / ".paused"


def pause(reason: str) -> None:
    _atomic_write(PAUSE_FILE, json.dumps({"reason": reason, "at": now()}, ensure_ascii=False))


def paused() -> dict | None:
    return json.loads(PAUSE_FILE.read_text(encoding="utf-8")) if PAUSE_FILE.exists() else None


def resume() -> None:
    PAUSE_FILE.unlink(missing_ok=True)


def retry_job(job: dict) -> dict:
    """失败或停止的任务：用同一版本、市场与参数重新排队（新的批次）。"""
    if job.get("kind") == "validate":
        run = json.loads((RUNTIME / "results" / "runs" / job["target_run"] / "run.json").read_text(encoding="utf-8"))
        new = create_validate_job(run, job["loop"], job.get("placebo_n", 20))
        return {"batch_id": new["batch_id"], "jobs": [new]}
    return create_jobs(job["research_id"], job["version"], [job["market"]], job["horizon"], job["loops"], job["budget_usd"])
