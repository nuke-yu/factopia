"""回测结果自动导出：后台运行器结束一次运行后留下 .backtest_pending 标记，这里导出每日收益、持仓、IC 与分组收益。

在 app 镜像中常驻运行（docker-compose.yml 的 exporter 服务；读取 Qlib 的持仓对象需要 Qlib）：
    python -m factopia.export.watch
"""
import time
import traceback
from datetime import datetime

from factopia.export.backtests import RUNS_DIR, export_run

POLL_S = 10


def main() -> None:
    print("[exporter] 等待待导出的运行", flush=True)
    while True:
        for marker in sorted(RUNS_DIR.glob("*/.backtest_pending")):
            run_dir = marker.parent
            try:
                n = export_run(run_dir)
                marker.unlink()
                print(f"[exporter {datetime.now():%H:%M:%S}] {run_dir.name}: 导出 {n} 组回测结果", flush=True)
            except Exception:
                (run_dir / ".backtest_failed").write_text(traceback.format_exc()[-2000:])
                marker.unlink()
                print(f"[exporter] {run_dir.name}: 导出失败\n{traceback.format_exc()}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
