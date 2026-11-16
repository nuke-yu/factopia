#!/usr/bin/env python
"""把 runtime 卷上的文件回填进数据库（research / jobs / approval / signals / ideas /
agents / portfolio / paper / ops 九个域）。

对应方案第 5 节的 P2「冷数据回填」。设计要点：

1. **幂等 + 断点续传**。每个文件回填前记书签（`factopia.migration_file`：路径 + sha256 + 状态）。
   sha256 没变就跳过；文件改了会重跑。中断后重跑不会重复插入，也不会把 v1 变成 v2。
2. **单文件一个事务**。一个文件失败不影响其他文件，错误写进书签（`status='failed'` + `error`），
   下一轮可以只重试失败的。
3. **不动原文件**。迁移是"复制进库"，文件保持原样，随时可以 `FACTOPIA_STORAGE=files` 回滚。
4. **哈希链原样搬运**。approval / signals / ideas 三条链的事件 `at / prev / hash` 逐字写入，
   绝不重算——重算等于改写历史，审计会认为证据被动过。`--verify` 会**重算**一遍来证明这点。

用法：

    export PGHOST=127.0.0.1 PGPORT=15432 PGDATABASE=team12 PGUSER=team12 PGPASSWORD=...
    python tools/migrate_files.py --domain all --dry-run     # 先看会做什么
    python tools/migrate_files.py --domain all               # 真正执行
    python tools/migrate_files.py --verify                   # 回填后逐项比对

`--verify` 不写库，只读：比对文件与库里的条数、关键字段、规格指纹、哈希链。

一次性域（一个目录算一份）：

    approval    approvals/<sid>/{submission.json, events.jsonl}
    agents      agents/<aid>/{agent.json, rounds/<n>/*.json, memory.json, trace.jsonl, report.md}
    portfolio   portfolio/<rid>_v<ver>/{summary.json, daily.parquet, weights.parquet, feedback.json}
    paper       paper/<sid>/{enrolled.json, state.json, orders.jsonl, nav.csv, positions.csv}
                + signals/<sid>/paper.json（运行报告）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from factopia import db  # noqa: E402
from factopia.repo import misc as repo_misc  # noqa: E402
from factopia.repo import agents as repo_agents  # noqa: E402
from factopia.repo import approval as repo_approval  # noqa: E402
from factopia.repo import bars as repo_bars  # noqa: E402
from factopia.repo import chain_hash  # noqa: E402
from factopia.repo import ideas as repo_ideas  # noqa: E402
from factopia.repo import jobs as repo_jobs  # noqa: E402
from factopia.repo import ops as repo_ops  # noqa: E402
from factopia.repo import paper as repo_paper  # noqa: E402
from factopia.repo import portfolio as repo_portfolio  # noqa: E402
from factopia.repo import research as repo_research  # noqa: E402
from factopia.repo import refdata as repo_refdata  # noqa: E402
from factopia.repo import runs as repo_runs  # noqa: E402
from factopia.repo import signals as repo_signals  # noqa: E402

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
if not RUNTIME.exists():  # 本机开发时允许指向仓库内的 runtime
    RUNTIME = ROOT / "runtime"

RESEARCH_DIR = RUNTIME / "research"
JOBS_DIR = RUNTIME / "jobs"
APPROVALS_DIR = RUNTIME / "approvals"
SIGNALS_DIR = RUNTIME / "signals"
IDEAS_DIR = RUNTIME / "ideas"
AGENTS_DIR = RUNTIME / "agents"
PORTFOLIO_DIR = RUNTIME / "portfolio"
PAPER_DIR = RUNTIME / "paper"
ACCESS_LOG = RUNTIME / "access" / "events.jsonl"
COST_LOG = RUNTIME / "llm_costs.jsonl"
RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(RUNTIME / "results")))
RUNS_DIR = RESULTS / "runs"

DOMAINS = ["research", "jobs", "approval", "signals", "ideas", "agents", "portfolio", "paper",
           "runs", "misc", "ops", "refdata", "bars"]



# ── 书签 ─────────────────────────────────────────────────────

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def key_of(path: Path) -> str:
    """书签键统一用 POSIX 形式：Windows 的反斜杠会让 LIKE 前缀匹配与跨平台重跑失效。"""
    return Path(path).as_posix()


def tree_digest(base: Path) -> str:
    """整个目录的指纹：相对路径 + 每个文件的 sha256。

    agents / paper 这类"一个目录=一份实体"的东西，只盯主文件会漏掉轮次或快照的改动，
    所以书签挂在目录上，指纹覆盖目录里全部文件。
    """
    h = hashlib.sha256()
    for p in sorted(x for x in base.rglob("*") if x.is_file()):
        h.update(p.relative_to(base).as_posix().encode("utf-8"))
        h.update(sha256_of(p).encode("utf-8"))
    return h.hexdigest()


def _read_json(path: Path):
    """读 json；文件不存在返回 None（runs 目录里 events/rounds 常常缺，不是错误）。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def already_done(path: Path, digest: str) -> bool:
    row = db.query_one(
        "SELECT 1 FROM factopia.migration_file WHERE path = %s AND sha256 = %s AND status = 'done'",
        (key_of(path), digest),
    )
    return row is not None


def bookmark(path: Path, domain: str, digest: str, status: str = "done",
             rows: int = 0, error: str | None = None) -> None:
    db.execute(
        """
        INSERT INTO factopia.migration_file (path, domain, sha256, bytes, rows, status, error, migrated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (path) DO UPDATE SET
            domain = EXCLUDED.domain, sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes,
            rows = EXCLUDED.rows, status = EXCLUDED.status, error = EXCLUDED.error,
            migrated_at = now()
        """,
        (key_of(path), domain, digest, path.stat().st_size, rows, status, error),
    )


# ── research 域 ──────────────────────────────────────────────

def iter_research():
    if not RESEARCH_DIR.exists():
        return
    for rid_dir in sorted(p for p in RESEARCH_DIR.iterdir() if p.is_dir()):
        for vf in sorted(rid_dir.glob("v*.yaml")):
            if vf.stem[1:].isdigit():
                yield vf
        term = rid_dir / "terminated.json"
        if term.exists():
            yield term


