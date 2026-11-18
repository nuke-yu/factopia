#!/usr/bin/env python3
"""最小 ECS 远程执行工具（paramiko）：跑命令 / 传文件。

用法：
  python scripts/ecs.py run "uname -a"
  python scripts/ecs.py run --sudo "docker ps"
  python scripts/ecs.py put 本地路径 远程路径
  python scripts/ecs.py get 远程路径 本地路径

连接参数从环境变量读取（默认取自 .workbuddy/env.sh）：
  FACTOPIA_SSH_HOST / FACTOPIA_SSH_PORT / FACTOPIA_SSH_USER / FACTOPIA_SSH_PASSWORD
"""
from __future__ import annotations

import os
import sys

import paramiko

HOST = os.environ.get("FACTOPIA_SSH_HOST", "101.201.239.22")
PORT = int(os.environ.get("FACTOPIA_SSH_PORT", "22"))
USER = os.environ.get("FACTOPIA_SSH_USER", "root")
PASSWORD = os.environ.get("FACTOPIA_SSH_PASSWORD", "")


def client() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        hostname=HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return c


def run(cmd: str, timeout: int = 300) -> int:
    c = client()
    try:
        stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout, get_pty=False)
        for line in iter(stdout.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", "replace")
        if err.strip():
            sys.stderr.write(err)
        return rc
    finally:
        c.close()


def put(local: str, remote: str) -> int:
    c = client()
    try:
        sftp = c.open_sftp()
        sftp.put(local, remote)
        sftp.close()
        print(f"uploaded {local} -> {remote}")
        return 0
    finally:
        c.close()


def get(remote: str, local: str) -> int:
    c = client()
    try:
        sftp = c.open_sftp()
        sftp.get(remote, local)
        sftp.close()
        print(f"downloaded {remote} -> {local}")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    action = sys.argv[1]
    if action == "run":
        args = [a for a in sys.argv[2:] if a != "--sudo"]
        sys.exit(run(" ".join(args)))
    if action == "put":
        sys.exit(put(sys.argv[2], sys.argv[3]))
    if action == "get":
        sys.exit(get(sys.argv[2], sys.argv[3]))
    print(__doc__)
    sys.exit(2)
