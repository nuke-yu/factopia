"""研究规格版本的数据库实现（对应 `runtime/research/<id>/v<k>.yaml` 与 `terminated.json`）。

两条保真要求，和文件时代必须完全一致：

1. **版本只新建、不修改**。文件用 `open("x")` 独占创建，这里用 `(research_id, version)` 主键 +
   `MAX(version)+1` 保证同样的语义；并发撞号时重试，绝不覆盖已有版本。
2. **规格整体进出**。`spec` 原样存 JSONB，不拆字段——研究规格里有 `ai_draft`、`data_needs`、
   `signal_source` 等扩展键，拆开就再也拼不回"当时提交的是什么"。
   另存一份规范化 JSON 的 sha256，用于校验冻结件与当前版本是否一致。
"""
from __future__ import annotations

import hashlib
import json
import logging

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, iso, json_safe

log = logging.getLogger(__name__)

MAX_VERSION_RETRY = 3


def now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_sha256(spec: dict) -> str:
    """规范化 JSON 的 sha256：键排序、无多余空白、保留非 ASCII。

    与文档里"冻结件指纹"的算法保持一致，迁移前后能对得上。
    """
    payload = json.dumps(
        json_safe(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 版本 ─────────────────────────────────────────────────────

def versions(research_id: str) -> list[int]:
    rows = db.query(
        "SELECT version FROM factopia.research_version WHERE research_id = %s ORDER BY version",
        (research_id,),
    )
    return [int(r[0]) for r in rows]


def load_version(research_id: str, version: int) -> dict:
    row = db.query_one(
        "SELECT spec FROM factopia.research_version WHERE research_id = %s AND version = %s",
        (research_id, int(version)),
    )
    if row is None:
        # 与文件版 FileNotFoundError 一致，调用方不必区分存储
        raise FileNotFoundError(f"研究版本不存在: {research_id} v{version}")
    return row[0]


def save_version(spec: dict, version: int | None = None) -> dict:
    """写入一个版本，返回**数据库里的实际内容**（带 version / locked_at）。

    `version` 显式给定时用于迁移回填：同一版本已存在则保持原样（ON CONFLICT DO NOTHING），
    这样迁移工具可以反复执行而不会把 v1 变成 v2。
    `version` 为空时取 `MAX(version)+1`，并发撞号重试。
    """
    research_id = str(spec["id"])
    at = spec.get("locked_at") or now()
    at_dt = as_dt(at) or None
    digest = canonical_sha256(spec)

    last_error: Exception | None = None
    for attempt in range(MAX_VERSION_RETRY):
        try:
            with db.tx() as conn:
                if version is None:
                    target = conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM factopia.research_version "
                        "WHERE research_id = %s",
                        (research_id,),
                    ).fetchone()[0]
                    conflict = ""
                else:
                    target = int(version)
                    conflict = "ON CONFLICT (research_id, version) DO NOTHING"

                # 主表必须先写：research_version 上有指向 research 的外键（ON DELETE RESTRICT）
                conn.execute(
                    """
                    INSERT INTO factopia.research (research_id, title_zh, markets, latest_version)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (research_id) DO UPDATE SET
                        title_zh = EXCLUDED.title_zh,
                        markets = EXCLUDED.markets,
                        latest_version = GREATEST(
                            COALESCE(factopia.research.latest_version, 0), EXCLUDED.latest_version
                        ),
                        updated_at = now()
                    """,
                    (
                        research_id,
                        str(spec.get("title_zh") or spec.get("title") or research_id),
                        list(spec.get("markets") or []),
                        target,
                    ),
                )

                stored = {**spec, "version": target, "locked_at": at}
                conn.execute(
                    f"""
                    INSERT INTO factopia.research_version
                        (research_id, version, spec, sha256, locked_at)
                    VALUES (%s, %s, %s, %s, %s)
                    {conflict}
                    """,
                    (research_id, target, Jsonb(json_safe(stored)), digest, at_dt),
                )

                # 以库里实际存的内容为准返回（显式版本号时可能与入参不同）
                row = conn.execute(
                    "SELECT spec, version FROM factopia.research_version "
                    "WHERE research_id = %s AND version = %s",
                    (research_id, target),
                ).fetchone()
            return row[0]
        except UniqueViolation as exc:  # 并发撞号：换下一个号重试
            last_error = exc
            log.warning("%s: 版本号 %s 撞号，重试第 %s 次", research_id, version, attempt + 1)
            version = None
    raise RuntimeError(f"研究 {research_id} 版本号分配失败") from last_error


def research_ids() -> list[str]:
    rows = db.query(
        """
        SELECT r.research_id
        FROM factopia.research r
        WHERE EXISTS (SELECT 1 FROM factopia.research_version v WHERE v.research_id = r.research_id)
        ORDER BY r.research_id
        """
    )
    return [r[0] for r in rows]


# ── 人工终止 ─────────────────────────────────────────────────

def termination(research_id: str | None) -> dict | None:
    if not research_id:
        return None
    row = db.query_one(
        "SELECT terminated FROM factopia.research WHERE research_id = %s", (research_id,)
    )
    return row[0] if row and row[0] else None


def terminate(
    research_id: str,
    version: int | None = None,
    reason: str = "人工终止当前研究",
    by: str = "研究员（演示，未登录）",
) -> dict:
    """写入终止标记；重复调用返回第一次的记录（幂等），与文件版一致。"""
    existing = termination(research_id)
    if existing:
        return existing
    rec = {
        "research_id": research_id,
        "version": version,
        "at": now(),
        "by": by,
        "reason": str(reason)[:1000],
    }
    with db.tx() as conn:
        updated = conn.execute(
            "UPDATE factopia.research SET terminated = %s, updated_at = now() "
            "WHERE research_id = %s AND terminated IS NULL",
            (Jsonb(rec), research_id),
        ).rowcount
        if not updated:
            # 主表还没有这行（库里只有版本）时补一条，保证终止标记不丢
            conn.execute(
                """
                INSERT INTO factopia.research (research_id, title_zh, markets, latest_version, terminated)
                VALUES (%s, %s, '{}', %s, %s)
                ON CONFLICT (research_id) DO UPDATE SET
                    terminated = COALESCE(factopia.research.terminated, EXCLUDED.terminated),
                    updated_at = now()
                """,
                (research_id, research_id, version, Jsonb(rec)),
            )
    return termination(research_id) or rec


def version_meta(research_id: str, version: int) -> dict | None:
    """版本指纹与锁定时间：审计与一致性校验用。"""
    row = db.query_one(
        "SELECT sha256, locked_at FROM factopia.research_version "
        "WHERE research_id = %s AND version = %s",
        (research_id, int(version)),
    )
    if row is None:
        return None
    return {"sha256": row[0], "locked_at": iso(row[1])}


def explain(research_id: str, version: int) -> dict | None:
    """AI 市场差异解释（原 explain_v<k>.json）。未生成过则为 None。"""
    row = db.query_one(
        "SELECT explain FROM factopia.research_version WHERE research_id = %s AND version = %s",
        (research_id, int(version)))
    if row is None or row[0] is None:
        return None
    return json_safe(row[0])


def save_explain(research_id: str, version: int, obj: dict) -> None:
    """写解释存档。版本行必须已存在——解释是"某个已锁定版本"的附属物，不能凭空产生。"""
    db.execute(
        "UPDATE factopia.research_version SET explain = %s WHERE research_id = %s AND version = %s",
        (Jsonb(json_safe(obj)), research_id, int(version)))