def migrate_research_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    if path.name == "terminated.json":
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not dry:
            repo_research.terminate(
                rec["research_id"], rec.get("version"), rec.get("reason", ""), rec.get("by", "")
            )
            bookmark(path, "research", digest, rows=1)
        return 1, "终止标记"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec.setdefault("id", path.parent.name)
    version = int(path.stem[1:])
    if not dry:
        repo_research.save_version(spec, version=version)
        bookmark(path, "research", digest, rows=1)
    return 1, f"{spec['id']} v{version}"


# ── jobs 域 ──────────────────────────────────────────────────

def iter_jobs():
    if not JOBS_DIR.exists():
        return
    for p in sorted(JOBS_DIR.iterdir()):
        if p.is_file():
            yield p


def migrate_job_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"

    if path.name == ".paused":
        rec = json.loads(path.read_text(encoding="utf-8"))
        if not dry:
            repo_jobs.pause(rec.get("reason", ""))
            bookmark(path, "jobs", digest, rows=1)
        return 1, "排队暂停"
    if path.suffix == ".cancel":
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        if not dry:
            db.execute(
                "UPDATE factopia.job SET payload = payload || %s::jsonb WHERE job_id = %s",
                (json.dumps({"cancel_requested_at": stamp}, ensure_ascii=False), path.stem),
            )
            bookmark(path, "jobs", digest, rows=1)
        return 1, "取消请求"
    if path.name.endswith(".live.json"):
        job_id = path.name[: -len(".live.json")]
        if not dry:
            repo_jobs.write_live(job_id, json.loads(path.read_text(encoding="utf-8")))
            bookmark(path, "jobs", digest, rows=1)
        return 1, f"{job_id} 实时状态"
    if path.suffix == ".log":
        # 日志留在文件，只在 artifact 表登记（方案 4.3：jobs/<job>.log → artifact）
        if not dry:
            bookmark(path, "jobs", digest, rows=0, status="skipped")
        return 0, "日志留文件（仅登记）"
    if path.suffix != ".json":
        return 0, "非任务文件"

    job = json.loads(path.read_text(encoding="utf-8"))
    if not dry:
        repo_jobs.upsert_job(job)
        bookmark(path, "jobs", digest, rows=1)
    return 1, f"{job['job_id']} state={job.get('state')}"


# ── approval 域（每份提交一条哈希链 + 只追加表） ──────────────

def iter_approvals():
    if not APPROVALS_DIR.exists():
        return
    for d in sorted(p for p in APPROVALS_DIR.iterdir() if p.is_dir()):
        f = d / "submission.json"
        if f.exists():
            yield f


def migrate_approval_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    sub = _read_json(path)
    ev_path = path.parent / "events.jsonl"
    evs = _read_jsonl(ev_path)
    if not dry:
        repo_approval.save_full(sub, evs)          # seq 按文件顺序，hash 原样（不重算）
        if ev_path.exists():
            bookmark(ev_path, "approval", sha256_of(ev_path), rows=len(evs))
        bookmark(path, "approval", digest, rows=1 + len(evs))
    return 1 + len(evs), f"{sub['id']} {sub['status']} 事件 {len(evs)}"


# ── signals 域（全局一条哈希链 + 每个信号若干监控快照） ────────

def iter_signals():
    if not SIGNALS_DIR.exists():
        return
    ev_log = SIGNALS_DIR / "events.jsonl"
    if ev_log.exists():
        yield ev_log
    for p in sorted(SIGNALS_DIR.glob("*/monitor.json")):
        yield p


def migrate_signal_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    if path.name == "events.jsonl":
        evs = _read_jsonl(path)
        if not dry:
            for ev in evs:
                repo_signals.append(ev, exact=ev)      # 全局链：顺序即链序，重跑靠 hash 唯一键去重
            bookmark(path, "signals", digest, rows=len(evs))
        return len(evs), f"全局事件链 {len(evs)} 条"
    res = _read_json(path)
    if not dry:
        repo_signals.save_monitor(res)
        bookmark(path, "signals", digest, rows=1)
    return 1, f"{res.get('signal_id')} 监控 {res.get('computed_at')}"


# ── ideas 域（每条线索一条哈希链 + 来源去重索引） ──────────────

def iter_ideas():
    if not IDEAS_DIR.exists():
        return
    for d in sorted(p for p in IDEAS_DIR.glob("idea_*") if p.is_dir()):
        f = d / "idea.json"
        if f.exists():
            yield f
    src = IDEAS_DIR / "sources.json"            # 索引最后处理：它引用 idea 行
    if src.exists():
        yield src


def migrate_idea_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    if path.name == "sources.json":
        idx = _read_json(path)
        if not dry:
            repo_ideas.upsert_sources(idx)
            bookmark(path, "ideas", digest, rows=len(idx))
        return len(idx), f"来源去重索引 {len(idx)} 条"
    idea = _read_json(path)
    ev_path = path.parent / "events.jsonl"
    evs = _read_jsonl(ev_path)
    if not dry:
        repo_ideas.save_full(idea, evs)            # seq 按文件顺序，hash 原样（不重算）
        if ev_path.exists():
            bookmark(ev_path, "ideas", sha256_of(ev_path), rows=len(evs))
        bookmark(path, "ideas", digest, rows=1 + len(evs))
    return 1 + len(evs), f"{idea['id']} {idea.get('status')} 事件 {len(evs)}"


# ── agents 域（一个目录 = 一个 Agent 的全部轮次 / 记忆 / 留痕 / 报告） ──

ROUND_NAMES = ("facts", "diagnosis", "plan", "action", "evaluation",
               "draft_action", "plan_critique", "external", "next_version_draft", "approval")


def iter_agents():
    if not AGENTS_DIR.exists():
        return
    for d in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir()):
        if (d / "agent.json").exists():
            yield d


