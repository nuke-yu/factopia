"""任务队列与实时状态的数据库实现（对应 `runtime/jobs/**`）。

这是方案里**第一个切换的域**（P3 第一步）：任务队列是单写者、高频、竞态明确的域，
切完立刻能观察到"不再丢更新"的收益，且失败影响面最小（任务可以重排）。

与文件时代的差异只有一个，但很关键：

    文件时代 next_job() = 「扫描目录 → 选中第一个 → 再写一次 state=running」
    中间没有任何保护，两个 runner 实例会同时选中同一个任务，后写的覆盖先写的。

    数据库时代 claim() 用一条 `FOR UPDATE SKIP LOCKED` 选中并立刻改状态，
    配合 `pg_try_advisory_xact_lock(hashtext(market))` 保证同一市场不会并发跑两个任务
    （RD-Agent 会改写共享的源码目录模板，同市场并发会互相踩）。

其余函数（load/list/live/pause/cancel）语义与文件版一一对应。
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, fnum, iso, json_safe


def _now() -> str:
    """与 `research/store.py::now()` 同格式：UTC、精确到秒。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")

log = logging.getLogger(__name__)

# 拆成列的字段：需要查询、排序、加约束
COLUMNS = (
    "job_id", "batch_id", "kind", "research_id", "version", "market", "horizon",
    "loops", "budget_usd", "state", "created_at", "started_at", "ended_at",
    "run_id", "cost_usd", "rounds", "error",
)
# 其余字段（target_run / loop / placebo_n / target_signal / agent_id / child_jobs /
# agent_state / resume / log_dir / resumes / cancel_requested_at …）整体进 payload
NUMERIC_COLUMNS = ("budget_usd", "cost_usd")
TIME_COLUMNS = ("created_at", "started_at", "ended_at")
INT_COLUMNS = ("version", "horizon", "loops", "rounds")

PAUSE_KEY = "queue.paused"


def _split(job: dict) -> tuple[dict, dict]:
    """一个 job dict → (列, payload)。payload 保留所有非列字段，不丢任何键。"""
    row = {k: job.get(k) for k in COLUMNS}
    for k in NUMERIC_COLUMNS:
        row[k] = fnum(row.get(k)) or 0
    for k in TIME_COLUMNS:
        row[k] = as_dt(row.get(k))
    for k in INT_COLUMNS:
        row[k] = int(row.get(k) or 0)
    row["job_id"] = str(row["job_id"])
    row["batch_id"] = str(row.get("batch_id") or row["job_id"])
    row["kind"] = str(row.get("kind") or "research")
    row["market"] = str(row.get("market") or "all")
    row["state"] = str(row.get("state") or "queued")
    row["rounds"] = int(row.get("rounds") or 0)

    # resumes 有独立的 job_resume 表（方案 4.3），不进 payload，避免两份真相
    payload = {k: v for k, v in job.items() if k not in COLUMNS and k != "resumes"}
    return row, json_safe(payload)


RESUME_COLUMNS = ("at", "from_state", "error", "rounds", "cost_usd", "checkpoint", "remaining", "model")


def _sync_resumes(conn, job_id: str, resumes: list[dict]) -> None:
    """把续跑记录整组重写：数量很少（个位数），删了重插比 diff 更简单也更不容易错。"""
    conn.execute("DELETE FROM factopia.job_resume WHERE job_id = %s", (job_id,))
    for i, rec in enumerate(resumes or [], start=1):
        conn.execute(
            f"""INSERT INTO factopia.job_resume (job_id, seq, {", ".join(RESUME_COLUMNS)})
                VALUES (%s, %s, {", ".join(["%s"] * len(RESUME_COLUMNS))})""",
            (job_id, i, as_dt(rec.get("at")), rec.get("from_state"), rec.get("error"),
             rec.get("rounds"), fnum(rec.get("cost_usd")), rec.get("checkpoint"),
             rec.get("remaining"), rec.get("model")),
        )


def _load_resumes(conn, job_ids: list[str]) -> dict[str, list[dict]]:
    if not job_ids:
        return {}
    rows = conn.execute(
        f"""SELECT job_id, {", ".join(RESUME_COLUMNS)} FROM factopia.job_resume
            WHERE job_id = ANY(%s) ORDER BY job_id, seq""",
        (job_ids,),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r[0], []).append({
            "at": iso(r[1]), "from_state": r[2], "error": r[3], "rounds": r[4],
            "cost_usd": fnum(r[5]), "checkpoint": r[6], "remaining": r[7], "model": r[8],
        })
    return out


