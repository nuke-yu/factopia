#!/usr/bin/env python
"""经本队 ECS 跳板访问比赛 RDS（PostgreSQL 16 / pgvector）的工具。

拓扑：
    本机 ──SSH（公网，加密）──> ECS 101.201.239.22 ──内网──> RDS 10.0.0.236:5432

RDS 的 5432 只对已登记 ECS 的内网 IP 开放，本机无法直连。本脚本用 paramiko/sshtunnel
在 ECS 上开一条 direct-tcpip 通道，并在本机回环地址上起一个转发端口，
之后的 psycopg 连接与 `psql "host=127.0.0.1 port=<转发口>"` 都走这条隧道。

之所以不用 `ssh -N -L`：Bash 环境是非交互的，ssh 的密码提示无法自动喂入，
本机也没有 sshpass。sshtunnel 用 paramiko 直接做密码认证，可完全非交互运行。

用法：
    # 1) 连通性与环境体检（不建任何对象）
    python scripts/db/pg_tunnel.py check

    # 2) 执行 DDL（幂等，可重复）
    python scripts/db/pg_tunnel.py apply migrations/001_init_schema.sql

    # 3) 只开隧道，供本地 psql / DBeaver / 应用使用（前台阻塞）
    python scripts/db/pg_tunnel.py keepalive --local-port 15432

密码来源优先级：命令行 > 环境变量。避免密码进 shell 历史，推荐环境变量：
    export FACTOPIA_SSH_PASSWORD='...'      # ECS root 密码
    export FACTOPIA_PGPASSWORD='...'        # RDS team12 密码
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_SSH_HOST = "101.201.239.22"
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22
DEFAULT_RDS_HOST = "pgm-2ze9t2uh39o1115k.rwlb.rds.aliyuncs.com"
DEFAULT_RDS_PORT = 5432
DEFAULT_RDS_DB = "team12"
DEFAULT_RDS_USER = "team12"
DEFAULT_LOCAL_PORT = 15432


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="经 ECS 跳板连接比赛 RDS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("mode", choices=["check", "apply", "keepalive"], help="运行模式")
    p.add_argument("sql", nargs="?", help="apply 模式下的 .sql 文件路径")
    p.add_argument("--ssh-host", default=_env("FACTOPIA_SSH_HOST") or DEFAULT_SSH_HOST)
    p.add_argument("--ssh-port", type=int, default=int(_env("FACTOPIA_SSH_PORT") or DEFAULT_SSH_PORT))
    p.add_argument("--ssh-user", default=_env("FACTOPIA_SSH_USER") or DEFAULT_SSH_USER)
    p.add_argument("--ssh-password", default=_env("FACTOPIA_SSH_PASSWORD", "SSH_PASSWORD"))
    p.add_argument("--ssh-key", default=_env("FACTOPIA_SSH_KEY"), help="私钥路径（与密码二选一）")
    p.add_argument("--rds-host", default=_env("FACTOPIA_RDS_HOST") or DEFAULT_RDS_HOST)
    # 注意：不能用 PGPORT 作默认——本地 env 里 PGPORT=15432 是"隧道本地端口"，
    # 不是 RDS 远端端口（2026-11-16 实测踩坑：隧道起来但转发到 10.0.0.236:15432 必然超时）
    p.add_argument("--rds-port", type=int, default=int(_env("FACTOPIA_RDS_PORT") or DEFAULT_RDS_PORT))
    p.add_argument("--db", default=_env("FACTOPIA_PGDATABASE", "PGDATABASE") or DEFAULT_RDS_DB)
    p.add_argument("--user", default=_env("FACTOPIA_PGUSER", "PGUSER") or DEFAULT_RDS_USER)
    p.add_argument("--password", default=_env("FACTOPIA_PGPASSWORD", "PGPASSWORD"))
    p.add_argument("--local-port", type=int, default=DEFAULT_LOCAL_PORT,
                   help="本地转发端口；0 表示由系统分配（默认 15432，避开本机可能已有的 5432）")
    p.add_argument("--report", help="把体检结果额外写成 JSON 文件")
    args = p.parse_args(argv)

    if args.mode == "apply" and not args.sql:
        p.error("apply 模式需要提供 .sql 文件路径")
    if not args.ssh_password and not args.ssh_key:
        p.error("缺少 ECS 认证：请设置 FACTOPIA_SSH_PASSWORD，或用 --ssh-key 指定私钥")
    return args


def open_tunnel(args: argparse.Namespace):
    """建立 SSH 隧道，返回 (server, local_port)。"""
    try:
        from sshtunnel import SSHTunnelForwarder
    except ImportError:
        print(
            "[deps] 缺少 sshtunnel / psycopg。安装到项目环境：\n"
            "       .venv/Scripts/python.exe -m pip install 'sshtunnel' 'psycopg[binary]'\n"
            "       或临时目录：python -m pip install --target /tmp/dbtools ... 并设 PYTHONPATH=/tmp/dbtools",
            file=sys.stderr,
        )
        raise SystemExit(3)

    target = (args.rds_host, args.rds_port)
    kwargs: dict = dict(
        ssh_address_or_host=(args.ssh_host, args.ssh_port),
        ssh_username=args.ssh_user,
        remote_bind_address=target,
        local_bind_address=("127.0.0.1", args.local_port),
        # RDS 侧可能空闲断连，隧道本身也要保活
        set_keepalive=30.0,
    )
    if args.ssh_key:
        kwargs["ssh_pkey"] = args.ssh_key
        if args.ssh_password:
            kwargs["ssh_private_key_password"] = args.ssh_password
    else:
        kwargs["ssh_password"] = args.ssh_password

    print(f"[tunnel] {args.ssh_user}@{args.ssh_host}:{args.ssh_port} -> {args.rds_host}:{args.rds_port}")
    server = SSHTunnelForwarder(**kwargs)
    try:
        server.start()
    except Exception as e:  # 认证失败 / 端口占用 / ECS 到 RDS 不通
        print(f"[tunnel] 建立失败: {type(e).__name__}: {e}")
        raise SystemExit(2)

    local_port = server.local_bind_port
    print(f"[tunnel] 本地转发已就绪 127.0.0.1:{local_port}")
    return server, local_port


def connect(local_port: int, args: argparse.Namespace):
    import psycopg

    conninfo = (
        f"host=127.0.0.1 port={local_port} dbname={args.db} "
        f"user={args.user} password={args.password or ''} "
        # 比赛库上限 30 连接，客户端不要悄悄开一堆；tunnel 下延迟低，5 秒超时足够
        "connect_timeout=10 application_name=factopia-dbcheck sslmode=prefer"
    )
    conn = psycopg.connect(conninfo, autocommit=True)
    print(f"[pg] 已连接 {args.db}@{args.rds_host}  user={args.user}")
    return conn


def run_check(conn) -> dict:
    """环境体检：版本、扩展、配额、对象、向量算子。"""
    report: dict = {}
    cur = conn.cursor()

    def one(sql: str, params=None):
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None

    def many(sql: str):
        cur.execute(sql)
        return cur.fetchall()

    report["server_version"] = one("SHOW server_version")
    report["encoding"] = one("SHOW server_encoding")
    report["current_user"] = one("SELECT current_user")
    report["current_database"] = one("SELECT current_database()")
    report["is_superuser"] = one(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    )

    # pgvector 是否装在本库（比赛说明里“已启用”，但扩展是按库安装的，要实测）
    report["extensions"] = [
        {"name": r[0], "version": r[1], "schema": r[2]}
        for r in many(
            "SELECT extname, extversion, n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace ORDER BY 1"
        )
    ]
    try:
        report["vector_distance_test"] = one(
            "SELECT ('[1,2,3]'::vector <-> '[1,2,4]'::vector)::text"
        )
    except Exception as e:
        conn.rollback()
        report["vector_distance_test"] = f"失败: {type(e).__name__}: {e}"

    report["max_connections"] = one("SHOW max_connections")
    report["current_connections"] = one("SELECT count(*) FROM pg_stat_activity")
    report["db_size_pretty"] = one("SELECT pg_size_pretty(pg_database_size(current_database()))")
    report["db_size_bytes"] = one("SELECT pg_database_size(current_database())")

    report["factopia_schema_exists"] = one(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'factopia')"
    )
    if report["factopia_schema_exists"]:
        report["factopia_tables"] = one(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'factopia' AND table_type = 'BASE TABLE'"
        )
        report["factopia_views"] = one(
            "SELECT count(*) FROM information_schema.views WHERE table_schema = 'factopia'"
        )
        report["factopia_indexes"] = one(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'factopia'"
        )
        report["factopia_triggers"] = one(
            "SELECT count(DISTINCT tgname) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'factopia'"
        )
    else:
        report["factopia_tables"] = 0
        report["factopia_views"] = 0
        report["factopia_indexes"] = 0
        report["factopia_triggers"] = 0

    # 建 schema 需要库级 CREATE 权限，提前确认，免得 apply 到一半才报错
    report["can_create_in_db"] = one(
        "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
    )
    return report


def print_report(r: dict) -> None:
    print("\n" + "=" * 62)
    print("RDS 体检")
    print("=" * 62)
    print(f"  server_version      {r['server_version']}   encoding={r['encoding']}")
    print(f"  user/database       {r['current_user']} / {r['current_database']}   superuser={r['is_superuser']}")
    print(f"  建库权限 CREATE      {r['can_create_in_db']}")
    print(f"  连接数               {r['current_connections']} / {r['max_connections']}")
    print(f"  库大小               {r['db_size_pretty']}  (预算 8 GB)")
    print(f"  扩展                 " + ", ".join(f"{e['name']} {e['version']}" for e in r["extensions"]))
    print(f"  向量算子 <->         {r['vector_distance_test']}   (预期 1)")
    print(f"  factopia schema      存在={r['factopia_schema_exists']}")
    print(f"  已建对象             表 {r['factopia_tables']} / 视图 {r['factopia_views']} "
          f"/ 索引 {r['factopia_indexes']} / 触发器 {r['factopia_triggers']}")
    print("=" * 62)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server, local_port = open_tunnel(args)
    try:
        if args.mode == "keepalive":
            print(f"\n隧道已开启，客户端请连 127.0.0.1:{local_port}")
            print("  psql \"host=127.0.0.1 port=%d dbname=%s user=%s\"" % (local_port, args.db, args.user))
            print("Ctrl-C 结束。\n")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
            return 0

        conn = connect(local_port, args)
        try:
            if args.mode == "check":
                report = run_check(conn)
                print_report(report)
                if args.report:
                    Path(args.report).write_text(
                        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print(f"[report] 已写入 {args.report}")
                return 0

            # apply
            sql_path = Path(args.sql)
            sql_text = sql_path.read_text(encoding="utf-8")
            print(f"[apply] {sql_path}  {len(sql_text):,} 字符")
            before = run_check(conn)
            t0 = time.time()
            # 脚本自带 BEGIN/COMMIT，用自动提交模式让事务边界由文件控制；
            # 任一句失败时 PostgreSQL 会中止整个隐式事务，等价于 psql 的 ON_ERROR_STOP。
            conn.execute(sql_text)
            elapsed = time.time() - t0
            after = run_check(conn)
            print(f"[apply] 执行完成  {elapsed:.2f}s")
            print(f"[apply] 表 {before['factopia_tables']} -> {after['factopia_tables']}，"
                  f"视图 {before['factopia_views']} -> {after['factopia_views']}，"
                  f"索引 {before['factopia_indexes']} -> {after['factopia_indexes']}，"
                  f"触发器 {before['factopia_triggers']} -> {after['factopia_triggers']}")
            print_report(after)
            if args.report:
                Path(args.report).write_text(
                    json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"[report] 已写入 {args.report}")
            return 0
        finally:
            conn.close()
    finally:
        server.stop()
        print("[tunnel] 已关闭")


if __name__ == "__main__":
    sys.exit(main())
