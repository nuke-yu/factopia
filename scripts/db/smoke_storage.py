#!/usr/bin/env python
"""存储层三模式与队列并发的验收脚本（对真实数据库跑，自带清理）。

验证的是"改造后行为是否与原文件版等价"，不是业务逻辑：

  1. files 模式：写文件、库里没有 —— 老路径不回归
  2. dual 模式：文件为准 + 库里补写一份，读仍走文件
  3. db 模式：只写库、不再产生新文件，读回来的字段类型与文件版一致
  4. 队列原子性：两个并发 worker 不会领到同一个任务（db 模式）
  5. 同市场串行：hk 有任务在跑时，另一个 hk 任务不会被领走
  6. 迁移工具：回填 → 校验一致 → 再跑一次幂等（书签命中）
  7. 规格物化：db 模式下外部进程仍能拿到 YAML 文件

用法（需先把隧道开在 15432）：
    export PGPORT=15432 PGHOST=127.0.0.1 PGDATABASE=team12 PGUSER=team12 PGPASSWORD=...
    python scripts/db/smoke_storage.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = Path(os.environ.get("FACTOPIA_SMOKE_RUNTIME", "/tmp/factopia_rt_smoke"))
RESEARCH_ID = "demo_smoke"
BATCH = "20261115000000_smk"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def build_fixture() -> None:
    """构造一份与线上形状一致的 runtime 样本。"""
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    (FIXTURE / "research" / RESEARCH_ID).mkdir(parents=True)
    (FIXTURE / "jobs").mkdir(parents=True)

    for v, hypothesis in ((1, "动量在港股有效"), (2, "动量+反转在港股有效")):
        (FIXTURE / "research" / RESEARCH_ID / f"v{v}.yaml").write_text(
            f"""id: {RESEARCH_ID}
