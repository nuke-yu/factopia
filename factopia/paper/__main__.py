"""命令行入口：docker compose exec runner python -m factopia.paper

无参数：对所有接入中的模拟盘各执行一轮推进（结算挂单 → 重算信号 → 月度调仓 → 出报告）。
参数 report：只重算报告，不推进行情（页面刷新 / 数据补齐时使用）。

退出码：全部成功（或没有模拟盘）为 0，任一失败为 1。
"""
import sys


def main(argv: list[str] | None = None) -> int:
    from factopia.paper import engine
    from factopia.paper.report import report  # 直接导入函数（report.py 单向依赖 engine，无循环）

    argv = list(sys.argv[1:] if argv is None else argv)
    only_report = bool(argv) and argv[0] == "report"
    rows = [r for r in engine.enrolled() if r.get("active")]
    if not rows:
        print("[paper] 没有接入中的模拟盘")
        return 0
    ok = True
    for r in rows:
        sid = r["signal_id"]
        try:
            if only_report:
                report(sid)
            else:
                engine.cycle(sid)
        except Exception as e:  # 单个信号失败不影响其他信号推进
            print(f"[paper] {sid} 失败：{e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
