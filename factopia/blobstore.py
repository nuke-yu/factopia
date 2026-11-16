"""运行时"零散产物"的唯一入口：单例快照、HTML 报告、尚未入库的派生表、静态配置。

这个模块存在的唯一理由是**别让 API 自己拼路径**。`/api/*` 里再有
`read_text` / `read_parquet` / `json.load`，就意味着"数据在哪"这件事散落在
几十个端点上——切 `FACTOPIA_STORAGE=db` 时没人能保证清干净，页面就会静默读到
上一次运行留下的旧文件。

按"有没有第二份真相"分三类，处理方式不同：

**A. 已有表，必须读库**（否则就是读到旧的）
    pit_status          → app_state['results.pit_status']
    quality_report      → app_state['data.quality_report']
    refdata 摘要        → app_state['refdata.*']
    vendor 导入记录     → app_state['refdata.vendor_imports']
    生成的 HTML 报告    → report_file

**B. 还没有表，暂时读文件**（只有一份真相，不存在"读到旧的"）
    refdata/*.parquet（securities / relations / corporate_actions）—— 已入库，走 A 类
    text 数据集的 filings.parquet

**C. 静态配置，永远读文件**（跟着代码走，不是运行时数据）
    configs/research/*.yaml、configs/rdagent/context/*.md、configs/refdata/*.yaml
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pandas as pd
import yaml

from factopia import db
from factopia.repo import misc as repo_misc

log = logging.getLogger(__name__)

DOMAIN = "app"

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(RUNTIME / "results")))
REFDATA = RUNTIME / "refdata"
REPORTS = RUNTIME / "reports"
STANDARDIZED = RUNTIME / "data" / "standardized"
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))


# ── A. 单例快照 ─────────────────────────────────────────────────────────────────────

def _snapshot(key: str, path: Path):
    # dual 也要读库：快照的写端只写 app_state（save_pit/save_quality 不落文件），
    # 若 dual 还去读文件，刚写的快照就读不回来。
    m = db.mode(DOMAIN)
    if m in ("dual", "db"):
        got = repo_misc.state(key)
        if got is not None or m == "db":
            return got
    return _read_json(path)


def _save_snapshot(key: str, obj) -> None:
    # dual 也要写：灰度期读是"库优先"，只写文件的话库里永远没有这份快照
    db.write_db(DOMAIN, lambda: repo_misc.save_state(key, obj), f"app_state:{key}")


def pit() -> dict | None:
    """各数据域的时间点一致性状态（所有结果页面据此标注，BR-18）。"""
    return _snapshot(repo_misc.KEY_PIT, RESULTS / "pit_status.json")


def save_pit(obj: dict) -> None:
    _save_snapshot(repo_misc.KEY_PIT, obj)


def overview() -> dict | None:
    """总览页的汇总（`results/overview.json`，由 factopia.export 生成）。"""
    return _read_json(RESULTS / "overview.json")


def quality() -> dict | None:
    """标准化流水线的数据质量报告（含数据指纹）。"""
    return _snapshot(repo_misc.KEY_QUALITY, STANDARDIZED / "quality_report.json")


def save_quality(obj: dict) -> None:
    _save_snapshot(repo_misc.KEY_QUALITY, obj)


def refdata_summary(name: str) -> dict | None:
    """参考数据摘要（master_summary / actions_summary）。"""
    return _snapshot(repo_misc.KEY_REFDATA.format(name=name), REFDATA / f"{name}.json")


def save_refdata_summary(name: str, obj: dict) -> None:
    _save_snapshot(repo_misc.KEY_REFDATA.format(name=name), obj)


def vendor_imports() -> list[dict]:
    """数据商导入记录（第 14.21 节）。文件是 JSONL，入库后是一个数组。"""
    if db.is_primary(DOMAIN):
        got = repo_misc.state(repo_misc.KEY_VENDOR_IMPORTS)
        if got is not None or db.mode(DOMAIN) == "db":
            return got if isinstance(got, list) else []
    p = REFDATA / "vendor_imports.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def save_vendor_imports(rows: list[dict]) -> None:
    _save_snapshot(repo_misc.KEY_VENDOR_IMPORTS, rows)


# ── A. 生成的 HTML 报告 ─────────────────────────────────────────────────────────────

def report(name: str) -> str | None:
    if db.is_primary(DOMAIN):
        got = repo_misc.report_file(name)
        if got is not None or db.mode(DOMAIN) == "db":
            return got
    p = REPORTS / name
    return p.read_text(encoding="utf-8") if p.exists() else None


def has_report(name: str) -> bool:
    if db.is_primary(DOMAIN):
        if repo_misc.has_report_file(name):
            return True
        return db.mode(DOMAIN) == "dual" and (REPORTS / name).exists()
    return (REPORTS / name).exists()


def save_report(name: str, html: str, research_id: str | None = None,
                version: int | None = None) -> None:
    """写报告。files 落盘、dual 两边都写、db 只入列。"""
    if db.mode(DOMAIN) in ("files", "dual"):
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / name).write_text(html, encoding="utf-8")
    db.write_db(DOMAIN, lambda: repo_misc.save_report_file(name, html, research_id, version),
                f"report_file:{name}")


def reports(research_id: str, version: int) -> list[dict]:
    if db.is_primary(DOMAIN):
        got = repo_misc.report_files(research_id, version)
        if got or db.mode(DOMAIN) == "db":
            return got
    if not REPORTS.exists():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size}
            for p in sorted(REPORTS.glob(f"{research_id}_v{version}_*.html"), reverse=True)]


# ── B. 尚未入库的派生表（只有一份真相，读文件是对的）──────────────────────────────────

def alpha_surface(horizon: int) -> dict | None:
    """Alpha158 因子评估面（`results/alpha158/h<horizon>/surface.json`，由 factopia.export 生成）。"""
    return _read_json(RESULTS / "alpha158" / f"h{int(horizon)}" / "surface.json")


def alpha_cube(horizon: int) -> dict | None:
    """Alpha158 因子评估立方（`results/alpha158/h<horizon>/cube.json`）。"""
    return _read_json(RESULTS / "alpha158" / f"h{int(horizon)}" / "cube.json")


def refdata_table(name: str) -> pd.DataFrame:
    """参考数据表：`securities` / `relations` / `corporate_actions`。

    P3 已把这三份 parquet 接进 `security_master` / `security_relation` / `corporate_action`，
    所以这里按 `refdata` 域的模式分派：`db` 读表且**不回落** parquet；`dual` 仍读 parquet。

    为什么 `dual` 不读库：dual 阶段的唯一真相还是文件，库里可能漏写。
    读库优先会把"某次写入没同步"变成"页面显示旧数据"——比读到文件旧更难查。
    读库的正确性由 `db` 模式验证（`scripts/db/smoke_refdata.py`），dual 只验证写。
    """
    from factopia.repo import refdata as repo_refdata
    loaders = {"securities": repo_refdata.load_securities,
               "relations": repo_refdata.load_relations,
               "corporate_actions": repo_refdata.load_corporate_actions}
    if name not in loaders:
        raise KeyError(f"未知参考数据表：{name}")
    if db.is_primary(repo_refdata.DOMAIN):
        got = loaders[name]()
        if len(got) or db.mode(repo_refdata.DOMAIN) == "db":
            return got
    p = REFDATA / f"{name}.parquet"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_parquet(p)


def has_refdata(name: str) -> bool:
    from factopia.repo import refdata as repo_refdata
    counters = {"securities": repo_refdata.count_securities,
                "relations": repo_refdata.count_relations,
                "corporate_actions": repo_refdata.count_corporate_actions}
    if db.is_primary(repo_refdata.DOMAIN) and counters.get(name):
        if counters[name]():
            return True
        if db.mode(repo_refdata.DOMAIN) == "db":
            return False
    return (REFDATA / f"{name}.parquet").exists()


def text_filings(dataset: str) -> pd.DataFrame:
    """公告文本数据集（`runtime/data/text/<ds>/filings.parquet`）。"""
    from factopia.text import common as tcommon
    p = tcommon.ds_dir(dataset) / "filings.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def job_log_tail(job_id: str, lines: int = 40) -> list[str]:
    """RD-Agent 控制台输出末尾若干行（日志天然是文件，不产第二份真相）。"""
    from factopia.research import store as rstore
    p = rstore.log_path(job_id)
    return p.read_text(errors="replace").splitlines()[-lines:] if p.exists() else []


# ── C. 静态配置 ─────────────────────────────────────────────────────────────────────

def config_yaml(*parts: str) -> dict:
    return yaml.safe_load((CONFIGS.joinpath(*parts)).read_text(encoding="utf-8"))


def config_text(*parts: str) -> str:
    return CONFIGS.joinpath(*parts).read_text(encoding="utf-8")


def membership(market: str) -> pd.DataFrame:
    """指数成分股名单。原文件是 `configs/universe/membership/<market>.csv`，
    已接进 `index_membership`（见 `factopia/refdata/membership.py`），这里只做分派。"""
    from factopia.refdata import membership as mem
    got = mem.load(market)
    return got if got is not None else pd.DataFrame()


def config_examples(subdir: str) -> list[dict]:
    """某配置子目录下的全部 yaml（研究规格示例用）。"""
    out = []
    for p in sorted((CONFIGS / subdir).glob("*.yaml")):
        got = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(got, dict):
            out.append(got)
    return out


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def all_report_names() -> list[str]:
    if db.is_primary(DOMAIN):
        rows = repo_misc.all_report_names()
        if rows or db.mode(DOMAIN) == "db":
            return rows
    return sorted((p.name for p in REPORTS.glob("*.html")), reverse=True) if REPORTS.exists() else []


def text_json(dataset: str | None, *parts: str):
    """公告文本数据集里的 json（summary/truth/sensitivity/docs/labels）。

    text 域尚未入库（P4 才接 pgvector），读写都还在文件侧；这里只做"路径知识收口"，
    免得 API 各处自己拼 `ds_dir(...)/summary.json`。
    """
    from factopia.text import common as tcommon
    base = tcommon.ds_dir(dataset) if dataset else tcommon.TEXT
    return tcommon.read_json(base.joinpath(*parts))