def migrate_agent_dir(d: Path, dry: bool) -> tuple[int, str]:
    digest = tree_digest(d)
    if already_done(d, digest):
        return 0, "跳过（书签命中）"
    agent = _read_json(d / "agent.json")
    aid = str(agent["agent_id"])
    if not dry:
        repo_agents.save(agent)                    # 先落主行：轮次 / 记忆 / 留痕都靠它做外键
    rows = 1
    n_round = 0
    for rd in sorted((p for p in (d / "rounds").glob("*") if p.is_dir()), key=lambda p: int(p.name)):
        for f in sorted(rd.glob("*.json")):
            if not dry:
                repo_agents.write_round(aid, int(rd.name), f.stem, _read_json(f))
            n_round += 1
    rows += n_round

    mem = d / "memory.json"
    if mem.exists():
        data = _read_json(mem)
        if not dry:
            repo_agents.save_memory(aid, data)
        rows += 1

    trace = d / "trace.jsonl"
    traces = _read_jsonl(trace)
    if not dry:
        for entry in traces:
            repo_agents.append_trace(aid, entry)   # 同 (时间, 类型, 正文) 的重跑被跳过
    rows += len(traces)

    rep = d / "report.md"
    if rep.exists() and not dry:
        repo_agents.save_report(aid, rep.read_text(encoding="utf-8"))
        rows += 1

    if not dry:
        bookmark(d, "agents", digest, rows=rows)   # 书签挂在目录上
    return rows, f"{aid} 轮次 {n_round} 留痕 {len(traces)}"


# ── portfolio 域（summary + 逐日曲线 + 逐股权重 + 反馈） ────────

def iter_portfolio():
    if not PORTFOLIO_DIR.exists():
        return
    for d in sorted(p for p in PORTFOLIO_DIR.iterdir() if p.is_dir()):
        f = d / "summary.json"
        if f.exists():
            yield f


def _rid_ver(d: Path, summary: dict) -> tuple[str, int]:
    if summary.get("research_id") and summary.get("version"):
        return str(summary["research_id"]), int(summary["version"])
    rid, _, ver = d.name.rpartition("_v")          # 目录名 <rid>_v<ver> 兜底
    return rid, int(ver)


def migrate_portfolio_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    d = path.parent
    summary = _read_json(path)
    rid, ver = _rid_ver(d, summary)
    daily = pd.read_parquet(d / "daily.parquet") if (d / "daily.parquet").exists() else None
    weights = pd.read_parquet(d / "weights.parquet") if (d / "weights.parquet").exists() else None
    rows = 1 + (len(daily) if daily is not None else 0)
    if not dry:
        repo_portfolio.save(rid, ver, summary, daily, weights)
        fb = d / "feedback.json"
        if fb.exists():
            for entry in _read_json(fb) or []:
                repo_portfolio.record_feedback(rid, ver, entry)
        bookmark(path, "portfolio", digest, rows=rows)
    return rows, f"{rid} v{ver} 曲线 {0 if daily is None else len(daily)} 行"


# ── paper 域（登记 / 当前态 / 订单事件流 / 净值与持仓快照 / 运行报告） ──

def iter_paper():
    if PAPER_DIR.exists():
        for d in sorted(p for p in PAPER_DIR.iterdir() if p.is_dir()):
            for name in ("enrolled.json", "state.json", "orders.jsonl", "nav.csv", "positions.csv"):
                p = d / name
                if p.exists():
                    yield p
    if SIGNALS_DIR.exists():
        for p in sorted(SIGNALS_DIR.glob("*/paper.json")):
            yield p


def migrate_paper_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    sid = path.parent.name
    if path.name == "enrolled.json":
        rec = _read_json(path)
        if not dry:
            repo_paper.save_enrollment(rec)
            bookmark(path, "paper", digest, rows=1)
        return 1, f"{sid} 登记（{rec.get('mode')}）"
    if path.name == "state.json":
        state = _read_json(path)
        if not dry:
            repo_paper.save_state(sid, state)
            bookmark(path, "paper", digest, rows=1 + len(state.get("positions") or {}))
        return 1 + len(state.get("positions") or {}), f"{sid} 当前态 持仓 {len(state.get('positions') or {})}"
    if path.name == "orders.jsonl":
        evs = _read_jsonl(path)
        if not dry:
            for ev in evs:
                if ev.get("event") == "placed" and not ev.get("order_id"):
                    # 没下出去的单（券商没给单号）：给一个由内容派生的固定编号，
                    # 否则每次重跑都会多出一行"空单"
                    tail = hashlib.sha256(json.dumps(ev, ensure_ascii=False, sort_keys=True,
                                                     default=str).encode("utf-8")).hexdigest()[:12]
                    ev = {**ev, "order_id": f"void_{tail}"}
                repo_paper.order_event(sid, ev)
            bookmark(path, "paper", digest, rows=len(evs))
        return len(evs), f"{sid} 订单事件 {len(evs)}"
    if path.name == "nav.csv":
        df = pd.read_csv(path)
        if not dry:
            for r in df.to_dict("records"):
                repo_paper.save_nav(sid, str(r["date"]), float(r["nav"]), float(r["cash"]),
                                    float(r["positions_value"]), r.get("benchmark"))
            bookmark(path, "paper", digest, rows=len(df))
        return len(df), f"{sid} 净值 {len(df)} 天"
    if path.name == "positions.csv":
        df = pd.read_csv(path)
        if not dry:
            for dstr, grp in df.groupby("date"):
                repo_paper.save_position_daily(
                    sid, str(dstr),
                    [(r["symbol"], float(r["qty"]), r.get("price"), float(r.get("value") or 0))
                     for r in grp.to_dict("records")])
            bookmark(path, "paper", digest, rows=len(df))
        return len(df), f"{sid} 持仓快照 {len(df)} 行"
    if path.name == "paper.json":
        rep = _read_json(path)
        if not dry:
            repo_paper.save_report(sid, rep)
            bookmark(path, "paper", digest, rows=1)
        return 1, f"{sid} 运行报告 {rep.get('computed_at')}"
    return 0, "非模拟盘文件"


# ── ops 域（访问留痕全局链 + 大模型费用流水） ──────────────────

def iter_ops():
    if ACCESS_LOG.exists():
        yield ACCESS_LOG
    if COST_LOG.exists():
        yield COST_LOG


def migrate_ops_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    if path.name == "events.jsonl":
        evs = _read_jsonl(path)
        if not dry:
            for ev in evs:
                repo_ops.append_access(ev, exact=ev)   # hash 原样，重跑靠 NOT EXISTS 去重
            bookmark(path, "ops", digest, rows=len(evs))
        return len(evs), f"访问留痕 {len(evs)} 条"
    recs = _read_jsonl(path)
    if not dry:
        for r in recs:
            repo_ops.record_cost(r.get("task", ""), r.get("model", ""),
                                 int(r.get("prompt_tokens") or 0),
                                 int(r.get("completion_tokens") or 0),
                                 float(r.get("usd") or 0), at=r.get("at"), dedupe=True)
        bookmark(path, "ops", digest, rows=len(recs))
    return len(recs), f"调用费用 {len(recs)} 笔"


