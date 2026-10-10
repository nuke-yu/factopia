"""内部因子库（业务需求 BR-35；Spec 第 14.18 节）。

因子库由多个来源组成，每个来源实现同一个接口 items() → 统一格式的条目；团队专有因子库按同一格式接入：

| 来源 | 内容 | 状态 |
|---|---|---|
| alpha158 | Qlib Alpha158 的 158 个公开基准因子：定义、因子组、三个市场各时间段的 rank IC 与 t 值、迁移标签 | 公开基准 |
| approved | 审批通过的信号（冻结的因子名称、说明、公式、代码；获批市场、获批时的 rank IC 与可信度；信号目录状态） | 已获批 |
| proprietary | 团队专有因子库：configs/factor_library/*.yaml（同一格式，见 README）；POC 只有接口与示例格式 | 专有（接入后） |

统一格式：id、name、source、status、group、description、formulation、code、markets（各市场统计）、label（迁移标签）、
period_note（统计所用时间段）、provenance（来源：研究编号 / 提交编号 / 文件）。
防泄漏：Alpha158 默认展示测试期之前（2015–2021）的统计；提供给 AI 的只有名称与说明（不含任何测试期以后的数值）。
"""
import json
import os
from pathlib import Path

import yaml

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(RUNTIME / "results")))
SOURCE_ZH = {"alpha158": "Alpha158 公开基准", "approved": "获批信号", "proprietary": "团队专有因子库"}
FIELDS = ("id", "name", "source", "status", "group", "description", "formulation", "code", "markets", "label",
          "period_note", "provenance")


def alpha158(horizon: int = 20) -> list[dict]:
    p = RESULTS / "alpha158" / f"h{horizon}" / "cube.json"
    if not p.exists():
        return []
    cube = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for f in cube["factors"]:
        per = f["periods"]
        out.append({"id": f"alpha158:{f['name']}", "name": f["name"], "source": "alpha158", "status": "公开基准",
                    "group": f["group"], "description": f["desc"], "formulation": None, "code": None,
                    "markets": {m: {"rank_ic": per["train"]["ric"].get(m), "t": per["train"]["t"].get(m)} for m in per["train"]["t"]},
                    "label": per["train"].get("label"),
                    "periods": {k: {"label": cube["periods"][k]["label"], "note": cube["periods"][k]["note"],
                                    "t": v["t"], "rank_ic": v["ric"], "transfer": v.get("label")} for k, v in per.items()},
                    "period_note": f"{cube['periods']['train']['label']}（{cube['periods']['train']['note']}），预测周期 {horizon} 日",
                    "provenance": f"results/alpha158/h{horizon}/cube.json"})
    return out


def approved() -> list[dict]:
    from factopia import approval, signals
    cat = {e["signal_id"]: e for e in signals.catalog()}
    out = []
    for s in approval.list_all():
        if s["status"] not in ("approved", "approved_restricted"):
            continue
        sub = approval.load(s["id"])
        fp = approval._dir(s["id"]) / "materials" / "factors.json"
        factors = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
        res = sub["materials"]["results"]
        names: dict[str, dict] = {}
        for m, fs in factors.items():
            for f in fs:
                e = names.setdefault(f["name"], {**{k: f.get(k) for k in ("name", "description", "formulation", "code")}, "markets": {}})
                sid = f"{s['research_id']}.v{s['version']}.{m}"
                e["markets"][m] = {"rank_ic": (res.get(m) or {}).get("rank_ic"), "t": (res.get(m) or {}).get("rank_ic_t"),
                                   "credibility": (res.get(m) or {}).get("credibility"),
                                   "state": (cat.get(sid) or {}).get("state_zh"), "approved": m in (s.get("approved_markets") or []),
                                   "simulated": bool((res.get(m) or {}).get("simulated"))}
        for n, e in names.items():
            out.append({"id": f"approved:{s['id']}:{n}", "source": "approved", "status": s["status_zh"],
                        "group": f"{s['research_id']} v{s['version']}", **e, "label": (sub["materials"].get("transfer") or {}).get("label"),
                        "period_note": "获批时的测试期统计（2022–2023）；为该研究版本采纳轮次（多个因子合成）的结果，不是单个因子", "provenance": f"审批提交 {s['id']}"})
    return out


def proprietary() -> list[dict]:
    """团队专有因子库的接入口：configs/factor_library/*.yaml，每个文件 {source_name, version, factors: [统一格式]}。"""
    out = []
    d = CONFIGS / "factor_library"
    for p in sorted(d.glob("*.yaml")) if d.exists() else []:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for f in doc.get("factors") or []:
            out.append({k: f.get(k) for k in FIELDS} | {"id": f"proprietary:{doc.get('source_name', p.stem)}:{f['name']}",
                                                        "source": "proprietary", "status": f.get("status") or "专有",
                                                        "provenance": f"{p.name} {doc.get('version', '')}".strip()})
    return out


SOURCES = {"alpha158": alpha158, "approved": approved, "proprietary": proprietary}


def items(sources: list[str] | None = None) -> list[dict]:
    from factopia import access
    out = []
    for k in sources or list(SOURCES):
        try:
            out += SOURCES[k]()
        except FileNotFoundError:
            continue
    if access.cross_region():
        return out
    # 地区视角（第 14.20 节）：各市场统计只保留本地区；获批信号只保留涉及本地区市场的
    view = []
    for i in out:
        ms = {m: v for m, v in (i.get("markets") or {}).items() if access.can(m)}
        if i["source"] == "approved" and not ms:
            continue
        view.append({**i, "markets": ms})
    return view


def summary() -> dict:
    its = items()
    return {"total": len(its), "by_source": {SOURCE_ZH[k]: sum(1 for i in its if i["source"] == k) for k in SOURCES},
            "proprietary_dir": "configs/factor_library/（尚未接入：按 README 的统一格式放入 YAML 即可）"}
