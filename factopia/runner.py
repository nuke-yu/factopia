"""后台运行器：按顺序执行页面发起的研究任务（Spec RS-09、第 16.9 节）。

在 rdagent 镜像中常驻运行（docker-compose.yml 的 runner 服务）：
    python -m factopia.runner

每个任务：用锁定的研究规格版本启动一次 RD-Agent（与命令行方式走同一个启动脚本），
运行期间每隔 15 秒导出一次进度（results/runs/<运行编号>/，供"运行中"页面显示），
累计费用超过预算或收到停止请求时中止；结束后标记回测结果待导出（由 exporter 服务完成）。

任务按顺序执行，不并行：RD-Agent 每次运行都会改写共享源码卷中的配置模板，并使用按市场与周期固定的工作目录。
"""
import json
import os
import signal
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from factopia.export import rdagent_run
from factopia.research import store

ENTRYPOINT = os.environ.get("FACTOPIA_ENTRYPOINT", "/usr/local/bin/entrypoint.sh")
POLL_S, EXPORT_EVERY_S, STOP_GRACE_S = 3, 15, 90
QLIB_IMAGE = os.environ.get("QLIB_DOCKER_IMAGE", "factopia-qlib:dev")
API_UID = int(os.environ.get("FACTOPIA_API_UID", "10001"))  # api 镜像以非 root 用户运行（docker/api/Dockerfile）


def log(msg: str) -> None:
    print(f"[runner {datetime.now():%H:%M:%S}] {msg}", flush=True)


def recover() -> None:
    """运行器重启时，上次未结束的任务已随进程中断。"""
    for job in store.list_jobs():
        if job["state"] == "running":
            job.update(state="failed", ended_at=store.now(), error="运行器重启，运行中断")
            store.write_job(job)
            log(f"{job['job_id']}: 标记为中断")


def next_job() -> dict | None:
    for job in store.list_jobs():
        if job["state"] != "queued":
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


def stop(proc: subprocess.Popen) -> None:
    """依次发送中断、终止、强制结束；并停止 RD-Agent 留在 dind 中的回测容器。"""
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
            c.stop(timeout=10)
    except Exception as e:  # 清理失败不影响任务状态
        log(f"清理回测容器失败：{e}")


def run_job(job: dict) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
    log_dir = store.RUNTIME / "rdagent" / job["market"] / f"h{job['horizon']}" / "log" / stamp
    run_id = rdagent_run._run_id(log_dir)[0]
    job.update(state="running", started_at=store.now(), run_id=run_id, log_dir=str(log_dir))
    store.write_job(job)
    log(f"{job['job_id']}: 开始 {job['research_id']} v{job['version']} {job['market']} → {run_id}")

    env = {**os.environ,
           "FACTOPIA_RESEARCH": str(store.version_path(job["research_id"], job["version"])),
           "FACTOPIA_MARKET": job["market"], "FACTOPIA_JOB_ID": job["job_id"], "LOG_TRACE_PATH": str(log_dir)}
    with store.log_path(job["job_id"]).open("ab") as out:
        proc = subprocess.Popen([ENTRYPOINT, "rdagent", "fin_factor", "--loop-n", str(job["loops"])],
                                stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        reason, last_export = None, 0.0
        while proc.poll() is None:
            time.sleep(POLL_S)
            if store.cancel_requested(job["job_id"]):
                reason = "cancelled"
            elif time.time() - last_export >= EXPORT_EVERY_S:
                last_export = time.time()
                if export_progress(job, log_dir) is not None:
                    store.write_job(job)
                if job["cost_usd"] >= job["budget_usd"]:
                    reason = "budget_stopped"
            if reason:
                log(f"{job['job_id']}: 停止（{reason}）")
                stop(proc)
                break

    run = export_progress(job, log_dir)
    if reason:
        job["state"] = reason
    elif proc.returncode == 0:
        job["state"] = "done"
    else:
        job["state"] = "failed"
        tail = store.log_path(job["job_id"]).read_text(errors="replace").strip().splitlines()[-15:]
        job["error"] = f"RD-Agent 退出码 {proc.returncode}：" + " | ".join(tail)[-1200:]
    if run is None and job["state"] == "done":
        job.update(state="failed", error="RD-Agent 已结束，但没有完成任何一轮")
    job["ended_at"] = store.now()
    store.write_job(job)
    if run is not None:  # 回测与持仓需要 Qlib 读取，由 exporter 服务（app 镜像）导出
        (rdagent_run.RUNS_DIR / run_id / ".backtest_pending").write_text(store.now())
    log(f"{job['job_id']}: {job['state']}，{job['rounds']} 轮，${job['cost_usd']:.3f}")


def init_dirs() -> None:
    """研究规格与任务目录由后端写入：交给 api 的用户，运行器以 root 运行仍可读写。"""
    for d in (store.RESEARCH_DIR, store.JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
        os.chown(d, API_UID, API_UID)
        d.chmod(0o775)


def main() -> None:
    init_dirs()
    recover()
    log("等待任务")
    while True:
        job = next_job()
        if job is None:
            time.sleep(POLL_S)
            continue
        try:
            run_job(job)
        except Exception:
            job.update(state="failed", ended_at=store.now(), error=traceback.format_exc()[-1200:])
            store.write_job(job)
            log(f"{job['job_id']}: 运行器异常\n{job['error']}")


if __name__ == "__main__":
    main()
