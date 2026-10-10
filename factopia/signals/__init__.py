"""信号目录（业务需求 BR-24、30、41、42；Spec 第 14.15 节）。

- 目录项按"研究版本 × 市场"分版本（signal_id = <研究>.v<版本>.<市场>），来自审批提交：
  获批且在部署范围内 → 生产中；获批但不在部署范围 → 不适用（保留原因）；退回 / 拒绝 / 审批中 → 未获批，不得用于生产。
- 标签（BR-30）：适用语言、授权类型、数据范围、规则版本、最后验证日期、下次复核日期（获批后 90 天，或预警后提前）。
- 状态操作：暂停 / 恢复 / 停用，按市场单独进行（BR-24：只暂停受影响地区的版本），须写原因；
  事件写入 runtime/signals/events.jsonl（哈希链，与审批留痕相同的做法）。
- 监控结果由后台运行器写入 runtime/signals/<signal_id>/monitor.json（factopia/signals/monitor.py）。
"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from factopia import approval

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
DIR = RUNTIME / "signals"
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}
STATE_ZH = {"active": "生产中", "paused": "已暂停", "retired": "已停用", "not_applicable": "不适用", "not_approved": "未获批", "in_review": "审批中"}
ACTION_ZH = {"pause": "暂停", "resume": "恢复", "retire": "停用"}
REVIEW_DAYS = 90


class CatalogError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def events() -> list[dict]:
    p = DIR / "events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def _append(ev: dict) -> dict:
    prev = (events() or [{"hash": "0" * 64}])[-1]["hash"]
    ev = {**ev, "at": now(), "prev": prev}
    ev["hash"] = hashlib.sha256(json.dumps({k: v for k, v in ev.items() if k != "hash"}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    DIR.mkdir(parents=True, exist_ok=True)
    with (DIR / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def monitor_result(signal_id: str) -> dict | None:
    p = DIR / signal_id / "monitor.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _mandate(mid: str | None) -> dict:
    for p in sorted((CONFIGS / "mandates").glob("*.yaml")):
        m = yaml.safe_load(p.read_text(encoding="utf-8"))
        if m.get("id") == mid:
            return m
    return {}


def _languages(spec: dict, mandate: dict) -> list[str]:
    langs = ["中文（主报告）", "English", "Français"]          # 报告与证据附件
    text = spec.get("hypothesis_original") or ""
    if any("一" <= ch <= "鿿" for ch in text):
        langs.insert(0, "假设原文：中文")
    return langs + ([f"授权报告语言：{mandate['report_language']}"] if mandate.get("report_language") else [])


def catalog() -> list[dict]:
    """由全部审批提交生成目录；同一研究版本以最新一次提交为准。"""
    latest: dict[tuple, dict] = {}
    for s in approval.list_all():
        k = (s["research_id"], s["version"])
        if k not in latest:                                    # list_all 按时间倒序
            latest[k] = s
    evs = events()
    out = []
    for (rid, ver), summ in latest.items():
        sub = approval.load(summ["id"])
        mt = sub["materials"]
        spec_p = approval._dir(sub["id"]) / "materials" / "spec.yaml"
        spec = yaml.safe_load(spec_p.read_text(encoding="utf-8")) if spec_p.exists() else {}
        mandate = _mandate(mt.get("mandate"))
        lock = sub.get("lock")
        packs = [f"{p['pack']} {p['version']}" for p in mt["prechecks"]["setup"]["packs"]]
        cats = sorted({d.get("category") for d in spec.get("data_needs") or []} | {"price_volume", "benchmark"})
        for m in sub["markets"]:
            sid = f"{rid}.v{ver}.{m}"
            if sub["status"] in ("approved", "approved_restricted"):
                state = "active" if m in (sub.get("approved_markets") or []) else "not_applicable"
                reason = None if state == "active" else "投资组合经理限定地区：" + next(
                    (e["note"] for e in approval.events(sub["id"]) if e["action"] == "restrict"), "未在获批范围内")
            elif sub["status"] == "in_review":
                state, reason = "in_review", "审批中，不得用于生产"
            else:
                state = "not_approved"
                last = next((e for e in reversed(approval.events(sub["id"])) if e["action"] in ("return", "reject")), None)
                reason = f"{approval.ACTION_ZH[last['action']]}（{last['role']}）：{last['note']}" if last else approval.STATUS_ZH[sub["status"]]
            mine = [e for e in evs if e["signal_id"] == sid]
            for e in mine:                                     # 暂停 / 恢复 / 停用 只作用于生产中的版本
                if state in ("active", "paused"):
                    state = {"pause": "paused", "resume": "active", "retire": "retired"}[e["action"]]
                    reason = f"{ACTION_ZH[e['action']]}（{e['role']}）：{e['note']}"
            mon = monitor_result(sid)
            validated = (mon or {}).get("computed_at") or (lock or {}).get("locked_at")
            due = None
            if lock:
                base = datetime.fromisoformat(lock["locked_at"])
                due_dt = base + timedelta(days=REVIEW_DAYS)
                if mon and mon["suggestion"] != "ok":
                    due_dt = min(due_dt, datetime.fromisoformat(mon["computed_at"]) + timedelta(days=7))
                due = due_dt.date().isoformat()
            res = mt["results"].get(m) or {}
            out.append({
                "signal_id": sid, "research_id": rid, "version": ver, "market": m, "market_zh": MARKET_ZH[m],
                "title": mt["title"], "submission": sub["id"], "state": state, "state_zh": STATE_ZH[state], "reason": reason,
                "production": state == "active",
                "labels": {"languages": _languages(spec, mandate), "mandate": f"{mandate.get('name', mt.get('mandate'))}（{mandate.get('service_region', '')}）",
                           "data_scope": {"categories": cats, "universe": {"hk": "恒生指数成分股", "uk": "富时 100 成分股", "fr": "CAC 40 成分股"}[m],
                                          "snapshot": (mt["data_snapshot"].get(m) or "")[:16]},
                           "rule_versions": packs, "last_validated": (validated or "")[:10] or None, "next_review": due,
                           "applicability": (lock or {}).get("applicability", {}).get(m)},
                "run": {"run_id": res.get("run_id"), "loop": res.get("loop"), "rank_ic": res.get("rank_ic"), "rank_ic_t": res.get("rank_ic_t"),
                        "credibility": res.get("credibility")},
                "thresholds": (lock or {}).get("thresholds") or {},
                "monitor": {k: mon[k] for k in ("computed_at", "suggestion", "suggestion_zh")} | {"alerts": len(mon["alerts"])} if mon else None,
                "events": mine,
            })
    return out


def entry(signal_id: str) -> dict:
    for e in catalog():
        if e["signal_id"] == signal_id:
            return e
    raise CatalogError("信号不存在")


def act(signal_id: str, action: str, role: str, note: str) -> dict:
    if role not in ("投资组合经理", "运营 / 技术负责人", "模型风险"):
        raise CatalogError("只有投资组合经理、运营 / 技术负责人、模型风险可以变更信号状态")
    if action not in ACTION_ZH:
        raise CatalogError("操作无效")
    if not note.strip():
        raise CatalogError("须写明原因")
    e = entry(signal_id)
    allowed = {"pause": ("active",), "resume": ("paused",), "retire": ("active", "paused")}[action]
    if e["state"] not in allowed:
        raise CatalogError(f"当前状态为「{e['state_zh']}」，不能{ACTION_ZH[action]}")
    _append({"signal_id": signal_id, "market": e["market"], "action": action, "role": role, "by": f"模拟用户（{role}）", "note": note})
    return entry(signal_id)


def monitor_entry(signal_id: str) -> dict:
    """后台运行器计算监控所需的输入：冻结的因子代码、参照期、预测周期与获批阈值。"""
    e = entry(signal_id)
    d = approval._dir(e["submission"]) / "materials"
    factors = json.loads((d / "factors.json").read_text(encoding="utf-8")).get(e["market"]) or []
    if not factors:
        raise CatalogError("该市场版本没有冻结的因子代码")
    sub = approval.load(e["submission"])
    periods = (sub["materials"]["parameters"]["periods"] or {}).get(e["market"]) or {}
    return {"signal_id": signal_id, "market": e["market"], "factors": factors, "horizon": sub["materials"]["parameters"]["horizon"],
            "ref_start": periods.get("test_start"), "ref_end": periods.get("test_end"), "thresholds": e["thresholds"]}
