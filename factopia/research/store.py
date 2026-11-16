"""研究规格版本与运行任务的存储（Spec RS-08、RS-09，第 16.9 节）。最小 POC 用文件，阶段 2 改存数据库。

    /workspace/runtime/research/<编号>/v<k>.yaml   锁定的版本：只新建、不修改
    /workspace/runtime/jobs/<任务编号>.json         运行任务：后端新建，此后只由后台运行器更新状态
    /workspace/runtime/jobs/<任务编号>.cancel       停止请求（后端写入，运行器读取）
    /workspace/runtime/jobs/<任务编号>.log          RD-Agent 的控制台输出
    /workspace/runtime/jobs/<任务编号>.live.json    运行中的实时状态（运行器每 3 秒更新，factopia/research/live.py）
    /workspace/runtime/jobs/.paused                排队暂停（大模型余额不足、密钥无效等，处理后由页面恢复）

可信度检查任务（kind = "validate"）也在同一队列中：target_run 为被检查的运行，loop 为轮次；run_id 始终为空。
信号监控任务（kind = "monitor"）同样排在这个队列中：target_signal 为信号目录中的市场版本。

任务状态：queued 排队 → running 运行中 → done 完成 / failed 失败 / cancelled 已停止 / budget_stopped 超出预算停止。
"""
import json
import math
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml

from factopia import db
from factopia.repo import jobs as _repo_jobs
from factopia.repo import research as _repo_research

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


