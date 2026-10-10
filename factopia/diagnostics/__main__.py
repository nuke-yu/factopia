"""研究结果诊断命令行（app 容器）：

  python -m factopia.diagnostics                 # 全部运行
  python -m factopia.diagnostics <运行编号> ...   # 指定运行
"""
import sys

from factopia.diagnostics import RESULTS, diagnose_run


def main() -> None:
    runs = sys.argv[1:] or sorted(p.name for p in (RESULTS / "runs").glob("*") if (p / "run.json").exists())
    for r in runs:
        print(r, diagnose_run(RESULTS / "runs" / r), "组", flush=True)


if __name__ == "__main__":
    main()
