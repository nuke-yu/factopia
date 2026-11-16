"""研究线索池的数据库实现（对应 `runtime/ideas/<id>/` 与 `ideas/sources.json`）。

三处与文件时代实质不同，都是为了消除竞态：

1. **去重索引从 json 改成唯一键**。`sources.json` 是"整份读出 → 判断 → 整份写回"，
   两个请求同时回流同一条监控预警时都会判定"不存在"，然后各建一张线索。
   现在 `idea_source.dedupe_key` 是主键，`INSERT … ON CONFLICT DO NOTHING` 让并发的
   第二个请求必然落空，再读回先到者的线索 —— 同一个结论只会有一张卡。
2. **线索卡与创建留痕同一事务**。文件版先 `_save(idea.json)` 再 `_append(events.jsonl)`，
   中间崩溃会留下"线索在、没有创建记录"，哈希链上看不出它从哪来。
3. **文档元数据拆表**。`idea_document` 让"这条线索引用了哪份 PDF、sha256 是多少、
   哪些线索用了同一份文献"变成一次查询；文件本体与抽取段落仍留在 `files/`（不可变归档字节）。

`source_ref` 在文件里是 `{research, run, note}` 字典。表里留了 `source_ref text` 列，
存的是其中的 research 引用（`rid.v3`），供"这个研究版本回过哪些线索"直接查；
完整字典仍在 payload 里，读回时以 payload 为准。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import as_dt, chain_hash, iso, json_safe

log = logging.getLogger(__name__)

DOMAIN = "ideas"

# 有独立列的字段；其余进 payload
_IDEA_COLUMNS = {
    "id", "channel", "title", "body", "language", "markets", "submitted_by", "role",
    "status", "pii_checked", "links", "fields", "similar", "created_at", "source_ref",
    "converted",
}


class DuplicateIdea(Exception):
    """并发回流撞上同一条线索：`idea_id` 是先到者。"""

    def __init__(self, idea_id: str):
        super().__init__(idea_id)
        self.idea_id = idea_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _split(idea: dict) -> dict:
    ref = idea.get("source_ref") or {}
    payload = {k: v for k, v in idea.items() if k not in _IDEA_COLUMNS}
    return {
        "idea_id": str(idea["id"]),
        "channel": str(idea["channel"]),
        "title": str(idea["title"]),
        "body": idea.get("body"),
        "language": idea.get("language"),
        "markets": list(idea.get("markets") or []),
        "submitted_by": idea.get("submitted_by"),
        "role": idea.get("role"),
        "source_ref": (ref.get("research") if isinstance(ref, dict) else None),
        "status": str(idea.get("status") or "new"),
        "pii_checked": bool(idea.get("pii_checked")),
        "links": Jsonb(json_safe(idea.get("links") or {})),
        "fields": Jsonb(json_safe(idea.get("fields") or {})),
        "similar_to": Jsonb(json_safe(idea["similar"])) if idea.get("similar") is not None else None,
        "converted": Jsonb(json_safe(idea["converted"])) if idea.get("converted") is not None else None,
        "created_at": as_dt(idea.get("created_at")) or as_dt(_now()),
        "payload": Jsonb(json_safe(payload)),
    }


_UPSERT_IDEA = """
INSERT INTO factopia.idea
    (idea_id, channel, title, body, language, markets, submitted_by, role, source_ref,
     status, pii_checked, links, fields, similar_to, converted, created_at, payload)
VALUES
    (%(idea_id)s, %(channel)s, %(title)s, %(body)s, %(language)s, %(markets)s,
     %(submitted_by)s, %(role)s, %(source_ref)s, %(status)s, %(pii_checked)s, %(links)s,
     %(fields)s, %(similar_to)s, %(converted)s, %(created_at)s, %(payload)s)
ON CONFLICT (idea_id) DO UPDATE SET
    channel = EXCLUDED.channel, title = EXCLUDED.title, body = EXCLUDED.body,
    language = EXCLUDED.language, markets = EXCLUDED.markets,
    submitted_by = EXCLUDED.submitted_by, role = EXCLUDED.role,
    source_ref = EXCLUDED.source_ref, status = EXCLUDED.status,
    pii_checked = EXCLUDED.pii_checked, links = EXCLUDED.links, fields = EXCLUDED.fields,
    similar_to = EXCLUDED.similar_to, converted = EXCLUDED.converted,
    payload = EXCLUDED.payload
