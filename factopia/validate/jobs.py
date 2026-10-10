"""可信度检查任务：由后台运行器执行（factopia/runner.py），不依赖 Qlib。

准备任务文件（该轮的 Qlib 配置、因子文件、各因子的 AI 代码），把 factopia 源码复制到共享卷，
然后在 dind 中启动 Qlib 回测镜像运行 worker.py：无网络，挂载完整数据（含 2024 年以后）。
AI 生成的因子代码只在这个容器里执行，与 RD-Agent 执行因子代码的方式相同。
"""
import json
import shutil
import time
from pathlib import Path

from factopia.research import store

RESULTS = store.RUNTIME / "results" / "runs"
VALIDATE = store.RUNTIME / "validate"
SRC = Path(__file__).resolve().parents[1]          # factopia 包目录
PLACEBO_N = 20
SEEDS = 10  # 每个回测用几个哈希种子（并列股票的处理顺序），见 worker.py


def output_path(run_id: str, loop: int) -> Path:
    return RESULTS / run_id / "validation" / f"{loop}.json"


def _artifact(workspace: str, name: str) -> Path | None:
    found = sorted(Path(workspace).glob(f"mlruns/*/*/artifacts/{name}"))
    return found[-1] if found else None


def build_spec(job: dict) -> dict:
    run = json.loads((RESULTS / job["target_run"] / "run.json").read_text())
    rounds = json.loads((RESULTS / job["target_run"] / "rounds.json").read_text())
    focus = next(r for r in rounds if r["loop"] == job["loop"])
    ws = Path(focus["workspace"])
    config = _artifact(str(ws), "config")
    if config is None or not (ws / "combined_factors_df.parquet").exists():
        raise RuntimeError(f"第 {job['loop']} 轮的回测文件不存在（{ws}）")
    # 因子文件包含此前采纳的因子与本轮新因子：按名称在本轮及之前的轮次中找代码（取最近一次）
    codes = {}
    for r in rounds:
        if r["loop"] > job["loop"]:
            break
        for f in r.get("factors") or []:
            if f.get("code"):
                codes[f["name"]] = f["code"]
    return {"run_id": job["target_run"], "loop": job["loop"], "market": job["market"], "horizon": job["horizon"],
            "config_path": str(config), "combined_path": str(ws / "combined_factors_df.parquet"), "codes": codes,
            "placebo_n": job.get("placebo_n", PLACEBO_N), "seeds": job.get("seeds", SEEDS), "data_cache": str(VALIDATE / "data" / job["market"]),
            "work_dir": str(VALIDATE / "work" / job["job_id"]), "output": str(output_path(job["target_run"], job["loop"])),
            "original": {"ai": focus.get("metrics"), "baseline": (run.get("baseline") or {}).get("metrics")}}


def sync_source() -> Path:
    """把 factopia 包复制到共享卷，供回测容器导入（dind 只能挂载命名卷中的路径）。"""
    dst = VALIDATE / "pkg"
    tmp = VALIDATE / f"pkg.tmp{int(time.time())}"
    shutil.copytree(SRC, tmp / "factopia", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(dst, ignore_errors=True)
    tmp.rename(dst)
    return dst


def run(job: dict, image: str, log_file: Path, should_stop) -> None:
    """启动回测容器并等待结束；should_stop() 为真时停止容器。失败时抛出异常（信息给研究员看）。"""
    import docker
    spec = build_spec(job)
    missing = [n for n in _factor_names(spec) if n not in spec["codes"]]
    if missing:
        raise RuntimeError(f"找不到因子代码：{', '.join(missing)}")
    pkg = sync_source()
    spec_path = VALIDATE / "jobs" / f"{job['job_id']}.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    client = docker.from_env()
    c = client.containers.run(
        image, ["python", "-m", "factopia.validate.worker", str(spec_path)], detach=True, network_mode="none",
        environment={"PYTHONPATH": str(pkg), "PYTHONUNBUFFERED": "1"}, working_dir=spec["work_dir"].rsplit("/", 1)[0],
        volumes={str(store.RUNTIME): {"bind": str(store.RUNTIME), "mode": "rw"},
                 str(store.RUNTIME / "qlib_data" / job["market"]): {"bind": "/root/.qlib/qlib_data/cn_data", "mode": "ro"}},
        labels={"factopia.job": job["job_id"]})
    try:
        with log_file.open("ab") as out:
            pos = 0
            while True:
                c.reload()
                logs = c.logs()
                out.write(logs[pos:])
                out.flush()
                pos = len(logs)
                if c.status in ("exited", "dead"):
                    break
                if should_stop():
                    c.stop(timeout=10)
                    raise InterruptedError("cancelled")
                time.sleep(3)
        code = c.wait().get("StatusCode", 1)
        if code != 0:
            lines = [x for x in c.logs().decode(errors="replace").splitlines() if x.strip()]
            err = next((x for x in reversed(lines) if "Error" in x or "失败" in x), lines[-1] if lines else "")
            raise RuntimeError(f"可信度检查失败（退出码 {code}）：{err[:500]}")
    finally:
        try:
            c.remove(force=True)
        except Exception:
            pass
        shutil.rmtree(spec["work_dir"], ignore_errors=True)


def _factor_names(spec: dict) -> list[str]:
    import pyarrow.parquet as pq
    names = pq.read_schema(spec["combined_path"]).names
    # 多级列名写入 parquet 后为 "('feature', 'NAME')" 形式
    out = []
    for n in names:
        if n.startswith("('feature', "):
            out.append(n[len("('feature', '"):-2])
        elif n not in ("datetime", "instrument", "__index_level_0__", "__index_level_1__"):
            out.append(n)
    return out
