"""研究迭代 Agent 的命令行入口（P0）。

    python -m factopia.agents run --research <id> --version <n> [--rounds 1] [--dry-run] [--yes]
    python -m factopia.agents show <agent_id>
    python -m factopia.agents list

--dry-run 只生成事实、诊断与计划，不执行动作；--yes 跳过人工闸门（演示 / 自动化）。
"""
import argparse
import json
import sys

from factopia.agents import approve as approve_mod
from factopia.agents import loop, policy as policy_mod, store


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factopia.agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="从一个研究版本启动 Agent")
    run.add_argument("--research", required=True, help="研究编号")
    run.add_argument("--version", type=int, required=True, help="锁定的版本号")
    run.add_argument("--rounds", type=int, default=1, help="本轮最多运行的轮数（P0 默认 1）")
    run.add_argument("--dry-run", action="store_true", help="只生成事实、诊断与计划")
    run.add_argument("--yes", action="store_true", help="自动批准原草稿（演示 / 测试）")
    run.add_argument("--config", help="策略 YAML 路径（默认 configs/agents/iteration.yaml）")
    show = sub.add_parser("show", help="查看一个 Agent 的记录与每轮文件")
    show.add_argument("agent_id")
    approve = sub.add_parser("approve", help="人工审改后批准 vN+1 草稿并继续运行")
    approve.add_argument("agent_id")
    approve.add_argument("--file", help="人工修改后的草稿 JSON / YAML；默认用 Agent 原草稿")
    approve.add_argument("--run", action=argparse.BooleanOptionalAction, default=True,
                         help="批准后是否自动创建运行（默认是；--no-run 只保存版本）")
    approve.add_argument("--by", default="研究员（演示，未登录）", help="审批人")
    reject = sub.add_parser("reject", help="拒绝当前轮次，不保存版本、不运行")
    reject.add_argument("agent_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--by", default="研究员（演示，未登录）", help="审批人")
    sub.add_parser("list", help="列出全部 Agent")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        try:
            policy = policy_mod.load(args.config) if args.config else policy_mod.load()
            agent = loop.run(args.research, args.version, rounds=args.rounds,
                             dry_run=args.dry_run, approve=args.yes, policy=policy)
        except Exception as e:
            print(f"失败：{e}", file=sys.stderr)
            return 1
        _dump({"agent_id": agent["agent_id"], "state": agent["state"],
               "root": agent["root"], "cost_usd": agent["cost_usd"],
               "stop_reason": agent.get("stop_reason"), "last_error": agent.get("last_error"),
               "report": str(store.dir_path(agent["agent_id"]) / "report.md")})
        print(f"记录：{store.dir_path(agent['agent_id'])}")
        return 0 if agent.get("state") != "failed" else 1

    if args.cmd == "show":
        try:
            agent = store.load(args.agent_id)
        except Exception as e:
            print(f"失败：{e}", file=sys.stderr)
            return 1
        _dump(agent)
        for n in range(1, int(agent.get("current_round") or 0) + 1):
            for name in ("facts", "diagnosis", "plan", "draft_action", "next_version_draft",
                         "approval", "action", "evaluation"):
                data = store.read_round(args.agent_id, n, name)
                if data is not None:
                    print(f"\n--- round {n} / {name} ---")
                    _dump(data)
        return 0

    if args.cmd == "approve":
        try:
            out = approve_mod.approve(args.agent_id, file=args.file, run=args.run, by=args.by)
        except Exception as e:
            print(f"失败：{e}", file=sys.stderr)
            return 1
        _dump({"agent_id": args.agent_id, "saved_version": out["saved"]["version"],
               "current_version": out["agent"]["current_version"],
               "run": args.run, "job": out["job"]["job_id"], "job_state": out["job"]["state"]})
        return 0

    if args.cmd == "reject":
        try:
            out = approve_mod.reject(args.agent_id, args.reason, by=args.by)
        except Exception as e:
            print(f"失败：{e}", file=sys.stderr)
            return 1
        _dump({"agent_id": args.agent_id, "state": out["agent"]["state"],
               "stop_reason": out["agent"].get("stop_reason")})
        return 0

    if args.cmd == "list":
        rows = store.list_agents()
        if not rows:
            print("还没有 Agent 记录。")
            return 0
        for a in rows:
            root = a.get("root") or {}
            print(f"{a['agent_id']}\t{a.get('state')}\t{root.get('research_id')} v{root.get('version')}"
                  f"\t{a.get('created_at')}\t{a.get('stop_reason') or ''}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