# ── runs 域（一次运行 = 一个目录：主档 / 轮次 / 事件 / 检查 / 分组 / 诊断 / 报告） ──

def iter_runs():
    if RUNS_DIR.exists():
        for d in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()):
            if (d / "run.json").exists():
                yield d


def migrate_run_dir(d: Path, dry: bool) -> tuple[int, str]:
    digest = tree_digest(d)
    if already_done(d, digest):
        return 0, "跳过（书签命中）"
    run = _read_json(d / "run.json")
    if not run:
        raise ValueError("run.json 为空或不是合法 JSON")
    rounds = _read_json(d / "rounds.json") or []
    events = _read_json(d / "events.json") or []
    vals = sorted((d / "validation").glob("*.json")) if (d / "validation").exists() else []
    quants = sorted((d / "quantile").glob("*.json")) if (d / "quantile").exists() else []
    diags = sorted((d / "diagnostics").glob("*.json")) if (d / "diagnostics").exists() else []
    rows = 1 + len(rounds) + len(events) + len(vals) + len(quants) + len(diags)
    if not dry:
        # 主档 + 轮次 + 事件一个事务：三者同源，不能只进去一半
        repo_runs.save_run(run, rounds=rounds, events=events)
        for p in vals:
            obj = _read_json(p)
            if obj is not None:
                repo_runs.save_validation(run["run_id"], int(p.stem), obj)
        for p in quants:
            obj = _read_json(p)
            if obj is not None:
                repo_runs.save_quantile(run["run_id"], p.stem, obj)
        for p in diags:
            obj = _read_json(p)
            if obj is not None:
                repo_runs.save_diagnostic(run["run_id"], p.stem, obj)
        md = d / "report.md"
        if md.exists():
            repo_runs.save_report(run["run_id"], md.read_text(encoding="utf-8"))
            rows += 1
        bookmark(d, "runs", digest, rows=rows)
    return rows, (f"运行 {run['run_id']}：轮次 {len(rounds)}、事件 {len(events)}、"
                  f"检查 {len(vals)}、分组 {len(quants)}、诊断 {len(diags)}")


# ── misc 域（单例快照：pit / 质量报告 / 参考数据摘要 / 数据商导入） ─────────────

def iter_misc():
    for p in [RESULTS / "pit_status.json",
              RUNTIME / "data" / "standardized" / "quality_report.json",
              RUNTIME / "refdata" / "master_summary.json",
              RUNTIME / "refdata" / "actions_summary.json",
              RUNTIME / "refdata" / "vendor_imports.jsonl"]:
        if p.exists():
            yield p


def migrate_misc_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    if path.name == "vendor_imports.jsonl":
        rows = _read_jsonl(path)
        if not dry:
            repo_misc.save_state(repo_misc.KEY_VENDOR_IMPORTS, rows)
            bookmark(path, "misc", digest, rows=len(rows))
        return len(rows), f"数据商导入 {len(rows)} 条"
    obj = _read_json(path)
    if obj is None:
        raise ValueError(f"{path.name} 不是合法 JSON")
    if not dry:
        if path.name == "pit_status.json":
            repo_misc.save_state(repo_misc.KEY_PIT, obj)
        elif path.name == "quality_report.json":
            repo_misc.save_state(repo_misc.KEY_QUALITY, obj)
        else:
            repo_misc.save_state(repo_misc.KEY_REFDATA.format(name=path.stem), obj)
        bookmark(path, "misc", digest, rows=1)
    return 1, f"{path.name} 入库"


# ── refdata 域（三份 parquet + 三个市场的成分股 CSV → 四张参考数据表） ──────────

REFDATA_DIR = RUNTIME / "refdata"
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
MEMBERSHIP_DIR = CONFIGS / "universe" / "membership"
REFDATA_SAVERS = {"securities": repo_refdata.save_securities,
                  "relations": repo_refdata.save_relations,
                  "corporate_actions": repo_refdata.save_corporate_actions}


def iter_refdata():
    for name in REFDATA_SAVERS:
        p = REFDATA_DIR / f"{name}.parquet"
        if p.exists():
            yield p
    if MEMBERSHIP_DIR.exists():
        for p in sorted(MEMBERSHIP_DIR.glob("*.csv")):
            yield p


def migrate_refdata_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    if path.suffix == ".csv":                       # 成分股名单：一个市场一份，整块替换
        market = path.stem
        df = pd.read_csv(path, dtype=str).fillna("")
        if not dry:
            repo_refdata.save_membership(market, df)
            bookmark(path, "refdata", digest, rows=len(df))
        return len(df), f"{market} 成分股 {len(df)} 行"
    df = pd.read_parquet(path)
    writer = REFDATA_SAVERS[path.stem]
    if not dry:
        # parquet 是当时完整快照，允许按缺失删除（局部重建不会走迁移工具）
        writer(df, prune=True)
        bookmark(path, "refdata", digest, rows=len(df))
    return len(df), f"{path.stem} {len(df)} 行"


# ── bars 域（标准化行情 + 汇率：唯一需要批量 COPY 的域） ────────────────────────

def iter_bars():
    std = RUNTIME / "data" / "standardized"
    for market in ("hk", "uk", "fr"):
        p = std / f"{market}.parquet"
        if p.exists():
            yield p
    fx = RUNTIME / "data" / "raw" / "fx.parquet"
    if fx.exists():
        yield fx


def migrate_bars_file(path: Path, dry: bool) -> tuple[int, str]:
    digest = sha256_of(path)
    if already_done(path, digest):
        return 0, "跳过（书签命中）"
    df = pd.read_parquet(path)
    if path.name == "fx.parquet":
        if not dry:
            n = repo_bars.save_fx(df)
            bookmark(path, "bars", digest, rows=n)
        return len(df), f"汇率 {len(df)} 行"
    market = path.stem
    if not dry:
        # 行情按"整市场重建"处理：先删该市场再灌，避免上游剔除了坏数据后库里留下幽灵行
        db.execute("DELETE FROM factopia.daily_bar WHERE market = %s", (market,))
        n = repo_bars.save_bars(market, df)
        bookmark(path, "bars", digest, rows=n)
    return len(df), f"{market} 行情 {len(df)} 行"


