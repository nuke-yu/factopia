"""运行产物的唯一入口：API / Agent 观察器 / 验收脚本都从这里取，不再自己拼路径。

设计原则见 `factopia/repo/runs.py` 的模块文档：**结构稳定的 JSON 进库，逐日 parquet
留在文件侧**。这个模块负责把两种位置藏起来——调用方问"第 3 轮的回测在哪"，
不需要知道答案是一张表还是一份 parquet。

模式由 `FACTOPIA_STORAGE_RUNS`（或全局 `FACTOPIA_STORAGE`）决定：

    files  run.json / rounds.json / events.json 从 runtime/results/runs 读（迁移前的行为）
    dual   读库；库里没有该 run 时回落到文件（灰度期两边的写入是同步的）
    db     只读库；missing 一律返回 None / 空，绝不偷偷读文件

**为什么 db 模式不允许回落文件**：回落会让"切了但没切"永远不被发现——页面照常
有数字，只是那是上一次运行留下的。宁可让接口返回 404/空，让人立刻看见。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

from factopia import db
from factopia.repo import runs as repo_runs

log = logging.getLogger(__name__)

DOMAIN = "runs"

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RUNS_DIR = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(RUNTIME / "results"))) / "runs"

# 只有这些子目录仍然是纯文件（RD-Agent 逐轮回测的直接产物，列结构随版本变化）
PARQUET_DIRS = ("backtest", "signal", "score", "positions")

_warned = False


def _warn_parquet_once() -> None:
    """db 模式下第一次读 parquet 时提示一次：这类产物仍需要文件卷共挂。"""
    global _warned
    if _warned or not db.is_primary(DOMAIN):
        return
    _warned = True
    log.warning("runs=%s：backtest/signal/score/positions 仍是文件产物（%s），"
                "部署时必须与 api 容器共挂同一结果卷", db.mode(DOMAIN), RUNS_DIR)


def run_dir(run_id: str) -> Path:
    """产物目录（迁移工具与回滚脚本用；业务代码不要直接用它拼文件路径）。"""
    return RUNS_DIR / str(run_id)


# ── 运行主档 ────────────────────────────────────────────────────────────────────────

def exists(run_id: str) -> bool:
    if db.is_primary(DOMAIN):
        if repo_runs.exists(run_id):
            return True
        return db.mode(DOMAIN) == "dual" and (run_dir(run_id) / "run.json").exists()
    return (run_dir(run_id) / "run.json").exists()


def load(run_id: str) -> dict | None:
    if db.is_primary(DOMAIN):
        got = repo_runs.load(run_id)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    return _file_json(run_dir(run_id) / "run.json")


def all_runs() -> list[dict]:
    """全部运行，开始时间倒序。"""
    if db.is_primary(DOMAIN):
        rows = repo_runs.all_runs()
        if rows or db.mode(DOMAIN) == "db":
            return rows
    out = []
    for p in sorted(RUNS_DIR.glob("*/run.json")):
        got = _file_json(p)
        if got is not None:
            out.append(got)
    return out


def latest(done_only: bool = True) -> dict | None:
    if db.is_primary(DOMAIN):
        got = repo_runs.latest(done_only)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    for r in all_runs():
        if done_only and r.get("status") != "done":
            continue
        return r
    return None


def save(run: dict, *, rounds: list[dict] | None = None, events: list[dict] | None = None) -> dict:
    """写主档（伴生轮次/事件一起写）。files 模式不处理——那时文件就是真相。

    `dual` 模式也要写库：灰度期读是"库优先、库空回落文件"，
    若这里只写文件，库会一直落后，等于 dual 什么都没验证。
    """
    got = db.write_db(DOMAIN, lambda: repo_runs.save_run(run, rounds=rounds, events=events),
                      "runstore.save")
    return got or run


# ── 轮次 / 事件 ──────────────────────────────────────────────────────────────────────

def rounds(run_id: str) -> list[dict]:
    if db.is_primary(DOMAIN):
        got = repo_runs.rounds(run_id)
        if got or db.mode(DOMAIN) == "db":
            return got
    return _file_json(run_dir(run_id) / "rounds.json") or []


def events(run_id: str) -> list[dict]:
    if db.is_primary(DOMAIN):
        got = repo_runs.events(run_id)
        if got or db.mode(DOMAIN) == "db":
            return got
    return _file_json(run_dir(run_id) / "events.json") or []


def event(run_id: str, index: int) -> dict | None:
    """第 index 条过程事件（提示词按需单取，不解析整份时间线）。"""
    if db.is_primary(DOMAIN):
        got = repo_runs.event(run_id, index)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    evs = events(run_id)
    return evs[index] if 0 <= index < len(evs) else None


# ── 可信度检查 / 分组 / 诊断 ────────────────────────────────────────────────────────

def validation(run_id: str, loop) -> dict | None:
    if loop is None:
        return None
    if db.is_primary(DOMAIN):
        got = repo_runs.validation(run_id, int(loop))
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    p = run_dir(run_id) / "validation" / f"{loop}.json"
    return _file_json(p)


def validations(run_id: str) -> dict[str, dict]:
    """整目录的 validation json：`{轮次键: 内容}`。"""
    if db.is_primary(DOMAIN):
        loops = repo_runs.validation_loops(run_id)
        if loops or db.mode(DOMAIN) == "db":
            out = {}
            for loop in loops:
                got = repo_runs.validation(run_id, loop)
                if got is not None:
                    out[str(loop)] = got
            return out
    d = run_dir(run_id) / "validation"
    if not d.exists():
        return {}
    return {p.stem: _file_json(p) for p in sorted(d.glob("*.json"))}


def save_validation(run_id: str, loop, obj: dict) -> None:
    db.write_db(DOMAIN, lambda: repo_runs.save_validation(run_id, int(loop), obj), "runstore.validation")


def quantiles(run_id: str) -> dict[str, dict]:
    """整目录的 quantile json：`{轮次键: 内容}`。"""
    if db.is_primary(DOMAIN):
        got = repo_runs.quantiles(run_id)
        if got or db.mode(DOMAIN) == "db":
            return got
    d = run_dir(run_id) / "quantile"
    if not d.exists():
        return {}
    return {p.stem: _file_json(p) for p in sorted(d.glob("*.json")) if p.exists()}


def quantile(run_id: str, key: str) -> dict | None:
    if db.is_primary(DOMAIN):
        got = repo_runs.quantile(run_id, key)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    return _file_json(run_dir(run_id) / "quantile" / f"{key}.json")


def save_quantile(run_id: str, key: str, obj: dict) -> None:
    db.write_db(DOMAIN, lambda: repo_runs.save_quantile(run_id, key, obj), "runstore.quantile")


def diagnostic(run_id: str, key) -> dict | None:
    """诊断明细。`key` 是轮次号（`0`/`1`）或 `baseline`。"""
    if key is None:
        return None
    if db.is_primary(DOMAIN):
        got = repo_runs.diagnostic(run_id, key)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    return _file_json(run_dir(run_id) / "diagnostics" / f"{key}.json")


def diagnostics(run_id: str) -> dict[str, dict]:
    if db.is_primary(DOMAIN):
        got = repo_runs.diagnostics(run_id)
        if got or db.mode(DOMAIN) == "db":
            return got
    d = run_dir(run_id) / "diagnostics"
    if not d.exists():
        return {}
    return {p.stem: _file_json(p) for p in sorted(d.glob("*.json"))}


def save_diagnostic(run_id: str, key, obj: dict) -> None:
    db.write_db(DOMAIN, lambda: repo_runs.save_diagnostic(run_id, key, obj), "runstore.diagnostic")


# ── 运行报告 ────────────────────────────────────────────────────────────────────────

def report(run_id: str) -> str | None:
    """report.md 正文；没有则 None。"""
    if db.is_primary(DOMAIN):
        got = repo_runs.report(run_id)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    p = run_dir(run_id) / "report.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def save_report(run_id: str, markdown: str) -> None:
    db.write_db(DOMAIN, lambda: repo_runs.save_report(run_id, markdown), "runstore.report")


# ── 逐日 parquet（留在文件侧，但由本模块独占访问）────────────────────────────────────

def backtest_keys(run_id: str) -> list[str]:
    return _keys(run_id, "backtest")


def backtest(run_id: str, key: str) -> pd.DataFrame:
    return _parquet(run_id, "backtest", key)


def signal_keys(run_id: str) -> list[str]:
    return _keys(run_id, "signal")


def signal(run_id: str, key: str) -> pd.DataFrame:
    return _parquet(run_id, "signal", key)


def score(run_id: str, key: str) -> pd.DataFrame:
    return _parquet(run_id, "score", key)


def positions(run_id: str, key: str) -> pd.DataFrame:
    return _parquet(run_id, "positions", key)


def has_positions(run_id: str, key: str) -> bool:
    return (run_dir(run_id) / "positions" / f"{key}.parquet").exists()


def has_signal(run_id: str, key: str) -> bool:
    return (run_dir(run_id) / "signal" / f"{key}.parquet").exists()


def has_score(run_id: str, key: str) -> bool:
    return (run_dir(run_id) / "score" / f"{key}.parquet").exists()


def _keys(run_id: str, kind: str) -> list[str]:
    d = run_dir(run_id) / kind
    if not d.exists():
        return []
    return [p.stem for p in sorted(d.glob("*.parquet"))]


def mtime(run_id: str, kind: str, key: str) -> float:
    """产物文件的修改时间；不存在返回 0。给调用方做"文件变了就重算"的缓存键。"""
    p = run_dir(run_id) / kind / f"{key}.parquet"
    return p.stat().st_mtime if p.exists() else 0.0


def _parquet(run_id: str, kind: str, key: str) -> pd.DataFrame:
    _warn_parquet_once()
    p = run_dir(run_id) / kind / f"{key}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def _file_json(path: Path):
    if not path.exists():
        return None
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