def _json_safe(obj):
    """递归把 NaN/Inf 换成 None：RD-Agent 失败时 cost 汇总会产生 NaN，
    落盘为 NaN 字面量后 API 响应序列化会 500（Out of range float values are not JSON compliant）。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _dumps(obj, indent: int | None = None) -> str:
    return json.dumps(_json_safe(obj), ensure_ascii=False, indent=indent, allow_nan=False)


# ── 研究规格版本 ─────────────────────────────────────────────

def version_path(research_id: str, version: int) -> Path:
    return RESEARCH_DIR / research_id / f"v{version}.yaml"


def _file_versions(research_id: str) -> list[int]:
    return sorted(int(p.stem[1:]) for p in (RESEARCH_DIR / research_id).glob("v*.yaml") if p.stem[1:].isdigit())


def _file_load_version(research_id: str, version: int) -> dict:
    return yaml.safe_load(version_path(research_id, version).read_text(encoding="utf-8"))


def _file_save_version(spec: dict) -> dict:
    """写入下一个版本号；文件以独占方式创建，已存在的版本不会被覆盖。"""
    existing = versions(spec["id"])
    spec = {**spec, "version": (existing[-1] + 1) if existing else 1, "locked_at": now()}
    path = version_path(spec["id"], spec["version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=120)
    return spec


def _file_research_ids() -> list[str]:
    return sorted(p.name for p in RESEARCH_DIR.glob("*") if p.is_dir() and versions(p.name))


# ── 人工终止研究（结果页 / 研究 Agent 页）────────────────────
# 按研究编号（而不是单个版本）终止：终止后该研究的任何版本都不能再启动 Agent。
# 版本字段只记录终止操作发生在哪个版本，便于追溯；已终止的研究不会自动解除。

def termination_path(research_id: str) -> Path:
    return RESEARCH_DIR / research_id / "terminated.json"


def _file_termination(research_id: str | None) -> dict | None:
    if not research_id:
        return None
    path = termination_path(research_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _file_terminate(research_id: str, version: int | None = None, reason: str = "人工终止当前研究",
              by: str = "研究员（演示，未登录）") -> dict:
    """写入终止标记；重复调用返回第一次的记录（幂等）。"""
    existing = _file_termination(research_id)
    if existing:
        return existing
    rec = {"research_id": research_id, "version": version, "at": now(), "by": by, "reason": str(reason)[:1000]}
    _atomic_write(termination_path(research_id), _dumps(rec, indent=2))
    return rec


# ── 运行任务 ─────────────────────────────────────────────────

def create_jobs(research_id: str, version: int, markets: list[str], horizon: int, loops: int, budget_usd: float,
                kind: str | None = None) -> dict:
    """一个版本在多个市场运行：每个市场一个任务，属于同一批次，按市场顺序排队。
    kind = "text"：文本信号研究（factopia/text/research.py），不启动 RD-Agent、不调用大模型。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    batch = f"{stamp}_{secrets.token_hex(2)}"
    jobs = []
    for i, m in enumerate(markets):
        job = {"job_id": f"{batch}_{i}_{m}", "batch_id": batch, "research_id": research_id, "version": version,
               "market": m, "horizon": horizon, "loops": loops, "budget_usd": budget_usd,
               "state": "queued", "created_at": now(), "started_at": None, "ended_at": None,
               "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None}
        if kind:
            job |= {"kind": kind, "loops": 1, "budget_usd": 0.0}
        _atomic_write(JOBS_DIR / f"{job['job_id']}.json", _dumps(job, indent=2))
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


def create_monitor_job(signal_id: str, market: str) -> dict:
    """信号监控任务（factopia/signals/monitor.py）：用冻结的因子代码重算上线期信号并计算监控指标，不调用大模型。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rid, ver = signal_id.rsplit(".", 2)[0], int(signal_id.rsplit(".", 2)[1][1:])
    job = {"job_id": f"{stamp}_{secrets.token_hex(2)}_0_{market}", "batch_id": f"{stamp}_m", "kind": "monitor",
           "research_id": rid, "version": ver, "market": market, "horizon": 0, "loops": 0, "budget_usd": 0.0,
           "target_signal": signal_id, "state": "queued", "created_at": now(), "started_at": None, "ended_at": None,
           "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None}
    write_job(job)
    return job


def create_paper_job(signal_id: str, market: str) -> dict:
    """模拟盘推进任务（factopia/paper/engine.py）：结算挂单、重算信号、月度调仓并出报告，不调用大模型。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rid, ver = signal_id.rsplit(".", 2)[0], int(signal_id.rsplit(".", 2)[1][1:])
    job = {"job_id": f"{stamp}_{secrets.token_hex(2)}_0_{market}", "batch_id": f"{stamp}_p", "kind": "paper",
           "research_id": rid, "version": ver, "market": market, "horizon": 0, "loops": 0, "budget_usd": 0.0,
           "target_signal": signal_id, "state": "queued", "created_at": now(), "started_at": None, "ended_at": None,
           "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None}
    write_job(job)
    return job


def create_agent_job(agent_id: str, research_id: str, version: int, markets: list[str],
                     horizon: int, budget_usd: float) -> dict:
    """研究迭代 Agent 的 runner 任务（kind = "agent"）：等待子任务时不占并行槽。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    job = {"job_id": f"{stamp}_{secrets.token_hex(2)}_0_all", "batch_id": f"{stamp}_a",
           "kind": "agent", "agent_id": agent_id, "research_id": research_id, "version": int(version),
           "market": "all", "horizon": int(horizon), "loops": 0, "budget_usd": float(budget_usd),
           "state": "queued", "created_at": now(), "started_at": None, "ended_at": None,
           "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None,
           "child_jobs": [], "agent_state": "queued"}
    write_job(job)
    return job


def _file_write_job(job: dict) -> None:
    _atomic_write(JOBS_DIR / f"{job['job_id']}.json", _dumps(job, indent=2))


def _file_load_job(job_id: str) -> dict:
    return json.loads((JOBS_DIR / f"{job_id}.json").read_text(encoding="utf-8"))


def _file_list_jobs() -> list[dict]:
    jobs = []
    for p in JOBS_DIR.glob("*.json"):
        if p.name.endswith(".live.json"):  # 实时状态文件，不是任务
            continue
        try:
            jobs.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):  # 正在替换的文件，下次再读
            continue
    return sorted(jobs, key=lambda j: j["job_id"])


def _file_request_cancel(job_id: str) -> None:
    (JOBS_DIR / f"{job_id}.cancel").write_text(now(), encoding="utf-8")


def _file_cancel_requested(job_id: str) -> bool:
    return (JOBS_DIR / f"{job_id}.cancel").exists()


def log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def _file_write_live(job_id: str, state: dict) -> None:
    _atomic_write(JOBS_DIR / f"{job_id}.live.json", _dumps(state))


def _file_load_live(job_id: str) -> dict | None:
    path = JOBS_DIR / f"{job_id}.live.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ── 排队暂停：大模型账户或密钥出错时，后面的任务不再启动 ───────────────────

PAUSE_FILE = JOBS_DIR / ".paused"


def _file_pause(reason: str) -> None:
    _atomic_write(PAUSE_FILE, json.dumps({"reason": reason, "at": now()}, ensure_ascii=False))


def _file_paused() -> dict | None:
    return json.loads(PAUSE_FILE.read_text(encoding="utf-8")) if PAUSE_FILE.exists() else None


def _file_resume() -> None:
    PAUSE_FILE.unlink(missing_ok=True)


def resume_info(job: dict) -> dict:
    """从断点继续（RD-Agent 检查点）：能否续跑、已完成几轮、还剩几轮。RD-Agent 每完成一步就在
    <日志目录>/__session__/<轮>/<步>_<名称> 保存检查点；一轮完整 = 有第 4 步 record。"""
    resumable = ("failed", "cancelled", "budget_stopped") + (("queued", "running") if job.get("resume") else ())   # 已排队 / 刚被运行器取走等待续跑的也算
    if job.get("kind") or job["state"] not in resumable or not job.get("log_dir"):
        return {"ok": False, "reason": "只有失败或已停止的 RD-Agent 研究任务可以从断点继续"}
    sess = Path(job["log_dir"]) / "__session__"
    loops = sorted((p for p in sess.glob("*") if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)) if sess.exists() else []
    if not loops or not any(loops[0].iterdir()):
        return {"ok": False, "reason": "没有可用的检查点（第一步都没有完成），请重新运行"}
    complete = sum(1 for p in loops if any(f.name.startswith("4_") for f in p.iterdir()))
    remaining = int(job["loops"]) - complete
    if remaining <= 0:
        return {"ok": False, "reason": f"已完成全部 {job['loops']} 轮，无需续跑"}
    last = max((f for p in loops for f in p.iterdir()), key=lambda f: (int(f.parent.name), int(f.name.split("_")[0])))
    return {"ok": True, "complete": complete, "remaining": remaining, "checkpoint": f"第 {last.parent.name} 轮 · {last.name.split('_', 1)[1]}"}


def resume_job(job: dict, budget_usd: float | None = None) -> dict:
    """原任务重新排队，从检查点续跑：沿用同一运行编号与日志目录，只跑剩下的轮数；续跑记录写入 resumes。"""
    info = resume_info(job)
    if not info["ok"]:
        raise ValueError(info["reason"])
    budget = float(budget_usd) if budget_usd is not None else float(job["budget_usd"])
    if job["cost_usd"] >= budget:
        raise ValueError(f"已花费 ${job['cost_usd']:.2f}，达到预算 ${budget:.2f}：续跑须提高预算")
    job.setdefault("resumes", []).append({"at": now(), "from_state": job["state"], "error": job.get("error"), "rounds": job.get("rounds"),
                                         "cost_usd": job.get("cost_usd"), "checkpoint": info["checkpoint"], "remaining": info["remaining"],
                                         "model": os.environ.get("CHAT_MODEL")})
    job.update(state="queued", resume=True, budget_usd=budget, error=None, ended_at=None)
    write_job(job)
    return job


def retry_job(job: dict) -> dict:
    """失败或停止的任务：用同一版本、市场与参数重新排队（新的批次）。"""
    if job.get("kind") == "monitor":
        new = create_monitor_job(job["target_signal"], job["market"])
        return {"batch_id": new["batch_id"], "jobs": [new]}
    if job.get("kind") == "paper":
        new = create_paper_job(job["target_signal"], job["market"])
        return {"batch_id": new["batch_id"], "jobs": [new]}
    if job.get("kind") == "validate":
        run = json.loads((RUNTIME / "results" / "runs" / job["target_run"] / "run.json").read_text(encoding="utf-8"))
        new = create_validate_job(run, job["loop"], job.get("placebo_n", 20))
        return {"batch_id": new["batch_id"], "jobs": [new]}
    if job.get("kind") == "agent":
        try:                                      # 重置 Agent 状态，让它从检查点继续
            from factopia.agents import store as agent_store
            agent = agent_store.load(job["agent_id"])
            agent.update(state="queued", last_error=None, stop_reason=None)
            agent_store.save(agent)
        except Exception:
            pass
        job.update(state="queued", error=None, ended_at=None, agent_state="queued")
        write_job(job)
        return {"batch_id": job["batch_id"], "jobs": [job]}
    return create_jobs(job["research_id"], job["version"], [job["market"]], job["horizon"], job["loops"], job["budget_usd"],
                       job.get("kind"))


# ══════════════════════════════════════════════════════════════════════════════
# 存储分发：files / dual / db
#
# 上面是原文件实现（_file_*），下面是公开接口。签名、返回值形状、异常类型全部保持不变，
# 调用方（API / runner / exporter）不需要改动。模式由环境变量决定：
#
#     FACTOPIA_STORAGE=files   老路径（缺省，用于回滚）
#     FACTOPIA_STORAGE=dual    写双份：文件为准，DB 补写并记日志；读走文件
#     FACTOPIA_STORAGE=db      读写都走数据库
#     FACTOPIA_STORAGE_RESEARCH / FACTOPIA_STORAGE_JOBS   按域覆盖
# ══════════════════════════════════════════════════════════════════════════════

RESEARCH_DOMAIN = "research"
JOBS_DOMAIN = "jobs"


# ── 研究规格 ─────────────────────────────────────────────────

def ensure_version_file(research_id: str, version: int) -> Path:
    """返回规格文件路径，必要时从数据库物化。

    外部进程（RD-Agent、exporter）只认文件路径，不接受"从数据库读"，所以 db 模式下
    第一次用到某个版本时把它落成只读副本。这份副本是**派生物**，不是事实来源：
    删掉会自动重建，内容以 `research_version.spec` 为准。
    """
    path = version_path(research_id, version)
    if path.exists() or not db.is_primary(RESEARCH_DOMAIN):
        return path
    spec = _repo_research.load_version(research_id, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump(spec, path.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False, width=120)
    return path


def versions(research_id: str) -> list[int]:
    if db.is_primary(RESEARCH_DOMAIN):
        return _repo_research.versions(research_id)
    return _file_versions(research_id)


def load_version(research_id: str, version: int) -> dict:
    if db.is_primary(RESEARCH_DOMAIN):
        return _repo_research.load_version(research_id, version)
    return _file_load_version(research_id, version)


def save_version(spec: dict) -> dict:
    mode = db.mode(RESEARCH_DOMAIN)
    if mode == "db":
        return _repo_research.save_version(spec)
    out = _file_save_version(spec)          # 文件为准，版本号由文件决定
    if mode == "dual":
        # 显式传版本号，避免两边各算一次导致错位
        db.mirror(lambda: _repo_research.save_version(out, version=out["version"]), "research.save_version")
    return out


def research_ids() -> list[str]:
    if db.is_primary(RESEARCH_DOMAIN):
        return _repo_research.research_ids()
    return _file_research_ids()


def termination(research_id: str | None) -> dict | None:
    if db.is_primary(RESEARCH_DOMAIN):
        return _repo_research.termination(research_id)
    return _file_termination(research_id)


def terminate(research_id: str, version: int | None = None, reason: str = "人工终止当前研究",
              by: str = "研究员（演示，未登录）") -> dict:
    mode = db.mode(RESEARCH_DOMAIN)
    if mode == "db":
        return _repo_research.terminate(research_id, version, reason, by)
    out = _file_terminate(research_id, version, reason, by)
    if mode == "dual":
        db.mirror(lambda: _repo_research.terminate(research_id, version, reason, by), "research.terminate")
    return out


# ── 任务队列 ─────────────────────────────────────────────────

def write_job(job: dict) -> None:
    mode = db.mode(JOBS_DOMAIN)
    if mode == "db":
        _repo_jobs.upsert_job(job)
        return
    _file_write_job(job)
    if mode == "dual":
        db.mirror(lambda: _repo_jobs.upsert_job(job), "jobs.write_job")


def load_job(job_id: str) -> dict:
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.load_job(job_id)
    return _file_load_job(job_id)


def list_jobs() -> list[dict]:
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.list_jobs()
    return _file_list_jobs()


def request_cancel(job_id: str) -> None:
    mode = db.mode(JOBS_DOMAIN)
    if mode in ("db", "dual"):
        # 取消是"写"，dual 模式下两边都要写：文件给旧 runner 看，DB 给新 runner 看
        if mode == "dual":
            _file_request_cancel(job_id)
        _repo_jobs.request_cancel(job_id)
        return
    _file_request_cancel(job_id)


def cancel_requested(job_id: str) -> bool:
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.cancel_requested(job_id)
    return _file_cancel_requested(job_id)


def claim_job(active: set[str] | None = None) -> dict | None:
    """取出下一个排队任务（原子）。

    `db` 模式用数据库的 `FOR UPDATE SKIP LOCKED`，多个 runner 不会重复领取；
    `files` / `dual` 模式仍是扫描目录的老逻辑（读走文件，不做跨进程互斥）。
    """
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.claim_job(active)
    if _file_paused():
        return None
    for job in _file_list_jobs():
        if job["state"] != "queued" or job["job_id"] in (active or set()):
            continue
        if _file_cancel_requested(job["job_id"]):
            job.update(state="cancelled", ended_at=now())
            _file_write_job(job)
            continue
        return job
    return None


def recover_running() -> list[dict]:
    """运行器重启的善后：返回被调整过的任务。db 模式一条 SQL 搞定，避免读一遍再写一遍。"""
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.recover_running()
    touched = []
    for job in _file_list_jobs():
        if job["state"] != "running":
            continue
        if job.get("kind") == "agent":
            job.update(state="queued", error=None, ended_at=None)
        else:
            job.update(state="failed", ended_at=now(), error="运行器重启，运行中断")
        _file_write_job(job)
        touched.append(job)
    return touched


# ── 实时状态 ─────────────────────────────────────────────────

def write_live(job_id: str, state: dict) -> None:
    mode = db.mode(JOBS_DOMAIN)
    if mode == "db":
        _repo_jobs.write_live(job_id, state)
        return
    _file_write_live(job_id, state)
    if mode == "dual":
        db.mirror(lambda: _repo_jobs.write_live(job_id, state), "jobs.write_live")


def load_live(job_id: str) -> dict | None:
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.load_live(job_id)
    return _file_load_live(job_id)


# ── 排队暂停 ─────────────────────────────────────────────────

def pause(reason: str) -> None:
    mode = db.mode(JOBS_DOMAIN)
    if mode == "db":
        _repo_jobs.pause(reason)
        return
    _file_pause(reason)
    if mode == "dual":
        db.mirror(lambda: _repo_jobs.pause(reason), "jobs.pause")


def paused() -> dict | None:
    if db.is_primary(JOBS_DOMAIN):
        return _repo_jobs.paused()
    return _file_paused()


def resume() -> None:
    mode = db.mode(JOBS_DOMAIN)
    if mode == "db":
        _repo_jobs.resume()
        return
    _file_resume()
    if mode == "dual":
        db.mirror(_repo_jobs.resume, "jobs.resume")


# ── AI 解释存档（原 explain_v<k>.json）────────────────────────

def explain(research_id: str, version: int) -> dict | None:
    if db.is_primary(RESEARCH_DOMAIN):
        return _repo_research.explain(research_id, version)
    p = RESEARCH_DIR / research_id / f"explain_v{version}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_explain(research_id: str, version: int, obj: dict) -> None:
    mode = db.mode(RESEARCH_DOMAIN)
    if mode == "db":
        _repo_research.save_explain(research_id, version, obj)
        return
    p = RESEARCH_DIR / research_id / f"explain_v{version}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    if mode == "dual":
        db.mirror(lambda: _repo_research.save_explain(research_id, version, obj), "research.explain")
