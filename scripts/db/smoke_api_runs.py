"""API 直读清零后的验收：同一批端点，在 files 与 db 两种模式下必须给出同样的答案。

这是"看起来切了其实没切"的正面检查。做法：

1. 用 `FACTOPIA_STORAGE=files` 起一个 TestClient，把只读端点全打一遍，存下结果；
2. 换成 `db`（runs/misc/research/portfolio 全部读库），同样打一遍；
3. 逐端点比对；差异只允许出现在"库里本来就没有"的字段上（明列）。

跑法（需要隧道与已完成回填）：

    python scripts/db/smoke_api_runs.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("FACTOPIA_RUNTIME", str(ROOT / "runtime"))
os.environ.setdefault("FACTOPIA_CONFIGS_DIR", str(ROOT / "configs"))
os.environ.setdefault("FACTOPIA_RESULTS_DIR", str(ROOT / "runtime" / "results"))

from fastapi.testclient import TestClient  # noqa: E402

from factopia import db  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, note: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'} {name}{('  ' + note) if note else ''}")


def endpoints() -> list[str]:
    """只读、且应该在两种模式下一致的端点。"""
    out = ["/api/runs", "/api/projects", "/api/refdata/summary", "/api/refdata/dictionary",
           "/api/context/hk", "/api/research/examples"]
    try:
        runs = db.query("SELECT run_id FROM factopia.run ORDER BY run_id")
    except Exception:
        runs = []
    for (rid,) in runs:
        out += [f"/api/runs/{rid}", f"/api/runs/{rid}/events", f"/api/runs/{rid}/quantiles",
                f"/api/runs/{rid}/validation", f"/api/runs/{rid}/monthly",
                f"/api/runs/{rid}/backtest", f"/api/runs/{rid}/signal"]
    return out


def snapshot(mode: str, isolate: bool = False) -> dict[str, tuple[int, str]]:
    """采样一轮。`isolate=True` 时把结果卷指到空目录——

    这是"切了就是切了"的硬证据：db 模式下即使文件全都不在，页面也必须拿到同一份数据。
    只要还有任何一处偷偷回落到文件，这个阶段就会和 files 阶段对不上（或直接 404）。
    """
    os.environ["FACTOPIA_STORAGE"] = mode
    for dom in ("RUNS", "APP", "RESEARCH", "PORTFOLIO", "PAPER", "JOBS", "AGENTS", "SIGNALS",
                "IDEAS", "APPROVAL"):
        os.environ.pop(f"FACTOPIA_STORAGE_{dom}", None)
    # 每个模式都要重新导入，避免 db.mode 缓存与 lru_cache 串味
    for mod in [m for m in list(sys.modules) if m.startswith("factopia")]:
        if mod in ("factopia.db", "factopia.repo", "factopia.blobstore", "factopia.runstore"):
            continue
        if mod == "factopia.api.main":
            del sys.modules[mod]
    import importlib
    main = importlib.import_module("factopia.api.main")
    importlib.reload(main)
    if isolate:
        import tempfile
        empty = Path(tempfile.mkdtemp(prefix="factopia_empty_"))
        (empty / "runs").mkdir()
        (empty / "reports").mkdir()
        main.runstore.RUNS_DIR = empty / "runs"
        main.blobstore.REPORTS = empty / "reports"
    client = TestClient(main.app, headers={"X-Factopia-Region": "hk"})
    out = {}
    for path in endpoints():
        try:
            r = client.get(path)
            body = r.text if r.status_code >= 400 else json.dumps(r.json(), ensure_ascii=False, sort_keys=True)
            out[path] = (r.status_code, body)
        except Exception as exc:                       # 服务器内部错误也要记下来
            out[path] = (-1, f"{type(exc).__name__}: {exc}")
    return out


def main() -> int:
    print("files 模式采样 …")
    files = snapshot("files")
    print("db 模式采样 …")
    store = snapshot("db")
    print("db 模式 + 结果卷清空（证明没有回落文件）…")
    isolated = snapshot("db", isolate=True)

    print("\n比对：")
    same = diff = 0
    for path, (code_a, body_a) in files.items():
        code_b, body_b = store.get(path, (None, ""))
        if code_a == code_b and body_a == body_b:
            same += 1
            continue
        diff += 1
        print(f"  DIFF {path}: files({code_a}, {len(body_a)}B) / db({code_b}, {len(body_b)}B)")
        if code_b == 500:
            print(f"       db 侧 500：{body_b[:200]}")
    check("全部端点两模式一致", diff == 0, f"{same} 个一致 / {diff} 个不同")

    # 只比运行类端点：它们的全部真相都应该已经在库里
    run_paths = [p for p in files if p.startswith("/api/runs")]
    bad_iso = []
    for path in run_paths:
        code_a, body_a = files[path]
        code_b, body_b = isolated.get(path, (None, ""))
        if code_a != code_b or body_a != body_b:
            bad_iso.append((path, code_a, code_b))
    check("结果卷清空后运行接口仍然一致", not bad_iso,
          f"{len(run_paths) - len(bad_iso)}/{len(run_paths)} 个一致")
    for path, ca, cb in bad_iso:
        print(f"       回落了文件？{path}: files {ca} / db(空卷) {cb}")

    print("\n结论：")
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    db.close_pool()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