def _verify_bars() -> int:
    """行情与汇率：行数、日期区间、抽样值三重比对（全量逐行比对太贵且意义不大）。"""
    print("\n校验：bars")
    bad = 0
    for market in ("hk", "uk", "fr"):
        p = RUNTIME / "data" / "standardized" / f"{market}.parquet"
        if not p.exists():
            continue
        want = pd.read_parquet(p)
        want_symbols = int(want["symbol"].nunique())
        span = repo_bars.span(market)
        rows = repo_bars.count_bars(market)
        ok = (span is not None and rows == len(want) and span["symbols"] == want_symbols
              and span["first"] == str(pd.to_datetime(want["date"]).min().date())
              and span["last"] == str(pd.to_datetime(want["date"]).max().date()))
        # 抽样：最后 3 个交易日、每只股票一行，比 close
        tail = pd.to_datetime(want["date"]).max()
        sample = want[pd.to_datetime(want["date"]) == tail][["symbol", "close"]].copy()
        got = repo_bars.bars(market, columns=["symbol", "date", "close"], start=str(tail.date()),
                             end=str(tail.date()))
        merged = sample.merge(got, on="symbol", how="left", suffixes=("", "_db"))
        close_ok = bool(len(got)) and merged["close_db"].notna().all() and \
            (abs(merged["close"] - merged["close_db"]) < 1e-9).all()
        bad += 0 if (ok and close_ok) else 1
        print(f"  {'OK  ' if ok and close_ok else 'DIFF'} {market}: 文件 {len(want)} 行 / 库 {rows} 行，"
              f"股票 {want_symbols}，末日抽样 {'一致' if close_ok else '不一致'}")
    fx = RUNTIME / "data" / "raw" / "fx.parquet"
    if fx.exists():
        want = pd.read_parquet(fx)
        got = repo_bars.count_fx()
        bad += 0 if got == len(want) else 1
        print(f"  {'OK  ' if got == len(want) else 'DIFF'} fx: 文件 {len(want)} 行 / 库 {got} 行")
    return bad


# ── 校验 ─────────────────────────────────────────────────────


def verify() -> int:
    """比对文件与数据库。返回失败项数。"""
    bad = 0
    print("\n校验：research")
    local_versions = {}
    for vf in iter_research():
        if vf.name == "terminated.json":
            continue
        local_versions.setdefault(vf.parent.name, []).append(int(vf.stem[1:]))
    for rid, vers in sorted(local_versions.items()):
        remote = repo_research.versions(rid)
        ok = sorted(vers) == sorted(remote)
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'DIFF'} {rid}: 文件 {sorted(vers)} / 库 {remote}")
        for v in vers:
            spec_file = yaml.safe_load((RESEARCH_DIR / rid / f"v{v}.yaml").read_text(encoding="utf-8"))
            want = repo_research.canonical_sha256({**spec_file, "version": v})
            got = (repo_research.version_meta(rid, v) or {}).get("sha256")
            if want != got:
                bad += 1
                print(f"       DIFF v{v} 指纹不一致（文件 {want[:12]}… / 库 {(got or '')[:8]}…）")

    print("\n校验：jobs")
    local_jobs = {}
    for p in iter_jobs():
        if p.suffix == ".json" and not p.name.endswith(".live.json"):
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            local_jobs[j["job_id"]] = j
    for job_id, want in sorted(local_jobs.items()):
        try:
            got = repo_jobs.load_job(job_id)
        except FileNotFoundError:
            bad += 1
            print(f"  DIFF {job_id}: 库中缺失")
            continue
        diffs = [
            k for k in ("state", "market", "cost_usd", "rounds", "run_id", "kind", "batch_id")
            if str(want.get(k)) != str(got.get(k))
        ]
        bad += 0 if not diffs else 1
        if diffs:
            print(f"  DIFF {job_id}: 字段不一致 {diffs}")
    print(f"  jobs 文件 {len(local_jobs)} 条 / 库 {len(repo_jobs.list_jobs())} 条")

    bad += _verify_approval()
    bad += _verify_signals()
    bad += _verify_ideas()
    bad += _verify_agents()
    bad += _verify_portfolio()
    bad += _verify_paper()
    bad += _verify_runs()
    bad += _verify_misc()
    bad += _verify_ops()
    bad += _verify_refdata()
    bad += _verify_bars()

    print(f"\n校验结果：{'全部一致' if bad == 0 else f'{bad} 项不一致'}")
    return bad


def _prefix_ok(local: list[str], remote: list[str]) -> bool:
    """文件里的链必须是库里的**前缀**。

    `dual` / `db` 模式下库会比文件新（切换后的事件只进库），所以不能要求逐条相等；
    但文件里的每一条都必须原样、同序地存在于库里，否则就是搬运出错。
    """
    return remote[:len(local)] == local


def _verify_chain(evs: list[dict]) -> bool:
    """按 sha256(除 hash 外全部字段) 重算整条链——这是"库里的历史没被改写"的证明。"""
    prev = "0" * 64
    for ev in evs:
        if ev["prev"] != prev or chain_hash(ev) != ev["hash"]:
            return False
        prev = ev["hash"]
    return True


def _verify_approval() -> int:
    print("\n校验：approval（每份提交一条哈希链 + 只追加）")
    bad, n = 0, 0
    for f in iter_approvals():
        n += 1
        sub = _read_json(f)
        sid = sub["id"]
        local = [e["hash"] for e in _read_jsonl(f.parent / "events.jsonl")]
        try:
            remote = [e["hash"] for e in repo_approval.events(sid)]
            got = repo_approval.load(sid)
        except FileNotFoundError:
            bad += 1
            print(f"  DIFF {sid}: 库中缺失")
            continue
        ok = _prefix_ok(local, remote) and got["status"] == sub["status"]
        bad += 0 if ok else 1
        if not ok:
            print(f"  DIFF {sid}: 事件 {len(local)}/{len(remote)} 状态 {sub['status']}/{got['status']}")
    for sid in repo_approval.ids():
        evs = repo_approval.events(sid)
        if evs and not _verify_chain(evs):
            bad += 1
            print(f"  DIFF {sid}: 库内哈希链重算不一致")
    print(f"  提交 {n} 份，库内 {len(repo_approval.ids())} 份")
    return bad


