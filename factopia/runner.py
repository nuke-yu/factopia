"""后台运行器：按顺序执行页面发起的研究任务（Spec RS-09、第 16.9 节）。

在 rdagent 镜像中常驻运行（docker-compose.yml 的 runner 服务）：
    python -m factopia.runner

每个任务：用锁定的研究规格版本启动一次 RD-Agent（与命令行方式走同一个启动脚本），
运行期间每 3 秒解析一次控制台输出，写入实时状态（jobs/<任务>.live.json：当前步骤、最近活动、回测结果），
每隔 15 秒导出一次结构化进度（results/runs/<运行编号>/）；每完成一轮回测即标记回测明细待导出（exporter 服务），
累计费用超过预算或收到停止请求时中止。大模型余额不足、密钥无效时，中止并暂停排队，避免后面的任务连续失败。

研究任务完成且有被采纳的轮次时，自动排入一个可信度检查任务（factopia/validate：未见数据、安慰剂、显著性），
同样在这个队列中执行，不调用大模型。

任务并行执行（最多 FACTOPIA_MAX_PARALLEL 个，默认 3；Spec BR-37）：RD-Agent 每次运行都会改写源码目录中的配置模板，
因此每个任务使用独立的源码副本（runtime/rdagent_src/<任务>，启动脚本复制）与独立的工作目录
（runtime/rdagent/<市场>/h<N>/jobs/<任务>）；停止任务时只停止该任务的回测容器。
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from factopia.export import rdagent_run
from factopia.mining.rdagent_ext.prepare_run import workdir as rdagent_workdir
from factopia.research import store
from factopia.research.live import ANSI, LiveParser
from factopia.validate import jobs as validate_jobs

ENTRYPOINT = os.environ.get("FACTOPIA_ENTRYPOINT", "/usr/local/bin/entrypoint.sh")
POLL_S, EXPORT_EVERY_S, STOP_GRACE_S = 3, 15, 90
QLIB_IMAGE = os.environ.get("QLIB_DOCKER_IMAGE", "factopia-qlib:dev")
API_UID = int(os.environ.get("FACTOPIA_API_UID", "10001"))  # api 镜像以非 root 用户运行（docker/api/Dockerfile）
MAX_PARALLEL = int(os.environ.get("FACTOPIA_MAX_PARALLEL", "3"))
SRC_ROOT = store.RUNTIME / "rdagent_src"


def log(msg: str) -> None:
    print(f"[runner {datetime.now():%H:%M:%S}] {msg}", flush=True)


def recover() -> None:
    """运行器重启时，上次未结束的任务已随进程中断。"""
    for job in store.list_jobs():
        if job["state"] == "running":
            job.update(state="failed", ended_at=store.now(), error="运行器重启，运行中断")
            store.write_job(job)
            log(f"{job['job_id']}: 标记为中断")


def next_job(active: set[str] | None = None) -> dict | None:
    if store.paused():
        return None
    for job in store.list_jobs():
        if job["state"] != "queued" or job["job_id"] in (active or set()):
            continue
        if store.cancel_requested(job["job_id"]):
            job.update(state="cancelled", ended_at=store.now())
            store.write_job(job)
            continue
        return job
    return None


def export_progress(job: dict, log_dir: Path) -> dict | None:
    """导出当前进度；日志正在写入或还没有任何一轮时返回 None。"""
    try:
        out = rdagent_run.export(log_dir)
    except Exception:
        return None
    run = json.loads((out / "run.json").read_text())
    job.update(cost_usd=run["cost_usd"], rounds=run["rounds"])
    return run


def follow(tail, live: LiveParser, job: dict) -> None:
    """读取控制台的新输出，更新并写入实时状态。"""
    for line in tail:
        live.feed(line)
    store.write_live(job["job_id"], {**live.state(), "updated_at": store.now()})


def mark_new_backtests(run_id: str, done: int) -> int:
    """有新的一轮完成回测时，标记回测明细待导出，运行中即可查看各轮净值。"""
    try:
        rounds = json.loads((rdagent_run.RUNS_DIR / run_id / "rounds.json").read_text())
    except (OSError, json.JSONDecodeError):
        return done
    n = sum(1 for r in rounds if r.get("metrics"))
    if n > done:
        (rdagent_run.RUNS_DIR / run_id / ".backtest_pending").write_text(store.now())
    return max(n, done)


def last_error(log_file: Path) -> str:
    """控制台输出中最后一条异常信息；没有时取最后几行。"""
    import re
    lines = [ANSI.sub("", x) for x in log_file.read_text(errors="replace").splitlines()[-400:]]
    found = [x.strip() for x in lines if re.match(r"^\s*\w*(Error|Exception): ", x)]
    return (found[-1] if found else " | ".join(x.strip() for x in lines[-6:] if x.strip()))[:600]


def stop(proc: subprocess.Popen, prefixes: list[Path]) -> None:
    """依次发送中断、终止、强制结束；并停止该任务留在 dind 中的回测容器（按挂载路径识别，不影响其他任务）。"""
    for sig, wait in ((signal.SIGINT, STOP_GRACE_S), (signal.SIGTERM, 20), (signal.SIGKILL, 10)):
        if proc.poll() is not None:
            break
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
    try:
        import docker
        for c in docker.from_env().containers.list(filters={"ancestor": QLIB_IMAGE}):
            mounts = [m.get("Source", "") for m in c.attrs.get("Mounts", [])]
            if any(s.startswith(str(p)) for s in mounts for p in prefixes):
                c.stop(timeout=10)
    except Exception as e:  # 清理失败不影响任务状态
        log(f"清理回测容器失败：{e}")


def run_job(job: dict) -> None:
    resume = store.resume_info(job) if job.get("resume") else None
    if job.get("resume") and not (resume and resume["ok"]):   # 要求续跑却没有检查点：不能悄悄改成从头开始
        job.update(state="failed", resume=False, ended_at=store.now(), error=f"无法从断点继续：{(resume or {}).get('reason')}")
        store.write_job(job)
        log(f"{job['job_id']}: {job['error']}")
        return
    if resume and resume["ok"]:                        # 从断点继续：同一日志目录与运行编号，只跑剩下的轮数
        log_dir = Path(job["log_dir"])
        run_id = job["run_id"]
        # RD-Agent 续跑时从第 0 轮重新计数（已完成的轮直接跳过），--loop-n 是总轮数而不是剩余轮数
        cmd = [ENTRYPOINT, "rdagent", "fin_factor", "--path", str(log_dir), "--loop-n", str(job["loops"])]
        job.update(state="running", resumed_at=store.now(), resume=False)
        log(f"{job['job_id']}: 从断点继续（{resume['checkpoint']}，还剩 {resume['remaining']} 轮）→ {run_id}")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
        log_dir = store.RUNTIME / "rdagent" / job["market"] / f"h{job['horizon']}" / "log" / stamp
        run_id = rdagent_run._run_id(log_dir)[0]
        cmd = [ENTRYPOINT, "rdagent", "fin_factor", "--loop-n", str(job["loops"])]
        job.update(state="running", started_at=store.now(), run_id=run_id, log_dir=str(log_dir), resume=False)
        log(f"{job['job_id']}: 开始 {job['research_id']} v{job['version']} {job['market']} → {run_id}")
    store.write_job(job)

    src = SRC_ROOT / job["job_id"]
    wd = rdagent_workdir(job["market"], job["horizon"], job["job_id"])
    env = {**os.environ,
           "FACTOPIA_RESEARCH": str(store.version_path(job["research_id"], job["version"])),
           "FACTOPIA_MARKET": job["market"], "FACTOPIA_JOB_ID": job["job_id"], "LOG_TRACE_PATH": str(log_dir),
           "FACTOPIA_SRC_DIR": str(src), **({"FACTOPIA_RESUME": "1"} if resume and resume["ok"] else {})}
    live = LiveParser()
    log_file = store.log_path(job["job_id"])
    with log_file.open("ab") as out, log_file.open("r", errors="replace") as tail:
        tail.seek(0, os.SEEK_END)                      # 只解析本次运行的输出：续跑 / 重试沿用同一日志文件，旧的错误不能再触发停止
        proc = subprocess.Popen(cmd,
                                stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        reason, last_export, backtested = None, 0.0, 0
        while proc.poll() is None:
            time.sleep(POLL_S)
            follow(tail, live, job)
            if live.fatal:
                reason = "fatal"
            elif store.cancel_requested(job["job_id"]):
                reason = "cancelled"
            elif time.time() - last_export >= EXPORT_EVERY_S:
                last_export = time.time()
                if export_progress(job, log_dir) is not None:
                    store.write_job(job)
                    backtested = mark_new_backtests(run_id, backtested)
                if job["cost_usd"] >= job["budget_usd"]:
                    reason = "budget_stopped"
            if reason:
                log(f"{job['job_id']}: 停止（{live.fatal or reason}）")
                stop(proc, [wd, src])
                break
        follow(tail, live, job)

    shutil.rmtree(src, ignore_errors=True)      # 源码副本只在运行期间需要；工作目录保留（回测文件供导出与可信度检查）
    run = export_progress(job, log_dir)
    if live.fatal:
        job.update(state="failed", error=live.fatal)
        store.pause(live.fatal)
        log(f"暂停排队：{live.fatal}")
    elif reason:
        job["state"] = reason
    elif proc.returncode == 0:
        job["state"] = "done"
    else:
        job["state"] = "failed"
        job["error"] = f"RD-Agent 退出码 {proc.returncode}：{last_error(log_file)}"
    if run is None and job["state"] == "done":
        job.update(state="failed", error="RD-Agent 已结束，但没有完成任何一轮")
    job["ended_at"] = store.now()
    store.write_job(job)
    if run is not None:  # 回测与持仓需要 Qlib 读取，由 exporter 服务（app 镜像）导出
        (rdagent_run.RUNS_DIR / run_id / ".backtest_pending").write_text(store.now())
    log(f"{job['job_id']}: {job['state']}，{job['rounds']} 轮，${job['cost_usd']:.3f}")
    if job["state"] == "done":
        enqueue_validation(run_id)


def enqueue_validation(run_id: str) -> None:
    """对 RD-Agent 最终采纳的轮次排入可信度检查。"""
    try:
        run = json.loads((rdagent_run.RUNS_DIR / run_id / "run.json").read_text())
        rounds = json.loads((rdagent_run.RUNS_DIR / run_id / "rounds.json").read_text())
    except (OSError, json.JSONDecodeError):
        return
    adopted = [r for r in rounds if r.get("decision") and r.get("metrics")]
    if adopted:
        v = store.create_validate_job(run, adopted[-1]["loop"])
        log(f"{v['job_id']}: 排入可信度检查 {run_id} 第 {adopted[-1]['loop']} 轮")


def run_validation(job: dict) -> None:
    job.update(state="running", started_at=store.now())
    store.write_job(job)
    log(f"{job['job_id']}: 可信度检查 {job['target_run']} 第 {job['loop']} 轮")
    try:
        validate_jobs.run(job, QLIB_IMAGE, store.log_path(job["job_id"]), lambda: store.cancel_requested(job["job_id"]))
        job["state"] = "done"
    except InterruptedError:
        job["state"] = "cancelled"
    except Exception as e:
        job.update(state="failed", error=str(e)[:800])
    job["ended_at"] = store.now()
    store.write_job(job)
    log(f"{job['job_id']}: {job['state']}{'：' + job['error'] if job.get('error') else ''}")


def run_monitor(job: dict) -> None:
    """信号监控（第 14.15 节）：在运行器进程内执行（需要 pandas / PyTables 读取完整数据）。"""
    from factopia import signals
    from factopia.signals import monitor
    job.update(state="running", started_at=store.now())
    store.write_job(job)
    log(f"{job['job_id']}: 信号监控 {job['target_signal']}")
    try:
        res = monitor.run(signals.monitor_entry(job["target_signal"]), log=log)
        job.update(state="done", summary=res["suggestion_zh"])
    except Exception as e:
        job.update(state="failed", error=str(e)[:800])
    job["ended_at"] = store.now()
    store.write_job(job)
    log(f"{job['job_id']}: {job['state']}{'：' + job['error'] if job.get('error') else ''}")


def run_text(job: dict) -> None:
    """文本信号研究（第 14.17 节）：检验情报台的文本信号，输出与 RD-Agent 运行相同格式的结果，并直接计算诊断。"""
    from factopia.diagnostics import diagnose_run
    from factopia.text import research as text_research
    job.update(state="running", started_at=store.now())
    store.write_job(job)
    try:
        run_id = text_research.run(job, log=log)
        job.update(run_id=run_id, rounds=1)
        store.write_job(job)
        diagnose_run(rdagent_run.RUNS_DIR / run_id)
        job["state"] = "done"
    except Exception as e:
        job.update(state="failed", error=f"{e}"[:800] or traceback.format_exc()[-800:])
        log(traceback.format_exc())
    job["ended_at"] = store.now()
    store.write_job(job)
    log(f"{job['job_id']}: {job['state']}{'：' + job['error'] if job.get('error') else ''}")


def init_dirs() -> None:
    """研究规格、任务、组合、报告与审批目录由后端写入：交给 api 的用户，运行器以 root 运行仍可读写。"""
    for d in (store.RESEARCH_DIR, store.JOBS_DIR, store.RUNTIME / "portfolio", store.RUNTIME / "reports",
              store.RUNTIME / "approvals", store.RUNTIME / "signals", store.RUNTIME / "acceptance",
              store.RUNTIME / "access", store.RUNTIME / "ideas"):
        d.mkdir(parents=True, exist_ok=True)
        os.chown(d, API_UID, API_UID)
        d.chmod(0o775)


def execute(job: dict) -> None:
    try:
        {"validate": run_validation, "monitor": run_monitor, "text": run_text}.get(job.get("kind"), run_job)(job)
    except Exception:
        job.update(state="failed", ended_at=store.now(), error=traceback.format_exc()[-1200:])
        store.write_job(job)
        log(f"{job['job_id']}: 运行器异常\n{job['error']}")


def main() -> None:
    init_dirs()
    recover()
    log(f"等待任务（最多同时运行 {MAX_PARALLEL} 个）")
    active: dict[str, threading.Thread] = {}
    while True:
        for jid in [j for j, th in active.items() if not th.is_alive()]:
            del active[jid]
        job = next_job(set(active)) if len(active) < MAX_PARALLEL else None
        if job is None:
            time.sleep(POLL_S)
            continue
        job.update(state="running", **({} if job.get("resume") else {"started_at": store.now()}))   # 先标记，避免下一轮再次取到；续跑保留原开始时间
        store.write_job(job)
        th = threading.Thread(target=execute, args=(job,), name=job["job_id"], daemon=True)
        th.start()
        active[job["job_id"]] = th
        time.sleep(1.2)   # 运行编号精确到秒：同一市场的两个任务不能在同一秒开始


if __name__ == "__main__":
    main()