def _merge(row: dict, payload: dict | None) -> dict:
    """(列, payload) → 原始的 job dict 形状。"""
    job = dict(row)
    for k in NUMERIC_COLUMNS:
        job[k] = fnum(job.get(k))
    for k in TIME_COLUMNS:
        job[k] = iso(job.get(k))
    for k in INT_COLUMNS:
        job[k] = None if job.get(k) is None else int(job[k])
    job["rounds"] = int(job.get("rounds") or 0)
    job.update(payload or {})
    # `kind='research'` 是表结构的缺省值，但文件时代这类任务的 json 里**没有 kind 键**，
    # 而 `resume_info()` 用 `if job.get("kind")` 判断"是不是特殊任务"——若把 'research'
    # 原样带出去，所有 RD-Agent 研究任务的断点续跑会被静默禁用。所以读取时还原缺省语义：
    # 'research' 视为"没有 kind"，只有 text/validate/monitor/paper/agent 才显式保留。
    if job.get("kind") == "research":
        job.pop("kind")
    return job


_RETURNING = ", ".join(COLUMNS) + ", payload"
_UPSERT = f"""
INSERT INTO factopia.job ({", ".join(COLUMNS)}, payload)
VALUES ({", ".join(f"%({c})s" for c in COLUMNS)}, %(payload)s)
ON CONFLICT (job_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "job_id")},
    payload = EXCLUDED.payload
RETURNING {_RETURNING}
"""


def upsert_job(job: dict) -> dict:
    """整条写入（新建或覆盖）。返回数据库里的最终形态。"""
    row, payload = _split(job)
    row["payload"] = Jsonb(payload)
    with db.tx() as conn:
        out = conn.execute(_UPSERT, row).fetchone()
        if "resumes" in job:
            _sync_resumes(conn, row["job_id"], job["resumes"])
        resumes = _load_resumes(conn, [row["job_id"]]).get(row["job_id"], [])
    merged = _merge(dict(zip(COLUMNS, out[: len(COLUMNS)])), out[-1])
    if resumes:
        merged["resumes"] = resumes
    return merged