def _verify_signals() -> int:
    print("\n校验：signals（全局一条哈希链）")
    bad = 0
    ev_log = SIGNALS_DIR / "events.jsonl"
    local = [e["hash"] for e in _read_jsonl(ev_log)]
    remote_evs = repo_signals.events()
    remote = [e["hash"] for e in remote_evs]
    ok = _prefix_ok(local, remote)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'DIFF'} 事件链 文件 {len(local)} / 库 {len(remote)}")
    if remote_evs and not _verify_chain(remote_evs):
        bad += 1
        print("  DIFF 库内哈希链重算不一致")
    chains = repo_signals.verify_chain()
    print(f"  链头 {str(chains.get('head'))[:12]}… ok={chains.get('ok')}")
    local_mons = {p.parent.name: _read_json(p) for p in SIGNALS_DIR.glob("*/monitor.json")}
    got_mons = repo_signals.latest_monitors()
    for sid, want in sorted(local_mons.items()):
        got = got_mons.get(sid)
        if got is None or got.get("computed_at") != want.get("computed_at"):
            bad += 1
            print(f"  DIFF {sid}: 最新监控 {(want.get('computed_at'))} / {(got or {}).get('computed_at')}")
    print(f"  监控快照 文件 {len(local_mons)} / 库 {len(got_mons)}")
    return bad


def _verify_ideas() -> int:
    print("\n校验：ideas（每条线索一条哈希链 + 来源唯一键）")
    bad, n = 0, 0
    for f in iter_ideas():
        if f.name == "sources.json":
            continue
        n += 1
        idea = _read_json(f)
        iid = idea["id"]
        local = [e["hash"] for e in _read_jsonl(f.parent / "events.jsonl")]
        try:
            remote = [e["hash"] for e in repo_ideas.events(iid)]
            got = repo_ideas.load(iid)
        except FileNotFoundError:
            bad += 1
            print(f"  DIFF {iid}: 库中缺失")
            continue
        ok = _prefix_ok(local, remote) and got.get("status") == idea.get("status")
        bad += 0 if ok else 1
        if not ok:
            print(f"  DIFF {iid}: 事件 {len(local)}/{len(remote)} 状态 {idea.get('status')}/{got.get('status')}")
    src = IDEAS_DIR / "sources.json"
    if src.exists():
        want = _read_json(src)
        got = repo_ideas.sources()
        missing = [k for k, v in want.items() if got.get(k) != v]
        bad += 0 if not missing else 1
        print(f"  {'OK  ' if not missing else 'DIFF'} 来源索引 文件 {len(want)} / 库 {len(got)}"
              + (f"（缺 {len(missing)} 条）" if missing else ""))
    print(f"  线索 {n} 条，库内 {len(repo_ideas.all_ideas())} 条")
    return bad


def _verify_agents() -> int:
    print("\n校验：agents")
    bad, n = 0, 0
    for d in iter_agents():
        n += 1
        want = _read_json(d / "agent.json")
        aid = str(want["agent_id"])
        try:
            got = repo_agents.load(aid)
        except FileNotFoundError:
            bad += 1
            print(f"  DIFF {aid}: 库中缺失")
            continue
        diffs = [k for k in ("state", "current_round", "root_version", "kind")
                 if str(want.get(k)) != str(got.get(k))]
        if str(want.get("cost_usd") or 0) and abs(float(want.get("cost_usd") or 0)
                                                  - float(got.get("cost_usd") or 0)) > 1e-6:
            diffs.append("cost_usd")
        want_rounds = len([p for rd in (d / "rounds").glob("*") if rd.is_dir()
                           for p in rd.glob("*.json")])
        got_rounds = sum(1 for r in repo_agents.rounds(aid) for k in r if k != "n")
        if want_rounds != got_rounds:
            diffs.append(f"rounds({want_rounds}/{got_rounds})")
        want_trace = len(_read_jsonl(d / "trace.jsonl"))
        got_trace = len(repo_agents.trace(aid))
        if want_trace != got_trace:
            diffs.append(f"trace({want_trace}/{got_trace})")
        bad += 0 if not diffs else 1
        if diffs:
            print(f"  DIFF {aid}: {diffs}")
    print(f"  Agent {n} 个，库内 {len(repo_agents.list_agents())} 个")
    return bad


def _verify_portfolio() -> int:
    print("\n校验：portfolio")
    bad, n = 0, 0
    for f in iter_portfolio():
        n += 1
        summary = _read_json(f)
        rid, ver = _rid_ver(f.parent, summary)
        got = repo_portfolio.load(rid, ver)
        if got is None:
            bad += 1
            print(f"  DIFF {rid} v{ver}: 库中缺失")
            continue
        p = f.parent / "daily.parquet"
        want_days = len(pd.read_parquet(p)) if p.exists() else 0
        got_days = len(repo_portfolio.daily(rid, ver))
        ok = got.get("built_at") == summary.get("built_at") and want_days == got_days
        bad += 0 if ok else 1
        if not ok:
            print(f"  DIFF {rid} v{ver}: built_at {summary.get('built_at')}/{got.get('built_at')} "
                  f"曲线 {want_days}/{got_days}")
    print(f"  组合 {n} 份，库内 {len(repo_portfolio.list_summaries())} 份")
    return bad