"""


def _merge(row) -> dict:
    (idea_id, channel, title, body, language, markets, submitted_by, role, source_ref,
     status, pii_checked, links, fields, similar_to, converted, created_at, payload) = row
    payload = dict(payload or {})
    idea = {
        "id": idea_id, "channel": channel, "title": title, "body": body,
        "language": language, "markets": list(markets or []),
        "submitted_by": submitted_by, "role": role,
        "created_at": iso(created_at), "status": status,
        "links": links or {}, "pii_checked": bool(pii_checked),
        "fields": fields or {},
    }
    if "source_ref" in payload:
        idea["source_ref"] = payload.pop("source_ref")
    elif source_ref:
        idea["source_ref"] = {"research": source_ref}
    if similar_to is not None:
        idea["similar"] = similar_to
    if converted is not None:
        idea["converted"] = converted
    idea.update(payload)
    return idea


_COLS = ("idea_id, channel, title, body, language, markets, submitted_by, role, source_ref, "
         "status, pii_checked, links, fields, similar_to, converted, created_at, payload")


def _insert_idea(conn, idea: dict) -> None:
    conn.execute(_UPSERT_IDEA, _split(idea))
    doc = idea.get("document") or {}
    if doc.get("sha256"):
        conn.execute(
            """INSERT INTO factopia.idea_document
                   (idea_id, filename, mime, bytes, sha256, extracted_chars)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (idea_id, filename) DO UPDATE SET
                   mime = EXCLUDED.mime, bytes = EXCLUDED.bytes,
                   sha256 = EXCLUDED.sha256, extracted_chars = EXCLUDED.extracted_chars""",
            (str(idea["id"]), str(doc.get("name") or "document"), doc.get("mime"),
             doc.get("bytes"), doc["sha256"], doc.get("paragraphs")),
        )


def _append(conn, idea_id: str, ev: dict, *, exact: dict | None = None) -> dict:
    """线索留痕：每条线索一条独立链（与信号不同——信号是全局一条链）。"""
    conn.execute("SELECT 1 FROM factopia.idea WHERE idea_id = %s FOR UPDATE", (idea_id,))
    if exact is not None:
        body = exact
    else:
        row = conn.execute(
            "SELECT hash FROM factopia.idea_event WHERE idea_id = %s ORDER BY seq DESC LIMIT 1",
            (idea_id,)).fetchone()
        prev = row[0] if row else "0" * 64
        body = {**ev, "at": _now(), "prev": prev}
        body["hash"] = chain_hash(body)
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM factopia.idea_event WHERE idea_id = %s",
        (idea_id,)).fetchone()[0]
    extra = {k: v for k, v in body.items() if k not in ("action", "by", "note", "at", "prev", "hash")}
    conn.execute(
        """INSERT INTO factopia.idea_event
               (idea_id, seq, at, action, actor, note, payload, prev_hash, hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT DO NOTHING""",
        (idea_id, seq, as_dt(body.get("at")), str(body["action"]), str(body.get("by") or ""),
         body.get("note"), Jsonb(json_safe(extra)), body["prev"], body["hash"]),
    )
    return body


def save(idea: dict, event: dict | None = None, source_key: str | None = None,
         *, exact_event: dict | None = None) -> dict:
    """写入线索（可带一条留痕），`source_key` 给定时同时登记去重索引。

    去重索引的插入放在同一个事务里并靠主键判重：并发的第二个写入者会拿到
    `DuplicateIdea`，调用方读回先到者的线索即可，绝不会出现两张同源卡片。

    顺序是**先落线索、后占唯一键**：`idea_source.idea_id` 有外键，反过来写会直接违约；
    而占位失败时把异常抛出去，整个事务回滚，刚才那条线索也跟着消失——不会留下孤儿卡片。
    """
    with db.tx() as conn:
        _insert_idea(conn, idea)
        if source_key:
            taken = conn.execute(
                """INSERT INTO factopia.idea_source (dedupe_key, idea_id, channel)
                   VALUES (%s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING
                   RETURNING idea_id""",
                (source_key, str(idea["id"]), str(idea["channel"])),
            ).fetchone()
            if taken is None:
                row = conn.execute(
                    "SELECT idea_id FROM factopia.idea_source WHERE dedupe_key = %s",
                    (source_key,)).fetchone()
                raise DuplicateIdea(row[0] if row else "")
        if event is not None or exact_event is not None:
            _append(conn, str(idea["id"]), event, exact=exact_event)
    return idea


def save_full(idea: dict, events: list[dict], source_key: str | None = None) -> dict:
    """迁移专用：整条线索 + 全部历史事件，一个事务。

    `seq` 按文件里的顺序（第 i 条就是 i+1），`ON CONFLICT DO NOTHING` 兜住重跑；
    事件里的 at / prev / hash **原样写入**，重算等于改写历史。
    """
    with db.tx() as conn:
        _insert_idea(conn, idea)
        if source_key:
            conn.execute(
                """INSERT INTO factopia.idea_source (dedupe_key, idea_id, channel)
                   VALUES (%s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING""",
                (source_key, str(idea["id"]), str(idea.get("channel") or "")),
            )
        for i, ev in enumerate(events, start=1):
            conn.execute("SELECT 1 FROM factopia.idea WHERE idea_id = %s FOR UPDATE", (str(idea["id"]),))
            extra = {k: v for k, v in ev.items()
                     if k not in ("action", "by", "note", "at", "prev", "hash")}
            conn.execute(
                """INSERT INTO factopia.idea_event
                       (idea_id, seq, at, action, actor, note, payload, prev_hash, hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (str(idea["id"]), i, as_dt(ev.get("at")), str(ev["action"]),
                 str(ev.get("by") or ""), ev.get("note"), Jsonb(json_safe(extra)),
                 ev["prev"], ev["hash"]),
            )
    return idea


def load(idea_id: str) -> dict:
    row = db.query_one(f"SELECT {_COLS} FROM factopia.idea WHERE idea_id = %s", (idea_id,))
    if row is None:
        raise FileNotFoundError(idea_id)
    return _merge(row)


def all_ideas() -> list[dict]:
    rows = db.query(f"SELECT {_COLS} FROM factopia.idea ORDER BY created_at DESC, idea_id DESC")
    return [_merge(r) for r in rows]


def events(idea_id: str) -> list[dict]:
    rows = db.query(
        """SELECT seq, at, action, actor, note, payload, prev_hash, hash
           FROM factopia.idea_event WHERE idea_id = %s ORDER BY seq""", (idea_id,))
    out = []
    for _seq, at, action, actor, note, payload, prev_hash, digest in rows:
        ev = {"action": action, "by": actor, "at": iso(at), "prev": prev_hash, "hash": digest}
        if note is not None:
            ev["note"] = note
        ev.update(payload or {})
        out.append(ev)
    return out


def append_event(idea_id: str, ev: dict) -> dict:
    with db.tx() as conn:
        return _append(conn, idea_id, ev)


def verify_chain(idea_id: str) -> dict:
    prev = "0" * 64
    evs = events(idea_id)
    for i, ev in enumerate(evs):
        if ev["prev"] != prev or chain_hash(ev) != ev["hash"]:
            return {"ok": False, "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(evs)}


# ── 去重索引 ─────────────────────────────────────────────────

def sources() -> dict[str, str]:
    return {k: v for k, v in db.query("SELECT dedupe_key, idea_id FROM factopia.idea_source")}


def source_idea(dedupe_key: str) -> str | None:
    row = db.query_one("SELECT idea_id FROM factopia.idea_source WHERE dedupe_key = %s", (dedupe_key,))
    return row[0] if row else None


def upsert_sources(idx: dict[str, str]) -> None:
    """把内存里的索引整份落库（迁移与 `sync()` 用）。已存在的不覆盖，保持"先到者为准"。"""
    if not idx:
        return
    with db.tx() as conn:
        for key, idea_id in idx.items():
            conn.execute(
                """INSERT INTO factopia.idea_source (dedupe_key, idea_id, channel)
                   SELECT %s, %s, channel FROM factopia.idea WHERE idea_id = %s
                   ON CONFLICT (dedupe_key) DO NOTHING""",
                (key, idea_id, idea_id),
            )


def by_source_ref(ref: str) -> list[dict]:
    """某个研究版本来源、仍在处理或已转立项的线索。"""
    rows = db.query(
        f"""SELECT {_COLS} FROM factopia.idea
            WHERE source_ref = %s AND status IN ('new','converted')
            ORDER BY created_at DESC, idea_id DESC""",
        (ref,),
    )
    return [_merge(r) for r in rows]