version: {v}
markets: [hk]
title_zh: 冒烟测试研究
hypothesis: {hypothesis}
horizon: 20
locked_at: '2026-11-15T00:0{v}:00+00:00'
""",
            encoding="utf-8",
        )
    (FIXTURE / "research" / RESEARCH_ID / "terminated.json").write_text(
        json.dumps({"research_id": RESEARCH_ID, "version": 2, "at": "2026-11-15T00:10:00+00:00",
                    "by": "smoke", "reason": "验收用"}, ensure_ascii=False),
        encoding="utf-8",
    )

    job = {
        "job_id": f"{BATCH}_0_hk", "batch_id": BATCH, "research_id": RESEARCH_ID, "version": 1,
        "market": "hk", "horizon": 20, "loops": 2, "budget_usd": 1.5, "state": "queued",
        "created_at": "2026-11-15T00:00:00+00:00", "started_at": None, "ended_at": None,
        "run_id": None, "cost_usd": 0.0, "rounds": 0, "error": None,
    }
    (FIXTURE / "jobs" / f"{job['job_id']}.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    monitor = {**job, "job_id": f"{BATCH}_1_hk", "kind": "monitor", "target_signal": f"{RESEARCH_ID}.v1.hk",
               "loops": 0, "budget_usd": 0.0}
    (FIXTURE / "jobs" / f"{monitor['job_id']}.json").write_text(
        json.dumps(monitor, ensure_ascii=False, indent=2), encoding="utf-8")
    (FIXTURE / "jobs" / f"{job['job_id']}.cancel").write_text("2026-11-15T00:05:00+00:00", encoding="utf-8")
    (FIXTURE / "jobs" / f"{job['job_id']}.live.json").write_text(
        json.dumps({"stage": "running", "progress": 0.4}, ensure_ascii=False), encoding="utf-8")
    (FIXTURE / "jobs" / ".paused").write_text(
        json.dumps({"reason": "大模型余额不足", "at": "2026-11-15T00:06:00+00:00"}, ensure_ascii=False),
        encoding="utf-8")


def cleanup() -> None:
    from factopia import db

    db.execute("DELETE FROM factopia.migration_file WHERE path LIKE %s", (f"{FIXTURE.as_posix()}%",))
    db.execute("DELETE FROM factopia.job WHERE batch_id = %s", (BATCH,))
    db.execute("DELETE FROM factopia.research_version WHERE research_id = %s", (RESEARCH_ID,))
    db.execute("DELETE FROM factopia.research WHERE research_id = %s", (RESEARCH_ID,))
    db.execute("DELETE FROM factopia.app_state WHERE key = 'queue.paused'")
    db.close_pool()


def main() -> int:
    os.environ["FACTOPIA_RUNTIME"] = str(FIXTURE)
    os.environ.setdefault("FACTOPIA_STORAGE", "files")
    os.environ["FACTOPIA_PG_POOL_MAX"] = "4"

    from factopia import db
    from factopia.repo import jobs as repo_jobs
    from factopia.repo import research as repo_research
    from factopia.research import store

    health = db.health()
    if not health.get("ok"):
        print(f"数据库不可用：{health.get('error')}")
        return 2
    print(f"目标库 {health['database']}（{health['size']}）\n")

    build_fixture()
    cleanup_partial = None

    try:
        print("① files 模式（缺省，必须与改造前完全一致）")
        os.environ["FACTOPIA_STORAGE"] = "files"
        spec = {"id": RESEARCH_ID, "markets": ["hk"], "title_zh": "冒烟测试研究", "hypothesis": "文件模式"}
        existing = len(list((FIXTURE / "research" / RESEARCH_ID).glob("v*.yaml")))
        out = store.save_version(spec)
        check("写文件返回下一个版本号", out["version"] == existing + 1 and out["locked_at"],
              f"已有 {existing} 个版本 → v{out['version']}")
        check("文件已落盘", (FIXTURE / "research" / RESEARCH_ID / "v1.yaml").exists())
        check("库里没有该研究", repo_research.versions(RESEARCH_ID) == [], "无 DB 记录")
        jobs = store.list_jobs()
        check("list_jobs 读文件", len(jobs) == 2, f"{len(jobs)} 条")

        # 清掉文件模式产生的 v1，后面从空库开始测 db
        shutil.rmtree(FIXTURE / "research")
        build_fixture()
        db.execute("DELETE FROM factopia.research_version WHERE research_id = %s", (RESEARCH_ID,))
        db.execute("DELETE FROM factopia.research WHERE research_id = %s", (RESEARCH_ID,))

        print("\n② dual 模式（文件为准，库里补写）")
        os.environ["FACTOPIA_STORAGE"] = "dual"
        fn = len(list((FIXTURE / "research" / RESEARCH_ID).glob("v*.yaml"))) + 1
        out = store.save_version(spec)
        check(f"文件写入 v{fn}", out["version"] == fn)
        check("库里补写成功", repo_research.versions(RESEARCH_ID) == [fn], f"{repo_research.versions(RESEARCH_ID)}")
        mirror = store.load_version(RESEARCH_ID, fn)
        check("dual 读仍走文件", mirror.get("hypothesis") == "文件模式")
        store.write_job({"job_id": f"{BATCH}_dual", "batch_id": BATCH, "market": "fr",
                         "state": "queued", "cost_usd": 0.0, "rounds": 0, "loops": 0,
                         "horizon": 0, "budget_usd": 0.0, "created_at": "2026-11-15T00:00:00+00:00"})
        check("任务双写", repo_jobs.load_job(f"{BATCH}_dual")["market"] == "fr")
        db.execute("DELETE FROM factopia.job WHERE batch_id = %s", (BATCH,))

        print("\n③ db 模式（读写都走库）")
        os.environ["FACTOPIA_STORAGE"] = "db"
        os.environ["FACTOPIA_STORAGE_JOBS"] = "db"
        os.environ["FACTOPIA_STORAGE_RESEARCH"] = "db"
        before = len(list((FIXTURE / "research" / RESEARCH_ID).glob("v*.yaml")))
        prev = max(repo_research.versions(RESEARCH_ID))
        out = store.save_version({**spec, "hypothesis": "数据库模式"})
        after = len(list((FIXTURE / "research" / RESEARCH_ID).glob("v*.yaml")))
        check("库里版本递增", out["version"] == prev + 1, f"v{prev} → v{out['version']}")
        check("不再产生新文件", before == after, f"{before} → {after}")
        back = store.load_version(RESEARCH_ID, out["version"])
        check("读回内容一致", back["hypothesis"] == "数据库模式" and back["version"] == out["version"])
        check("时间字段是 UTC ISO 字符串",
              isinstance(back["locked_at"], str) and back["locked_at"].endswith("+00:00"), back["locked_at"])

        print("\n④ 队列原子性与同市场串行（db 模式）")
        db.execute("DELETE FROM factopia.job WHERE batch_id = %s", (BATCH,))
        for i, market in enumerate(["hk", "uk", "fr"]):
            store.write_job({"job_id": f"{BATCH}_q{i}", "batch_id": BATCH, "market": market,
                             "state": "queued", "cost_usd": 0.0, "rounds": 0, "loops": 0,
                             "horizon": 0, "budget_usd": 0.0,
                             "created_at": f"2026-11-15T00:0{i}:00+00:00"})
        claimed: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            job = repo_jobs.claim_job()
            if job:
                with lock:
                    claimed.append(job["job_id"])

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("三个 worker 领到互不相同的任务", len(set(claimed)) == len(claimed) and len(claimed) == 3,
              f"{sorted(claimed)}")

        # 同市场串行：hk 已在跑，另一个 hk 任务不应被领走
        db.execute("UPDATE factopia.job SET state='done' WHERE batch_id = %s AND market <> 'hk'", (BATCH,))
        store.write_job({"job_id": f"{BATCH}_q_hk2", "batch_id": BATCH, "market": "hk",
                         "state": "queued", "cost_usd": 0.0, "rounds": 0, "loops": 0,
                         "horizon": 0, "budget_usd": 0.0,
                         "created_at": "2026-11-15T00:09:00+00:00"})
        again = repo_jobs.claim_job()
        check("同市场不再并发（hk 占用中）", again is None, f"claim={again and again['job_id']}")

        print("\n⑤ 取消与暂停（db 模式）")
        store.request_cancel(f"{BATCH}_q_hk2")
        check("取消请求可读", store.cancel_requested(f"{BATCH}_q_hk2"))
        store.pause("大模型余额不足")
        check("暂停可读写", (store.paused() or {}).get("reason") == "大模型余额不足")
        store.resume()
        check("恢复后为空", store.paused() is None)

        print("\n⑥ 规格物化（外部进程仍要文件路径）")
        path = store.ensure_version_file(RESEARCH_ID, out["version"])
        check("按需物化出 YAML", path.exists() and "数据库模式" in path.read_text(encoding="utf-8"), str(path))

        print("\n⑦ 迁移工具（回填 → 校验 → 幂等重跑）")
        db.execute("DELETE FROM factopia.job WHERE batch_id = %s", (BATCH,))
        db.execute("DELETE FROM factopia.research_version WHERE research_id = %s", (RESEARCH_ID,))
        db.execute("DELETE FROM factopia.research WHERE research_id = %s", (RESEARCH_ID,))
        shutil.rmtree(FIXTURE / "research")
        shutil.rmtree(FIXTURE / "jobs")
        build_fixture()

        env = {**os.environ, "FACTOPIA_RUNTIME": str(FIXTURE), "FACTOPIA_STORAGE": "files"}
        cmd = [sys.executable, str(ROOT / "tools" / "migrate_files.py"), "--domain", "all"]
        run1 = subprocess.run(cmd, capture_output=True, text=True, env=env)
        check("迁移执行成功", run1.returncode == 0, run1.stdout.strip().splitlines()[-1] if run1.stdout else run1.stderr[:120])
        run2 = subprocess.run(cmd, capture_output=True, text=True, env=env)
        skipped = all("书签命中" in line for line in run2.stdout.splitlines() if "[" in line)
        check("二次执行幂等（全部书签命中）", skipped, "没有重复写入")

        ver = subprocess.run(cmd + ["--verify"], capture_output=True, text=True, env=env)
        check("校验文件与库一致", "全部一致" in ver.stdout, ver.stdout.strip().splitlines()[-1] if ver.stdout else "")

    finally:
        os.environ.pop("FACTOPIA_STORAGE_JOBS", None)
        os.environ.pop("FACTOPIA_STORAGE_RESEARCH", None)
        cleanup()
        print("\n" + "-" * 60)
        bad = [r for r in results if not r[1]]
        print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
        left = 0
        try:
            from factopia import db as _db
            left = _db.query_one("SELECT count(*) FROM factopia.job WHERE batch_id = %s", (BATCH,))[0]
            _db.close_pool()
        except Exception:
            pass
        print(f"残留检查：{left} 行（应为 0）")
    return 0 if all(r[1] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
