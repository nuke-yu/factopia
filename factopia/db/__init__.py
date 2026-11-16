"""数据库接入层：连接池、事务边界、按域灰度开关。

这一个模块负责回答两个问题，业务代码不需要关心别的：

1. **现在该走哪条路？** `mode(domain)` 返回 `files` / `dual` / `db`，由环境变量决定，
   支持按域灰度（任务队列先切，不影响其他域）。
2. **连接怎么来？** `pool()` 给连接池，`tx()` 给事务。DSN 里已经带好
   `search_path=factopia,public` 与 `statement_timeout`，避免每个调用点重复设置。

环境变量：

    FACTOPIA_STORAGE=files|dual|db         全局默认，缺省 files（迁移前的行为）
    FACTOPIA_STORAGE_JOBS=db               按域覆盖，域名为大写模块名
    PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD   连接信息
    FACTOPIA_DSN                           直接给完整 DSN，优先于上面的分项
    FACTOPIA_PG_POOL_MIN / FACTOPIA_PG_POOL_MAX          连接池大小（默认 1 / 5）
    FACTOPIA_PG_TIMEOUT_MS                 statement_timeout（默认 15000）

为什么连接池默认只开 5：比赛账号上限 30 条连接，api / runner / exporter 加起来
要留足够余量给 psql 排查，方案 4.5 定的峰值是 ≤15。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

MODES = ("files", "dual", "db")
DEFAULT_SCHEMA_PATH = "factopia,public"

_pool = None


# ── 灰度开关 ─────────────────────────────────────────────────

def mode(domain: str | None = None) -> str:
    """解析某个域的存储模式。`domain` 传模块名（jobs / research / approval …）。"""
    keys = [f"FACTOPIA_STORAGE_{domain.upper()}"] if domain else []
    keys.append("FACTOPIA_STORAGE")
    for key in keys:
        raw = (os.environ.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw not in MODES:
            raise ValueError(f"{key}={raw!r} 无效，只能是 {' / '.join(MODES)}")
        return raw
    return "files"


def is_primary(domain: str | None = None) -> bool:
    """读是否以数据库为准（`db` 模式）。"""
    return mode(domain) == "db"


def mirrors(domain: str | None = None) -> bool:
    """是否需要在写文件之后补写数据库（`dual` 模式）。"""
    return mode(domain) == "dual"


# ── 连接 ─────────────────────────────────────────────────────

def dsn() -> str:
    """组装 DSN。已经带好 search_path 与 statement_timeout。"""
    explicit = os.environ.get("FACTOPIA_DSN") or os.environ.get("DATABASE_URL")
    schema_path = os.environ.get("FACTOPIA_PG_SEARCH_PATH", DEFAULT_SCHEMA_PATH)
    timeout_ms = int(os.environ.get("FACTOPIA_PG_TIMEOUT_MS", "15000"))
    # TimeZone=UTC 与文件时代的 `now()`（UTC ISO）对齐，避免读回的时间带 +08:00
    options = (
        f"-c search_path={schema_path} -c statement_timeout={timeout_ms} -c TimeZone=UTC"
    )

    if explicit:
        # 已有 options 就追加，避免覆盖调用方自己的设置
        sep = "&" if "?" in explicit else "?"
        if "options=" in explicit:
            return explicit
        return f"{explicit}{sep}options={options.replace(' ', '%20')}"

    host = os.environ.get("PGHOST", "127.0.0.1")
    port = os.environ.get("PGPORT", "5432")
    db = os.environ.get("PGDATABASE", "team12")
    user = os.environ.get("PGUSER", "team12")
    password = os.environ.get("PGPASSWORD", "")
    return (
        f"host={host} port={port} dbname={db} user={user} password={password} "
        f"connect_timeout=10 application_name=factopia options='{options}'"
    )


def pool():
    """惰性创建连接池。导入 psycopg 失败时给出可执行的提示，而不是 ImportError 堆栈。"""
    global _pool
    if _pool is not None:
        return _pool
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - 依赖缺失时才走到
        raise RuntimeError(
            "缺少 psycopg：pip install 'psycopg[binary,pool]>=3.2'"
        ) from exc

    min_size = int(os.environ.get("FACTOPIA_PG_POOL_MIN", "1"))
    max_size = int(os.environ.get("FACTOPIA_PG_POOL_MAX", "5"))
    _pool = ConnectionPool(
        dsn(),
        min_size=min_size,
        max_size=max_size,
        # 自动提交：事务边界统一由 tx() 显式控制，不让驱动偷偷开事务
        kwargs={"autocommit": True},
        timeout=10,
        open=False,
        name="factopia",
    )
    _pool.open()
    _pool.wait(timeout=10)
    log.info("数据库连接池就绪（%s–%s 条连接）", min_size, max_size)
    return _pool


def close_pool() -> None:
    """关闭连接池（测试与进程退出时用）。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[Any]:
    """一个事务。异常自动回滚，正常退出提交。

    用法：
        with db.tx() as conn:
            conn.execute("UPDATE ...")
            conn.execute("INSERT ...")
    """
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def conn() -> Iterator[Any]:
    """借一条连接但不开启事务（只读查询、单语句用）。"""
    with pool().connection() as c:
        yield c


# ── 便捷查询 ─────────────────────────────────────────────────

def query(sql: str, params: Any = None) -> list[tuple]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def query_one(sql: str, params: Any = None):
    with conn() as c:
        row = c.execute(sql, params).fetchone()
    return row


def execute(sql: str, params: Any = None) -> int:
    with conn() as c:
        return c.execute(sql, params).rowcount


def health() -> dict:
    """给 /api/health 与排障用的最小体检。"""
    try:
        row = query_one(
            "SELECT current_database(), current_user, "
            "(SELECT extversion FROM pg_extension WHERE extname='vector'), "
            "pg_size_pretty(pg_database_size(current_database())), "
            "(SELECT count(*) FROM pg_stat_activity)"
        )
        return {
            "ok": True,
            "database": row[0],
            "user": row[1],
            "pgvector": row[2],
            "size": row[3],
            "connections": row[4],
            "dsn_hint": _mask(dsn()),
        }
    except Exception as exc:  # 网络 / 认证 / 库不存在都可能
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "dsn_hint": _mask(dsn())}


def mirror(fn, what: str = ""):
    """`dual` 模式的补写：文件写成功后再写数据库。

    失败只记日志，**绝不向上抛**——dual 阶段的读仍然走文件，补写失败不能影响用户请求，
    但必须留下痕迹，否则"两边不一致"会静默积累。差异核对交给 tools/verify_migration.py。
    """
    try:
        return fn()
    except Exception:
        log.exception("补写数据库失败（%s）：文件已写入，DB 侧可能落后", what or "未命名")
        return None


def write_db(domain: str | None, fn, what: str = ""):
    """写库的统一入口：`db` 模式直写并让异常上抛，`dual` 模式补写且吞掉异常。

    各域 `save_*` 里重复的 `if mode == "db" … elif mode == "dual": db.mirror(...)`
    都可以换成这一个调用，免得漏掉 dual 分支——那会让灰度期库里悄悄落后。
    """
    m = mode(domain)
    if m == "db":
        return fn()
    if m == "dual":
        return mirror(fn, what)
    return None


def _mask(text: str) -> str:
    """日志里不要出现口令。"""
    import re

    return re.sub(r"password=(\S+)", "password=***", text)