def load_job(job_id: str) -> dict:
    """读一条任务。找不到时抛 `FileNotFoundError`，与文件版 `Path.read_text` 行为一致，
    调用方的 except 分支不用改。"""
    with db.conn() as conn:
        row = conn.execute(
            f"SELECT {_RETURNING} FROM factopia.job WHERE job_id = %s", (job_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"任务不存在: {job_id}")
        resumes = _load_resumes(conn, [job_id]).get(job_id, [])
    job = _merge(dict(zip(COLUMNS, row[: len(COLUMNS)])), row[-1])
    if resumes:
        job["resumes"] = resumes
    return job


def list_jobs() -> list[dict]:
    """全部任务，按 job_id 升序（与文件版一致；job_id 自带时间戳前缀，等价于先入先出）。"""
    with db.conn() as conn:
        rows = conn.execute(f"SELECT {_RETURNING} FROM factopia.job ORDER BY job_id").fetchall()
        resumes = _load_resumes(conn, [r[0] for r in rows])
    jobs = [_merge(dict(zip(COLUMNS, r[: len(COLUMNS)])), r[-1]) for r in rows]
    for job in jobs:
        if job["job_id"] in resumes:
            job["resumes"] = resumes[job["job_id"]]
    return jobs


def claim_job(active: set[str] | None = None) -> dict | None:
    """原子地取出下一个排队任务并置为 running。

    实现要点（这里踩过一个坑，值得记下来）：

    * **不要在候选查询上直接加 `FOR UPDATE SKIP LOCKED`**。因为 `LIMIT 20` 会把返回的每一行
      都锁住，三个 worker 并发时后到者的候选集会被前两者的行锁全部 SKIP 掉，直接拿到空集
      —— 表现为"三个 worker 只领到两个任务"。正确做法是：
        1) 不带锁地读出候选（可能过期，无所谓）；
        2) 用 `UPDATE … WHERE job_id = ? AND state = 'queued'` 做**条件更新**，
           影响行数为 0 说明别人先拿走了，继续看下一个。
      原子性由这条 UPDATE 保证，而不是由行锁范围保证。
    * `pg_try_advisory_xact_lock(hashtext(market))`：同一市场串行、不同市场并行
      （RD-Agent 会改写共享源码模板）。事务提交即释放，只负责"领取瞬间"的互斥；
      持久互斥由下面 UPDATE 里的 `NOT EXISTS (… state='running')` 兜底。
    * 排队期间被请求取消的任务直接落成 cancelled，不占执行槽。
    """
    skip = list(active or ())
    with db.tx() as conn:
        candidates = conn.execute(
            """
            SELECT job_id, market, payload
            FROM factopia.job
            WHERE state = 'queued'
              AND NOT (job_id = ANY(%s))
            ORDER BY created_at, job_id
            LIMIT 20
            """,
            (skip,),
        ).fetchall()

        for job_id, market, payload in candidates:
            if (payload or {}).get("cancel_requested_at"):
                conn.execute(
                    "UPDATE factopia.job SET state='cancelled', ended_at=now() "
                    "WHERE job_id=%s AND state='queued'",
                    (job_id,),
                )
                log.info("%s: 排队期间已请求停止，落为 cancelled", job_id)
                continue
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (str(market),)
            ).fetchone()[0]
            if not locked:
                continue  # 另一个 runner 正在领取同市场的任务
            row = conn.execute(
                f"""
                UPDATE factopia.job j SET state='running', started_at=now()
                WHERE j.job_id = %s
                  AND j.state = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM factopia.job r
                      WHERE r.state = 'running' AND r.market = j.market
                  )
                RETURNING {_RETURNING}
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                continue  # 被别的 worker 先一步领走
            return _merge(dict(zip(COLUMNS, row[: len(COLUMNS)])), row[-1])
    return None


def recover_running() -> list[dict]:
    """运行器重启时善后：Agent 任务重新排队，其余 running 标为中断。

    与 `runner.recover()` 的规则一致，只是改成在一条 SQL 里做，避免"读一遍再写一遍"。
    """
    with db.tx() as conn:
        back = conn.execute(
            f"""UPDATE factopia.job
                SET state='queued', error=NULL, ended_at=NULL
                WHERE state='running' AND kind='agent'
                RETURNING {_RETURNING}"""
        ).fetchall()
        failed = conn.execute(
            f"""UPDATE factopia.job
                SET state='failed', ended_at=now(), error='运行器重启，运行中断'
                WHERE state='running'
                RETURNING {_RETURNING}"""
        ).fetchall()
    return [
        _merge(dict(zip(COLUMNS, r[: len(COLUMNS)])), r[-1]) for r in list(back) + list(failed)
    ]


# ── 实时状态（原 jobs/<id>.live.json）────────────────────────

def write_live(job_id: str, state: dict) -> None:
    db.execute(
        """INSERT INTO factopia.job_live (job_id, state, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (job_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""",
        (job_id, Jsonb(json_safe(state))),
    )


def load_live(job_id: str) -> dict | None:
    row = db.query_one("SELECT state FROM factopia.job_live WHERE job_id = %s", (job_id,))
    return row[0] if row else None


# ── 取消请求（原 jobs/<id>.cancel）───────────────────────────

def request_cancel(job_id: str) -> None:
    db.execute(
        "UPDATE factopia.job SET payload = payload || %s WHERE job_id = %s",
        (Jsonb({"cancel_requested_at": _now()}), job_id),
    )


def cancel_requested(job_id: str) -> bool:
    row = db.query_one(
        "SELECT payload->>'cancel_requested_at' FROM factopia.job WHERE job_id = %s", (job_id,)
    )
    return bool(row and row[0])


# ── 排队暂停（原 jobs/.paused）───────────────────────────────

def pause(reason: str) -> None:
    db.execute(
        """INSERT INTO factopia.app_state (key, value, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (PAUSE_KEY, Jsonb({"reason": reason, "at": _now()})),
    )


def paused() -> dict | None:
    row = db.query_one("SELECT value FROM factopia.app_state WHERE key = %s", (PAUSE_KEY,))
    return row[0] if row else None


def resume() -> None:
    db.execute("DELETE FROM factopia.app_state WHERE key = %s", (PAUSE_KEY,))


def counts() -> dict[str, int]:
    """各状态任务数：给 overview 页面与监控用，一次索引扫描代替全量文件解析。"""
    rows = db.query("SELECT state, count(*) FROM factopia.job GROUP BY state")
    return {state: int(n) for state, n in rows}
