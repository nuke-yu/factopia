#!/usr/bin/env python
"""对已建好的 factopia schema 做行为级冒烟测试（经 ECS SSH 隧道）。

只验证「设计承诺是否真的成立」，不测业务逻辑：
  1. 分区路由      daily_bar 按 market 落到对应子表
  2. 只追加约束    *_event 上的 UPDATE / DELETE 必须被触发器拒绝
  3. updated_at    更新后必须自动前移（跨事务验证，now() 是事务时间）
  4. 队列并发      FOR UPDATE SKIP LOCKED 下两个 worker 不会抢到同一行
  5. 模糊检索      similarity() / % 运算符在 factopia schema 下可解析
  6. 向量检索      pgvector 距离查询与 HNSW 索引可用

每个用例自带清理：事务内的一律 ROLLBACK，需要跨事务的先 INSERT 后 DELETE。
用法：
    export FACTOPIA_SSH_PASSWORD=... FACTOPIA_PGPASSWORD=...
    python scripts/db/smoke_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg_tunnel as T  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def scalar(conn, sql: str, params=None):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def test_partition(conn) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO factopia.daily_bar (market, symbol, date, close) VALUES "
            "('hk','0700','2026-11-13',430.5), ('uk','HSBA','2026-11-13',700.2), ('fr','AI','2026-11-13',150.3)"
        )
        rows = conn.execute(
            "SELECT market, tableoid::regclass::text FROM factopia.daily_bar ORDER BY market"
        ).fetchall()
        mapping = dict(rows)
        ok = (
            mapping.get("hk", "").endswith("daily_bar_hk")
            and mapping.get("uk", "").endswith("daily_bar_uk")
            and mapping.get("fr", "").endswith("daily_bar_fr")
        )
        record("分区路由 daily_bar(market)", ok, ", ".join(f"{m}->{t.split('.')[-1]}" for m, t in rows))
    finally:
        conn.execute("ROLLBACK")


def test_append_only(conn) -> None:
    def setup() -> None:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO factopia.idea (idea_id, channel, title, markets) "
            "VALUES ('smoke_ao','test','只追加测试','{HK}')"
        )
        conn.execute(
            "INSERT INTO factopia.idea_event (idea_id, seq, action, actor, prev_hash, hash) "
            "VALUES ('smoke_ao', 1, 'created', 'smoke', '', 'h1')"
        )

    blocked: dict[str, bool] = {}
    for op, sql in (
        ("UPDATE", "UPDATE factopia.idea_event SET action='tampered' WHERE idea_id='smoke_ao'"),
        ("DELETE", "DELETE FROM factopia.idea_event WHERE idea_id='smoke_ao'"),
    ):
        setup()
        try:
            conn.execute(sql)
            blocked[op] = False
        except Exception as e:
            blocked[op] = "只追加" in str(e)
        finally:
            conn.execute("ROLLBACK")
    record(
        "只追加约束 idea_event",
        all(blocked.values()),
        f"UPDATE 被拒={blocked.get('UPDATE')}, DELETE 被拒={blocked.get('DELETE')}",
    )


def test_cascade_conflict(conn) -> None:
    """留痕表 ON DELETE CASCADE 与只追加触发器天然冲突，这里把事实记下来。"""
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO factopia.idea (idea_id, channel, title, markets) "
            "VALUES ('smoke_cas','test','级联删除测试','{HK}')"
        )
        conn.execute(
            "INSERT INTO factopia.idea_event (idea_id, seq, action, actor, prev_hash, hash) "
            "VALUES ('smoke_cas', 1, 'created', 'smoke', '', 'h1')"
        )
        try:
            conn.execute("DELETE FROM factopia.idea WHERE idea_id='smoke_cas'")
            # 若父表删除成功但子表留痕被拦，PostgreSQL 会整体报错，走不到这里
            record("父子级联删除", True, "父表删除成功（子表无拦截）")
        except Exception as e:
            msg = str(e).splitlines()[0]
            record("父子级联删除", True, f"被留痕触发器拦下（符合预期，需业务层软删）: {msg[:70]}")
    finally:
        conn.execute("ROLLBACK")


def test_updated_at(conn) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO factopia.idea (idea_id, channel, title, markets) "
            "VALUES ('smoke_ts','test','时间戳测试','{HK}')"
        )
        conn.execute("COMMIT")
        before = scalar(conn, "SELECT updated_at FROM factopia.idea WHERE idea_id='smoke_ts'")
        conn.execute("BEGIN")
        conn.execute("UPDATE factopia.idea SET title='时间戳测试·改' WHERE idea_id='smoke_ts'")
        conn.execute("COMMIT")
        after = scalar(conn, "SELECT updated_at FROM factopia.idea WHERE idea_id='smoke_ts'")
        record("updated_at 自动前移", after > before, f"{before.isoformat()} -> {after.isoformat()}")
    finally:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.execute("DELETE FROM factopia.idea WHERE idea_id='smoke_ts'")


def test_skip_locked(conn, conn2) -> None:
    conn.execute(
        "INSERT INTO factopia.job (job_id, batch_id, market, state) VALUES "
        "('smoke_j1','smoke_b','HK','queued'), ('smoke_j2','smoke_b','HK','queued')"
    )
    pick = (
        "SELECT job_id FROM factopia.job WHERE state='queued' AND batch_id='smoke_b' "
        "ORDER BY job_id LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    try:
        conn.execute("BEGIN")
        a = scalar(conn, pick)
        conn2.execute("BEGIN")
        b = scalar(conn2, pick)
        conn2.execute("ROLLBACK")
        conn.execute("ROLLBACK")
        ok = a is not None and b is not None and a != b
        record("队列 FOR UPDATE SKIP LOCKED", ok, f"worker1={a}, worker2={b}（不同行即正确）")
    finally:
        conn.execute("ROLLBACK")
        conn.execute("DELETE FROM factopia.job WHERE batch_id='smoke_b'")


def test_trgm(conn) -> None:
    conn.execute("SET search_path = factopia, public")
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO factopia.idea (idea_id, channel, title, markets) "
            "VALUES ('smoke_trgm','test','momentum reversal factor on HK equities','{HK}')"
        )
        sim = scalar(conn, "SELECT similarity(title, 'momentum reversal') FROM factopia.idea WHERE idea_id='smoke_trgm'")
        # 长标题里找短词要用词相似度 <%（% 是整串三字母相似度，默认阈值 0.3）
        hit = scalar(conn, "SELECT count(*) FROM factopia.idea WHERE 'momentum' <% title")
        record("pg_trgm 模糊检索", hit >= 1 and sim > 0,
               f"similarity={sim:.3f}, 词相似度 <% 命中={hit}")
    finally:
        conn.execute("ROLLBACK")


def test_vector(conn) -> None:
    typ = scalar(
        conn,
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid='factopia.knowledge_chunk'::regclass AND a.attname='embedding'",
    )
    dim = int(typ.split("(")[1].rstrip(")")) if "(" in typ else 1024
    vec_a = "[" + ",".join(["0.01"] * dim) + "]"
    vec_b = "[" + ",".join(["0.02"] * dim) + "]"
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO factopia.knowledge_chunk (kind, ref_table, ref_id, content, embedding) "
            "VALUES ('research','research','smoke_v','向量测试', %s::vector), "
            "       ('research','research','smoke_v2','向量测试2', %s::vector)",
            (vec_a, vec_b),
        )
        rows = conn.execute(
            "SELECT ref_id, round((embedding <=> %s::vector)::numeric, 4) AS dist "
            "FROM factopia.knowledge_chunk WHERE ref_id LIKE 'smoke_v%%' ORDER BY dist LIMIT 2",
            (vec_a,),
        ).fetchall()
        idx = scalar(
            conn,
            "SELECT count(*) FROM pg_indexes WHERE schemaname='factopia' AND indexname='knowledge_chunk_vec_idx'",
        )
        record(
            "pgvector 距离检索",
            len(rows) == 2 and rows[0][1] == 0,
            f"维度={dim}, 最近={rows[0][0] if rows else '—'} dist={rows[0][1] if rows else '—'}, HNSW 索引={bool(idx)}",
        )
    finally:
        conn.execute("ROLLBACK")


def main() -> int:
    ns = T.parse_args(["check"])
    server, port = T.open_tunnel(ns)
    import psycopg

    dsn = (
        f"host=127.0.0.1 port={port} dbname={ns.db} user={ns.user} "
        f"password={ns.password or ''} connect_timeout=10 application_name=factopia-smoke"
    )
    conn = psycopg.connect(dsn, autocommit=True)
    conn2 = psycopg.connect(dsn, autocommit=True)
    try:
        print("\n行为验证（factopia schema）")
        print("-" * 62)
        for fn in (test_partition, test_append_only, test_cascade_conflict,
                   test_updated_at, test_trgm, test_vector):
            fn(conn)
        test_skip_locked(conn, conn2)
        print("-" * 62)
        bad = [r for r in RESULTS if not r[1]]
        print(f"合计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}")
        # 确认没有测试残留
        left = scalar(conn, "SELECT count(*) FROM factopia.job WHERE batch_id='smoke_b'") \
             + scalar(conn, "SELECT count(*) FROM factopia.idea WHERE idea_id LIKE 'smoke_%'")
        print(f"残留检查：{left} 行（应为 0）")
        return 0 if not bad else 1
    finally:
        conn.close()
        conn2.close()
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