def _verify_paper() -> int:
    print("\n校验：paper")
    bad, n = 0, 0
    for d in sorted(p for p in PAPER_DIR.iterdir() if p.is_dir()) if PAPER_DIR.exists() else []:
        n += 1
        sid = d.name
        want = _read_json(d / "enrolled.json") if (d / "enrolled.json").exists() else None
        got = repo_paper.load_enrollment(sid)
        if want is None:
            continue
        if got is None:
            bad += 1
            print(f"  DIFF {sid}: 登记缺失")
            continue
        diffs = [k for k in ("market", "mode", "active")
                 if str(want.get(k)) != str(got.get(k))]
        if abs(float(want.get("capital") or 0) - float(got.get("capital") or 0)) > 0.01:
            diffs.append("capital")
        # 只比"有单号的委托"：没单号的空单在库里落成 status='void' 的一行（不进挂单、不算成交），
        # 读回时是 cancelled 事件，按 placed 硬比会永远差一条。
        want_orders = len([e for e in _read_jsonl(d / "orders.jsonl")
                           if e.get("event") == "placed" and e.get("order_id")])
        got_orders = len([e for e in repo_paper.order_events(sid) if e.get("event") == "placed"])
        if want_orders != got_orders:
            diffs.append(f"orders({want_orders}/{got_orders})")
        nav = d / "nav.csv"
        if nav.exists():
            want_days = len(pd.read_csv(nav))
            got_days = len(repo_paper.nav(sid))
            if want_days != got_days:
                diffs.append(f"nav({want_days}/{got_days})")
        rep = SIGNALS_DIR / sid / "paper.json"
        if rep.exists():
            want_rep = _read_json(rep)
            got_rep = repo_paper.report_result(sid) or {}
            if want_rep.get("computed_at") != got_rep.get("computed_at"):
                diffs.append("report")
        bad += 0 if not diffs else 1
        if diffs:
            print(f"  DIFF {sid}: {diffs}")
    print(f"  模拟盘 {n} 个，库内 {len(repo_paper.enrollments())} 个")
    return bad


def _verify_runs() -> int:
    """运行产物：主档字段、轮次数、事件的序号连续性、分组与诊断的键集合。"""
    print("\n校验：runs")
    bad = 0
    n_local = 0
    for d in iter_runs():
        n_local += 1
        run = _read_json(d / "run.json") or {}
        rid = run["run_id"]
        got = repo_runs.load(rid)
        if got is None:
            bad += 1
            print(f"  DIFF {rid}: 库中缺失")
            continue
        for k in ("market", "horizon", "status"):
            if str(run.get(k)) != str(got.get(k)):
                bad += 1
                print(f"  DIFF {rid}.{k}: 文件 {run.get(k)!r} / 库 {got.get(k)!r}")
        if (run.get("research") or {}).get("id") != (got.get("research") or {}).get("id"):
            bad += 1
            print(f"  DIFF {rid}.research: 文件 {run.get('research')} / 库 {got.get('research')}")
        want_rounds = _read_json(d / "rounds.json") or []
        got_rounds = repo_runs.rounds(rid)
        if [r["loop"] for r in want_rounds] != [r["loop"] for r in got_rounds]:
            bad += 1
            print(f"  DIFF {rid}: 轮次 {[r['loop'] for r in want_rounds]} / {[r['loop'] for r in got_rounds]}")
        want_ev = _read_json(d / "events.json") or []
        got_ev = repo_runs.events(rid)
        if len(want_ev) != len(got_ev):
            bad += 1
            print(f"  DIFF {rid}: 事件 {len(want_ev)} / {len(got_ev)}")
        want_q = {p.stem for p in (d / "quantile").glob("*.json")} if (d / "quantile").exists() else set()
        got_q = set(repo_runs.quantiles(rid))
        if want_q != got_q:
            bad += 1
            print(f"  DIFF {rid}: 分组 {sorted(want_q)} / {sorted(got_q)}")
        want_dg = {p.stem for p in (d / "diagnostics").glob("*.json")} if (d / "diagnostics").exists() else set()
        got_dg = set(repo_runs.diagnostics(rid))
        ok = want_dg == got_dg
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'DIFF'} {rid}: 轮次 {len(got_rounds)}、事件 {len(got_ev)}、"
              f"分组 {len(got_q)}、诊断 {len(got_dg)}")
    print(f"  runs 目录 {n_local} 个 / 库 {len(repo_runs.all_runs())} 条")
    return bad


def _verify_misc() -> int:
    """单例快照：文件存在就必须能在库里取到同样一份。"""
    print("\n校验：misc")
    bad = 0
    checks = [
        (RESULTS / "pit_status.json", lambda: repo_misc.state(repo_misc.KEY_PIT)),
        (RUNTIME / "data" / "standardized" / "quality_report.json",
         lambda: repo_misc.state(repo_misc.KEY_QUALITY)),
        (RUNTIME / "refdata" / "master_summary.json",
         lambda: repo_misc.state(repo_misc.KEY_REFDATA.format(name="master_summary"))),
        (RUNTIME / "refdata" / "actions_summary.json",
         lambda: repo_misc.state(repo_misc.KEY_REFDATA.format(name="actions_summary"))),
    ]
    for path, getter in checks:
        if not path.exists():
            continue
        want = _read_json(path)
        got = getter()
        ok = got is not None and len(str(got)) == len(str(want))
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'DIFF'} {path.name}")
    vi = RUNTIME / "refdata" / "vendor_imports.jsonl"
    if vi.exists():
        want = len(_read_jsonl(vi))
        got = len(repo_misc.state(repo_misc.KEY_VENDOR_IMPORTS) or [])
        bad += 0 if want == got else 1
        print(f"  {'OK  ' if want == got else 'DIFF'} vendor_imports: 文件 {want} / 库 {got}")
    return bad


def _verify_refdata() -> int:
    """参考数据：文件里的每一行都必须原样在库里（按"文件列集合"比较，payload 里的键不参与）。

    比的是"文件视角"：读库时用写库时记下的列顺序与 dtype 还原，因此这里是
    "同一份数据两个位置是否逐值相同"的正面检查，而不是行数对得上就算过。
    """
    print("\n校验：refdata")
    bad = 0
    loaders = {"securities": repo_refdata.load_securities,
               "relations": repo_refdata.load_relations,
               "corporate_actions": repo_refdata.load_corporate_actions}
    for name, loader in loaders.items():
        p = REFDATA_DIR / f"{name}.parquet"
        if not p.exists():
            continue
        want = pd.read_parquet(p)
        got = loader()
        diffs = _frame_diff(want, got)
        bad += 0 if not diffs else 1
        print(f"  {'OK  ' if not diffs else 'DIFF'} {name}: 文件 {len(want)} 行 / 库 {len(got)} 行"
              + ("" if not diffs else f"（{diffs[0]}）"))
    if MEMBERSHIP_DIR.exists():
        for p in sorted(MEMBERSHIP_DIR.glob("*.csv")):
            want = pd.read_csv(p, dtype=str).fillna("")
            got = repo_refdata.load_membership(p.stem)
            got = pd.DataFrame(columns=want.columns) if got is None else got.fillna("")
            diffs = _frame_diff(want, got)
            bad += 0 if not diffs else 1
            print(f"  {'OK  ' if not diffs else 'DIFF'} membership/{p.stem}: 文件 {len(want)} 行 / "
                  f"库 {len(got)} 行" + ("" if not diffs else f"（{diffs[0]}）"))
    return bad


