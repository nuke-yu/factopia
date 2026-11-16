"""零散小对象的落点：单例键值（app_state）与生成的 HTML 报告（report_file）。

这两类东西的共同点是**没有按行查询的需求**——一次取一整份。因此不做规范化：

* `app_state`：refdata 摘要、pit_status、数据质量报告、vendor 导入记录。
  它们本来就是"一份快照"，写成 `(key, value jsonb)` 比建四张单行表更诚实。
* `report_file`：单文件 HTML 研究报告。正文几万字，放进 `text` 列，
  `/api/reports/<name>` 直接取列返回，磁盘上那份只是缓存。

`reference_data`（证券主数据那批 parquet）不在这个模块：那些是真正的表数据，
对应 `security_master` / `index_membership` / `corporate_action`，见 `factopia/repo/refdata.py`。
"""
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, iso, json_safe

DOMAIN = "app"

# app_state 的键名集中在这里，避免各处手写字符串拼错
KEY_PIT = "results.pit_status"
KEY_QUALITY = "data.quality_report"
KEY_REFDATA = "refdata.{name}"           # refdata.master_summary / refdata.actions_summary
KEY_VENDOR_IMPORTS = "refdata.vendor_imports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 单例键值 ────────────────────────────────────────────────────────────────────────

def state(key: str):
    row = db.query_one("SELECT value FROM factopia.app_state WHERE key = %s", (str(key),))
    return json_safe(row[0]) if row else None


def save_state(key: str, value) -> None:
    db.execute(
        """INSERT INTO factopia.app_state (key, value, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (str(key), Jsonb(json_safe(value))))


def drop_state(key: str) -> None:
    db.execute("DELETE FROM factopia.app_state WHERE key = %s", (str(key),))


def states(prefix: str) -> dict:
    """按前缀取一批（`refdata.` 一次性取全部摘要）。"""
    return {k: json_safe(v) for k, v in db.query(
        "SELECT key, value FROM factopia.app_state WHERE key LIKE %s ORDER BY key",
        (str(prefix) + "%",))}


# ── 生成的 HTML 报告 ────────────────────────────────────────────────────────────────

def save_report_file(name: str, html: str, research_id: str | None = None,
                     version: int | None = None) -> None:
    db.execute(
        """INSERT INTO factopia.report_file (name, research_id, version, html, bytes)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET
               html = EXCLUDED.html, bytes = EXCLUDED.bytes, created_at = now()""",
        (str(name), research_id, None if version is None else int(version),
         html, len(html.encode("utf-8"))))


def report_file(name: str) -> str | None:
    row = db.query_one("SELECT html FROM factopia.report_file WHERE name = %s", (str(name),))
    return row[0] if row else None


def has_report_file(name: str) -> bool:
    return db.query_one("SELECT 1 FROM factopia.report_file WHERE name = %s", (str(name),)) is not None


def report_files(research_id: str, version: int) -> list[dict]:
    """某版本下已生成的报告清单，新的在前（与原 `sorted(glob, reverse=True)` 一致）。"""
    return [{"name": n, "bytes": int(b or 0), "created_at": iso(t)}
            for n, b, t in db.query(
                """SELECT name, bytes, created_at FROM factopia.report_file
                    WHERE research_id = %s AND version = %s ORDER BY name DESC""",
                (str(research_id), int(version)))]


def all_report_names() -> list[str]:
    """全部报告名（体检页只数份数，不需要正文）。"""
    return [r[0] for r in db.query(
        "SELECT name FROM factopia.report_file ORDER BY name DESC")]