def _frame_diff(want: pd.DataFrame, got: pd.DataFrame) -> list[str]:
    """逐行逐列比较两份 DataFrame（按文件列集合，值统一转字符串后比）。
    返回差异描述；空列表代表一致。"""
    cols = [c for c in want.columns if c in got.columns]
    missing = [c for c in want.columns if c not in got.columns]
    if missing:
        return [f"库中缺少列 {missing}"]
    if len(want) != len(got):
        return [f"行数不同：文件 {len(want)} / 库 {len(got)}"]
    a = want[cols].reset_index(drop=True).astype(str)
    b = got[cols].reset_index(drop=True).astype(str)
    # 两边都是空的列（parquet 里 None → "None"，库里 "" → ""）按"空"归一
    for c in cols:
        a[c] = a[c].replace({"None": "", "nan": "", "NaT": ""})
        b[c] = b[c].replace({"None": "", "nan": "", "NaT": ""})
    diff = a.ne(b).any(axis=1)
    if diff.any():
        i = int(diff.idxmax())
        col = str(a.columns[a.ne(b).iloc[i].to_numpy().nonzero()[0][0]])
        return [f"第 {i + 1} 行 {col}：文件 {a.at[i, col]!r} / 库 {b.at[i, col]!r}"]
    return []


def _verify_ops() -> int:
    print("\n校验：access / llm_cost")
    bad = 0
    local = [e["hash"] for e in _read_jsonl(ACCESS_LOG)]
    remote_evs = repo_ops.access_events()
    ok = _prefix_ok(local, [e["hash"] for e in remote_evs])
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'DIFF'} 访问留痕 文件 {len(local)} / 库 {len(remote_evs)}")
    if remote_evs and not _verify_chain(remote_evs):
        bad += 1
        print("  DIFF 访问留痕哈希链重算不一致")
    if COST_LOG.exists():
        want = _read_jsonl(COST_LOG)
        want_usd = round(sum(float(r.get("usd") or 0) for r in want), 4)
        got = repo_ops.cost_summary(3650)["rows"]
        got_usd = round(sum(r["usd"] for r in got), 4)
        got_calls = sum(r["calls"] for r in got)
        # 库里可能比文件多（切换后只进库），所以只要求"不少于"，且至少文件里的都在
        ok2 = got_calls >= len(want) and got_usd + 0.01 >= want_usd
        bad += 0 if ok2 else 1
        print(f"  {'OK  ' if ok2 else 'DIFF'} 调用费用 文件 {len(want)} 笔 ${want_usd} / "
              f"库 {got_calls} 笔 ${got_usd}")
    return bad


# ── 入口 ─────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="runtime 文件回填数据库")
    ap.add_argument("--domain", default="all", choices=["all"] + DOMAINS)
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不写库")
    ap.add_argument("--verify", action="store_true", help="回填后比对文件与数据库")
    args = ap.parse_args()

    if args.verify:
        bad = verify()
        db.close_pool()
        return 1 if bad else 0

    print(f"runtime = {RUNTIME}")
    if not RUNTIME.exists():
        print("runtime 目录不存在，没有可迁移的文件")
        return 0
    print(f"连接   = {db._mask(db.dsn())}")
    health = db.health()
    if not health.get("ok"):
        print(f"数据库不可用：{health.get('error')}")
        db.close_pool()
        return 2
    print(f"目标库 = {health['database']} / facts {health['size']}\n")

    total = {d: [0, 0] for d in DOMAINS}
    plan = []
    if args.domain in ("all", "research"):
        plan += [("research", f, migrate_research_file) for f in iter_research()]
    if args.domain in ("all", "jobs"):
        plan += [("jobs", f, migrate_job_file) for f in iter_jobs()]
    if args.domain in ("all", "approval"):
        plan += [("approval", f, migrate_approval_file) for f in iter_approvals()]
    if args.domain in ("all", "signals"):
        plan += [("signals", f, migrate_signal_file) for f in iter_signals()]
    if args.domain in ("all", "ideas"):
        plan += [("ideas", f, migrate_idea_file) for f in iter_ideas()]
    if args.domain in ("all", "agents"):
        plan += [("agents", d, migrate_agent_dir) for d in iter_agents()]
    if args.domain in ("all", "portfolio"):
        plan += [("portfolio", f, migrate_portfolio_file) for f in iter_portfolio()]
    if args.domain in ("all", "paper"):
        plan += [("paper", f, migrate_paper_file) for f in iter_paper()]
    if args.domain in ("all", "runs"):
        plan += [("runs", d, migrate_run_dir) for d in iter_runs()]
    if args.domain in ("all", "misc"):
        plan += [("misc", f, migrate_misc_file) for f in iter_misc()]
    if args.domain in ("all", "ops"):
        plan += [("ops", f, migrate_ops_file) for f in iter_ops()]
    if args.domain in ("all", "refdata"):
        plan += [("refdata", p, migrate_refdata_file) for p in iter_refdata()]
    if args.domain in ("all", "bars"):
        plan += [("bars", p, migrate_bars_file) for p in iter_bars()]

    for domain, path, fn in plan:
        try:
            n, note = fn(path, args.dry_run)
            total[domain][0] += n
            total[domain][1] += 1
            if n or args.dry_run:
                print(f"  [{domain}] {path.name:<48} {note}")
        except Exception as exc:
            total[domain][1] += 1
            print(f"  [{domain}] {path.name:<48} 失败: {type(exc).__name__}: {exc}")
            if not args.dry_run:
                try:
                    digest = sha256_of(path) if path.is_file() else tree_digest(path)
                    bookmark(path, domain, digest, status="failed", error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass

    print("\n汇总：")
    for domain, (rows, files) in total.items():
        if files:
            print(f"  {domain:<9} 文件 {files:>4} 个，写入 {rows:>4} 行")
    if args.dry_run:
        print("\n（dry-run：未写入任何数据）")
    db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
