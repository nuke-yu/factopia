"""跨地区审批与审计（业务需求 BR-22、23、29、47；Spec 第 12.2 节、WB-04、05，第 14.14 节）。

流程顺序固定（WB-05）：立项预检 → 运行 → 组合与组合预检 → 报告 → 提交审批 → 逐级审批 → 获批锁定，不能跳步。

- 提交（submit）：前置条件——版本已锁定；各市场有采纳轮次；已构建授权组合；组合预检无阻断；已生成报告；
  使用文本数据时情报台没有待复核的低置信度翻译（BR-47）。提交时冻结提交材料（第 12.2.3 节）：假设与经济逻辑、
  因子代码、数据快照指纹、参数、三市场结果与迁移标签、两次预检、组合摘要、报告文件、提示词与模型版本、规则包与
  数据来源登记表全文、平台代码指纹；使用文本数据时冻结翻译（生效译文、概念、复核记录、术语表与翻译模型 / 提示词 /
  术语表版本，materials/translations/）；整体计算 sha256 作为提交指纹。
- 审批路线（第 12.2.1 节）：各市场本地研究员（香港 → 英国 → 法国）→ 数据负责人 → 有命中警告的地区合规 → 模型风险
  → 投资组合经理 → 运营 / 技术负责人。每一步只能由当前角色操作（演示时切换角色）。
- 操作（第 12.2.2 节）：批准（可附条件）、退回（须写原因，提交结束，修改后重新提交）、拒绝（合规、模型风险、组合经理；
  须写原因）、限定地区（组合经理：只批准部分市场）、例外处理（合规 / 模型风险对本人负责的警告规则写明理由）、意见。
  合规与模型风险在批准前须处理本人负责的全部警告命中（例外或确认，BR-47 重大规则命中人工复核）；
  模型风险须处理"模型例外"（可信度检查存疑的市场）。阻断规则不能例外（提交前即被拦截）。
- 运营 / 技术负责人：核对提交指纹与当前材料一致、部署范围等于获批市场，填写监控阈值，完成后生成获批锁定记录
  （地区适用性标签：总标签"通用 / 仅适用于 …"，各市场"适用 / 需调整参数 / 待合规确认 / 不适用"，BR-29、30）。
- 留痕（BR-23）：events.jsonl 只追加，每条记录包含上一条的哈希（哈希链），可校验是否被改动。
- 审计证据包：zip，含提交材料、全部事件、报告、规则包与数据来源登记表、因子代码、文件清单（sha256）与说明，
  回答"当时使用了什么数据、规则、模型，由谁批准"。
存储：runtime/approvals/<提交编号>/（submission.json、events.jsonl、materials/）。POC 用文件，正式平台改数据库。
"""
import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
DIR = RUNTIME / "approvals"
PKG = Path(__file__).resolve().parents[1]
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}
ORDER = ("hk", "uk", "fr")                      # 香港 → 伦敦 → 巴黎
ACTION_ZH = {"submit": "提交", "approve": "批准", "return": "退回", "reject": "拒绝", "restrict": "限定地区批准",
             "exception": "例外处理", "acknowledge": "确认规则命中", "comment": "意见", "complete": "完成部署核对"}
STATUS_ZH = {"in_review": "审批中", "returned": "已退回", "rejected": "已拒绝", "approved": "已获批", "approved_restricted": "已获批（限定地区）"}


class ApprovalError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _dump(x) -> bytes:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def code_fingerprint() -> str:
    """平台代码指纹：factopia 包内全部 .py 的 sha256（容器内没有 .git；FACTOPIA_GIT_COMMIT 设置时一并记录）。"""
    h = hashlib.sha256()
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(str(p.relative_to(PKG)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 路线

def route(markets: list[str], hits: list[dict], model_exceptions: list[str]) -> list[dict]:
    """审批路线：每一步一个角色，附带该角色须处理的规则命中（警告级）与模型例外。"""
    ms = [m for m in ORDER if m in markets]
    warn = [h for h in hits if h["severity"] == "warn"]

    def owned(role: str) -> list[str]:
        return sorted({f"{h['rule_id']}@{h['market']}" for h in warn if role in h["approvers"]})
    steps = [{"role": f"本地研究员（{MARKET_ZH[m]}）", "market": m, "duty": "本地数据定义、语言理解、市场适用性"} for m in ms]
    steps.append({"role": "数据负责人", "duty": "数据来源、质量、血缘、许可证、跨地区访问权限"})
    for m in ms:
        if owned(f"{MARKET_ZH[m]}合规"):
            steps.append({"role": f"{MARKET_ZH[m]}合规", "market": m, "duty": "本地区规则命中项与例外"})
    steps.append({"role": "模型风险", "duty": "稳健性、模型局限、模型例外"})
    steps.append({"role": "投资组合经理", "duty": "最终决定：批准、退回、限定地区或拒绝"})
    steps.append({"role": "运营 / 技术负责人", "duty": "获批版本、部署范围、监控阈值与审批对象一致"})
    for s in steps:
        s["hits"] = owned(s["role"])
        s["model_exceptions"] = model_exceptions if s["role"] == "模型风险" else []
    return steps


def roles() -> list[str]:
    return ["发起研究员", "本地研究员（香港）", "本地研究员（英国）", "本地研究员（法国）", "数据负责人",
            "香港合规", "英国合规", "法国合规", "模型风险", "投资组合经理", "运营 / 技术负责人"]


# ---------------------------------------------------------------- 存储

def _dir(sid: str) -> Path:
    if not sid.replace("_", "").isalnum():
        raise ApprovalError("提交编号无效")
    return DIR / sid


def load(sid: str) -> dict:
    p = _dir(sid) / "submission.json"
    if not p.exists():
        raise ApprovalError("提交不存在")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(sub: dict) -> None:
    d = _dir(sub["id"])
    tmp = d / "submission.json.tmp"
    tmp.write_text(json.dumps(sub, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "submission.json")


def events(sid: str) -> list[dict]:
    p = _dir(sid) / "events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def _append(sid: str, ev: dict) -> dict:
    """哈希链：每条事件记录上一条的哈希，事件本身的哈希覆盖除 hash 以外的全部字段。"""
    prev = (events(sid) or [{"hash": "0" * 64}])[-1]["hash"]
    ev = {**ev, "at": now(), "prev": prev}
    ev["hash"] = _sha(_dump({k: v for k, v in ev.items() if k != "hash"}))
    with (_dir(sid) / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def verify_chain(sid: str) -> dict:
    prev = "0" * 64
    for i, ev in enumerate(events(sid)):
        if ev["prev"] != prev or _sha(_dump({k: v for k, v in ev.items() if k != "hash"})) != ev["hash"]:
            return {"ok": False, "broken_at": i}
        prev = ev["hash"]
    return {"ok": True, "events": len(events(sid)), "head": prev}


def list_all(research_id: str | None = None) -> list[dict]:
    if not DIR.exists():
        return []
    out = []
    for p in sorted(DIR.glob("*/submission.json"), reverse=True):
        s = json.loads(p.read_text(encoding="utf-8"))
        if research_id and s["research_id"] != research_id:
            continue
        out.append(summary(s))
    return out


def summary(s: dict) -> dict:
    cur = s["steps"][s["current"]] if s["status"] == "in_review" and s["current"] < len(s["steps"]) else None
    last = s.get("updated_at") or s["created_at"]
    wait_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
    return {k: s[k] for k in ("id", "research_id", "version", "status", "markets", "created_at", "fingerprint")} | {
        "status_zh": STATUS_ZH[s["status"]], "current_role": cur["role"] if cur else None, "waiting_hours": round(wait_h, 1),
        "step": s["current"], "steps": len(s["steps"]), "approved_markets": s.get("approved_markets"), "title": s["materials"]["title"]}


def handling(research_id: str, version: int) -> dict | None:
    """风控台显示规则命中的处理状态（第 14.12 节）：取该研究版本最新一次提交，列出每条命中由哪些角色处理、结论与理由。
    键为 规则编号@市场（与审批步骤的 hits 一致）；提交时冻结的命中（frozen）用于发现提交后材料或规则的变化。"""
    subs = [x for x in list_all(research_id) if x["version"] == version]
    if not subs:
        return None
    s = load(subs[0]["id"])
    pre = s["materials"]["prechecks"]
    frozen = sorted({f"{h['rule_id']}@{h['market']}" for st in ("setup", "portfolio") for h in ((pre.get(st) or {}).get("hits") or [])})
    items: dict[str, list[dict]] = {}
    for st in s["steps"]:
        for k in st["hits"]:
            d = s["rule_decisions"].get(f"{st['role']}|{k}")
            items.setdefault(k, []).append({"role": st["role"], "action": d["action"] if d else None,
                                            "action_zh": ACTION_ZH.get(d["action"]) if d else None,
                                            "note": d.get("note") if d else None, "at": d.get("at") if d else None,
                                            "by": d.get("by") if d else None})
    return {"submission_id": s["id"], "status": s["status"], "status_zh": STATUS_ZH[s["status"]], "created_at": s["created_at"],
            "items": items, "frozen": frozen, "earlier_submissions": len(subs) - 1}


# ---------------------------------------------------------------- 提交

FACTOR_KEYS = ("name", "description", "formulation", "code", "kind", "dataset", "source", "simulated")   # kind = text：文本信号


def preconditions(m: dict) -> list[str]:
    """WB-05：提交前的固定顺序检查；返回未满足的条件。"""
    bad = []
    if not m.get("spec", {}).get("locked_at"):
        bad.append("研究规格尚未锁定")
    if not any((e.get("focus") or {}) for e in m["study"]["markets"].values()):
        bad.append("还没有完成的运行（没有采纳轮次）")
    if not m.get("portfolio"):
        bad.append("尚未构建授权组合（组合台）")
    pre = m["rules"].get("portfolio")
    if not pre:
        bad.append("尚未完成组合预检")
    elif pre["blocked"]:
        bad.append("组合预检命中阻断规则：" + "、".join(h["rule_id"] for h in pre["hits"] if h["severity"] == "block"))
    if m["rules"]["setup"]["blocked"]:
        bad.append("立项预检命中阻断规则")
    if not m.get("report"):
        bad.append("尚未生成研究报告（组合台 → 生成研究报告）")
    if m.get("text_pending"):
        bad.append(f"研究使用文本数据，情报台还有 {m['text_pending']} 条待复核的低置信度翻译")
    return bad


def submit(m: dict, by: str, note: str = "") -> dict:
    """m：提交材料（由接口层收集：spec、study、rules、portfolio、report、text_pending 等）。"""
    bad = preconditions(m)
    if bad:
        raise ApprovalError("还不能提交：" + "；".join(bad))
    spec, st = m["spec"], m["study"]
    ms = [x for x in ORDER if x in spec["markets"]]
    hits = m["rules"]["setup"]["hits"] + m["rules"]["portfolio"]["hits"]
    model_exc = [mk for mk, e in st["markets"].items()
                 if ((e.get("focus") or {}).get("credibility") or {}).get("state") not in (None, "credible")]
    report_bytes = m["report"]["path"].read_bytes()
    packs = {p.name: p.read_bytes() for p in sorted((CONFIGS / "rules").glob("*.yaml"))}
    factors = {}
    for mk, e in st["markets"].items():
        f = e.get("focus")
        if f:
            rounds = json.loads((RUNTIME / "results" / "runs" / e["run_id"] / "rounds.json").read_text(encoding="utf-8"))
            r = next((x for x in rounds if x["loop"] == f["loop"]), {})
            factors[mk] = [{k: x[k] for k in FACTOR_KEYS if k in x} for x in r.get("factors") or []]
    q = m.get("quality") or {}
    materials = {
        "title": spec.get("title_zh") or spec["id"], "hypothesis": spec.get("hypothesis"), "hypothesis_original": spec.get("hypothesis_original"),
        "rationale": spec.get("rationale"), "falsification": spec.get("falsification"), "mandate": spec.get("mandate"),
        "parameters": {"horizon": spec.get("horizon"), "portfolio_terms": m["portfolio"]["terms"], "periods": {
            mk: (json.loads((RUNTIME / "results" / "runs" / e["run_id"] / "run.json").read_text()).get("settings") if e.get("run_id") else None)
            for mk, e in st["markets"].items()}},
        "data_snapshot": {mk: (q.get("markets") or {}).get(mk, {}).get("sha256") for mk in ms},
        "results": {mk: {"run_id": e.get("run_id"), "loop": (e.get("focus") or {}).get("loop"),
                         "rank_ic": ((e.get("focus") or {}).get("metrics") or {}).get("rank_ic"),
                         "rank_ic_t": ((e.get("focus") or {}).get("significance") or {}).get("t"),
                         "credibility": ((e.get("focus") or {}).get("credibility") or {}).get("label"),
                         # 收益：单次回测与多次回测（并列排序种子）的中位数和区间；打分并列（第 14.7 节）
                         "return_single": ((e.get("focus") or {}).get("metrics") or {}).get("annual_return_with_cost"),
                         "return_median": (((e.get("focus") or {}).get("credibility") or {}).get("noise") or {}).get("median"),
                         "return_range": (((e.get("focus") or {}).get("credibility") or {}).get("noise") or {}).get("range"),
                         "ties": ((e.get("focus") or {}).get("ties") or {}).get("cutoff_tied_median"),
                         "kind": e.get("kind", "rdagent"), "simulated": bool((e.get("price_source") or {}).get("simulated")),
                         "insufficient": e.get("insufficient")} for mk, e in st["markets"].items()},
        "transfer": st.get("transfer"), "overall": st.get("overall"),
        "portfolio": {k: m["portfolio"][k] for k in ("built_at", "base_currency", "period", "metrics", "attribution", "fx_split", "feedback")},
        "prechecks": {"setup": m["rules"]["setup"], "portfolio": m["rules"]["portfolio"]},
        "report": {"name": m["report"]["name"], "sha256": _sha(report_bytes)},
        "models": {"draft": (spec.get("ai_draft") or {}).get("model"), "draft_prompt": (spec.get("ai_draft") or {}).get("prompt_version"),
                   "explain": (m.get("explanation") or {}).get("model"), "explain_prompt": (m.get("explanation") or {}).get("prompt_version"),
                   "rdagent": os.environ.get("CHAT_MODEL") or os.environ.get("FACTOPIA_LLM_MODEL")},
        "rule_packs": {k: _sha(v) for k, v in packs.items()},
        "code_fingerprint": code_fingerprint(), "git_commit": os.environ.get("FACTOPIA_GIT_COMMIT"),
        "translations": m.get("translations"),
        "idea": None,
    }
    if (spec.get("ai_draft") or {}).get("idea_id"):           # 研究线索来源（第 17.6 节）
        from factopia import ideas
        materials["idea"] = ideas.lineage(spec["ai_draft"]["idea_id"])
    tr_files = {}
    if (m.get("translations") or {}).get("datasets"):         # 冻结翻译：生效译文、概念、复核记录、术语表（第 14.22 节）
        from factopia.text import review as treview
        frozen, tr_files = treview.snapshot(m["translations"]["datasets"])
        materials["translations"] = {**m["translations"], "frozen": frozen}
    fp = _sha(_dump(materials))
    sid = f"{spec['id']}_v{spec['version']}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    d = _dir(sid)
    (d / "materials").mkdir(parents=True, exist_ok=True)
    (d / "materials" / m["report"]["name"]).write_bytes(report_bytes)
    (d / "materials" / "spec.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / "materials" / "factors.json").write_text(json.dumps(factors, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in tr_files.items():
        (d / "materials" / "translations").mkdir(exist_ok=True)
        (d / "materials" / "translations" / k).write_bytes(v)
    for k, v in packs.items():
        (d / "materials" / "rules").mkdir(exist_ok=True)
        (d / "materials" / "rules" / k).write_bytes(v)
    steps = route(ms, hits, model_exc)
    sub = {"id": sid, "research_id": spec["id"], "version": spec["version"], "markets": ms, "status": "in_review", "current": 0,
           "steps": [s | {"state": "pending"} for s in steps], "created_at": now(), "created_by": by, "fingerprint": fp,
           "materials": materials, "rule_decisions": {}, "approved_markets": None, "conditions": [], "lock": None}
    _save(sub)
    _append(sid, {"action": "submit", "role": "发起研究员", "by": by, "note": note, "fingerprint": fp})
    return sub


# ---------------------------------------------------------------- 地区适用性标签

def labels(s: dict) -> dict:
    """获批信号的地区适用性标签（BR-29、30；业务 spec 示例："香港 / 英国 / 法国通用""仅适用于香港"
    "法国市场需调整参数""英国市场待合规确认"；第 14.22 节）。

    - 总标签：获批且非模拟数据的市场 = 三个市场 → "香港 / 英国 / 法国通用"；否则 "仅适用于 …"；没有 → "不适用于生产"。
    - 每个市场：模拟数据 → 不适用（仅演示）；不在获批市场 → 不适用（限定地区）；获批市场再加限定：
      需调整参数：该市场采纳轮次的 t 值（方向对齐）只达到迁移门槛的一半到门槛之间（迁移标签的"需调参"区间）；
      待合规确认：提交时冻结的该市场规则包状态不是"已确认"（POC 规则包均为"待本地合规确认"），
      或该市场合规对规则命中做了例外处理、或批准时附加了条件。
    """
    mt = s["materials"]
    approved = s.get("approved_markets") or []
    per_t = ((mt.get("transfer") or {}).get("markets") or {})
    thr = (mt.get("transfer") or {}).get("threshold") or 2.0
    d = _dir(s["id"]) / "materials" / "rules"
    out, prod = {}, []
    for m in s["markets"]:
        res = mt["results"].get(m) or {}
        if res.get("simulated"):
            out[m] = {"label": "不适用（模拟数据，仅用于流程演示）", "tags": ["模拟数据"], "reasons": ["行情为模拟数据，不得用于生产"]}
            continue
        if m not in approved:
            out[m] = {"label": "不适用（投资组合经理限定地区）" if s.get("approved_markets") is not None else "未获批",
                      "tags": [], "reasons": []}
            continue
        tags, why = [], []
        t = (per_t.get(m) or {}).get("t_aligned")
        if t is not None and thr / 2 < t <= thr:
            tags.append("需调整参数")
            why.append(f"t 值 {t:.2f} 介于迁移门槛的一半与门槛（{thr}）之间")
        pack = yaml.safe_load((d / f"{m}.yaml").read_text(encoding="utf-8")) if (d / f"{m}.yaml").exists() else {}
        comp = f"{MARKET_ZH[m]}合规"
        exc = [v["rule"] for k, v in s.get("rule_decisions", {}).items() if k.startswith(comp + "|") and v["action"] == "exception"]
        cond = [c["note"] for c in s.get("conditions") or [] if c["role"] == comp]
        if (pack.get("status") or "") != "已确认" or exc or cond:
            tags.append("待合规确认")
            why += ([f"规则包 {pack.get('version', '')} 状态：{pack.get('status')}"] if (pack.get("status") or "") != "已确认" else []) \
                + ([f"合规例外：{'、'.join(exc)}"] if exc else []) + ([f"合规附加条件：{'；'.join(cond)}"] if cond else [])
        prod.append(m)
        out[m] = {"label": "适用" + ("（" + "、".join(f"{MARKET_ZH[m]}市场{x}" for x in tags) + "）" if tags else ""),
                  "tags": tags, "reasons": why}
    names = [MARKET_ZH[m] for m in ORDER if m in prod]
    head = ("香港 / 英国 / 法国通用" if len(prod) == 3 else "仅适用于" + "、".join(names) if prod else "不适用于生产")
    flags = [f"{MARKET_ZH[m]}市场{x}" for m in ORDER if m in prod for x in out[m]["tags"]]
    return {"headline": head, "flags": flags, "display": "；".join([head] + flags), "markets": out}


# ---------------------------------------------------------------- 审批操作

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ApprovalError(msg)


def act(sid: str, role: str, by: str, action: str, note: str = "", rule: str | None = None,
        markets: list[str] | None = None, thresholds: dict | None = None, current_fingerprint: str | None = None) -> dict:
    s = load(sid)
    _require(action in ACTION_ZH and action != "submit", f"操作无效：{action}")
    if action == "comment":                                  # 意见：任何角色、任何时候
        _require(bool(note.strip()), "意见不能为空")
        _append(sid, {"action": action, "role": role, "by": by, "note": note})
        return s
    _require(s["status"] == "in_review", f"该提交已结束（{STATUS_ZH[s['status']]}）")
    step = s["steps"][s["current"]]
    _require(role == step["role"], f"当前应由「{step['role']}」处理，不能跳步（WB-05）")
    ev = {"action": action, "role": role, "by": by, "note": note, "step": s["current"]}

    if action in ("exception", "acknowledge"):
        _require(rule in step["hits"] + [f"MODEL@{m}" for m in step["model_exceptions"]], "该规则命中不由本角色处理")
        _require(action == "acknowledge" or bool(note.strip()), "例外处理须写明理由")
        s["rule_decisions"][f"{role}|{rule}"] = {"rule": rule, "action": action, "by": by, "role": role, "note": note, "at": now()}
        ev["rule"] = rule
    elif action in ("return", "reject"):
        _require(bool(note.strip()), f"{ACTION_ZH[action]}须写明原因")
        if action == "reject":
            _require(role in ("投资组合经理", "模型风险") or role.endswith("合规"), "只有合规、模型风险与投资组合经理可以拒绝")
        step["state"] = action
        s["status"] = "returned" if action == "return" else "rejected"
    elif action in ("approve", "restrict"):
        _require(role != "运营 / 技术负责人", "运营 / 技术负责人请使用「完成部署核对」")
        todo = [h for h in step["hits"] + [f"MODEL@{m}" for m in step["model_exceptions"]] if f"{role}|{h}" not in s["rule_decisions"]]
        _require(not todo, "批准前须逐条处理本角色负责的规则命中 / 模型例外：" + "、".join(todo))
        if action == "restrict":
            _require(role == "投资组合经理", "只有投资组合经理可以限定地区")
            _require(bool(markets) and set(markets) < set(s["markets"]), "限定地区须选择部分市场")
            s["approved_markets"] = [m for m in ORDER if m in markets]
            ev["markets"] = s["approved_markets"]
        elif role == "投资组合经理":
            s["approved_markets"] = list(s["markets"])
        if note.strip() and action == "approve" and role != "投资组合经理":
            s["conditions"].append({"role": role, "note": note})
        step["state"] = "approved"
        s["current"] += 1
    elif action == "complete":
        _require(role == "运营 / 技术负责人", "只有运营 / 技术负责人可以完成部署核对")
        _require(current_fingerprint == s["fingerprint"], "当前材料与提交时不一致（组合、报告或规则已变化）：须重新提交")
        _require(sorted(markets or []) == sorted(s["approved_markets"] or []), "部署范围须与获批市场一致：" + "、".join(MARKET_ZH[m] for m in s["approved_markets"] or []))
        _require(bool(thresholds), "须填写监控阈值")
        step["state"] = "approved"
        s["current"] += 1
        restricted = set(s["approved_markets"]) != set(s["markets"])
        s["status"] = "approved_restricted" if restricted else "approved"
        s["lock"] = {"locked_at": now(), "fingerprint": s["fingerprint"], "report": s["materials"]["report"],
                     "code_fingerprint": s["materials"]["code_fingerprint"], "rule_packs": s["materials"]["rule_packs"],
                     "data_snapshot": s["materials"]["data_snapshot"], "deployment": s["approved_markets"], "thresholds": thresholds,
                     "translations": {ds: {k: v[k] for k in ("labels_sha256", "reviews_sha256", "model", "prompt_version", "glossary_version")}
                                      for ds, v in ((s["materials"].get("translations") or {}).get("frozen") or {}).items() if isinstance(v, dict)},
                     "applicability": None, "labels": None,
                     "conditions": s["conditions"], "by": by}
        lab = labels(s)
        s["lock"] |= {"applicability": {m: v["label"] for m, v in lab["markets"].items()}, "labels": lab}
        ev |= {"markets": s["approved_markets"], "thresholds": thresholds, "labels": lab["display"]}
    s["updated_at"] = now()
    _save(s)
    _append(sid, ev)
    return s


# ---------------------------------------------------------------- 审计证据包

def evidence_zip(sid: str) -> tuple[str, bytes]:
    s = load(sid)
    d = _dir(sid)
    evs = events(sid)
    chain = verify_chain(sid)
    files: dict[str, bytes] = {
        "submission.json": json.dumps(s, ensure_ascii=False, indent=1).encode("utf-8"),
        "events.jsonl": (d / "events.jsonl").read_bytes(),
        "chain_verification.json": json.dumps(chain, ensure_ascii=False, indent=1).encode("utf-8"),
    }
    for p in sorted((d / "materials").rglob("*")):
        if p.is_file():
            files[f"materials/{p.relative_to(d / 'materials').as_posix()}"] = p.read_bytes()
    src = CONFIGS / "rules" / "data_sources.yaml"
    if src.exists() and "materials/rules/data_sources.yaml" not in files:
        files["materials/rules/data_sources.yaml"] = src.read_bytes()
    who = [f"- {e['at']} {e['role']}{'' if e['role'] in e['by'] else '（' + e['by'] + '）'} {ACTION_ZH[e['action']]}" + (f" [{e['rule']}]" if e.get("rule") else "")
           + (f"：{e['note']}" if e.get("note") else "") for e in evs]
    mt = s["materials"]
    readme = "\n".join([
        f"# 审计证据包 · {s['id']}", "",
        f"研究：{mt['title']}（{s['research_id']} v{s['version']}）；状态：{STATUS_ZH[s['status']]}；获批市场：{'、'.join(MARKET_ZH[m] for m in s.get('approved_markets') or []) or '—'}",
        f"提交指纹（材料整体 sha256）：{s['fingerprint']}", "",
        *([f"研究线索：{mt['idea']['id']}（{mt['idea']['channel_zh']}，{mt['idea']['submitted_by']} 提交于 {mt['idea']['created_at'][:10]}：{mt['idea']['title']}）"
           + (f"；文档 {mt['idea']['document']['source']}（{mt['idea']['document']['licence_zh']}），sha256 {(mt['idea']['document']['sha256'] or '')[:16]}…" if mt['idea'].get('document') else ""), ""]
          if mt.get("idea") else []),
        "## 当时使用了什么数据",
        *[f"- {MARKET_ZH[m]} 标准化行情 sha256：{v}" for m, v in mt["data_snapshot"].items()],
        "- 数据来源与许可：materials/rules/data_sources.yaml", "",
        "## 当时使用了什么规则",
        *[f"- {k}：sha256 {v}" for k, v in mt["rule_packs"].items()],
        f"- 立项预检：阻断 {mt['prechecks']['setup']['counts']['block']} / 警告 {mt['prechecks']['setup']['counts']['warn']}；"
        f"组合预检：阻断 {mt['prechecks']['portfolio']['counts']['block']} / 警告 {mt['prechecks']['portfolio']['counts']['warn']}", "",
        "## 当时使用了什么模型与代码",
        f"- 起草：{mt['models']['draft']}（提示词 {mt['models']['draft_prompt']}）；AI 解释：" + (f"{mt['models']['explain']}（{mt['models']['explain_prompt']}）" if mt['models']['explain'] else "未生成"),
        "- 因子代码：materials/factors.json；研究规格：materials/spec.yaml",
        *([f"- 翻译（{ds}{'，模拟数据' if v.get('simulated') else ''}）：{v['paragraphs']} 段，已复核 {v['reviewed']}、待复核 {v['pending']}；"
           f"模型 {'/'.join(v['model']) or '—'}，提示词 {'/'.join(v['prompt_version']) or '—'}，术语表 {'/'.join(v['glossary_version']) or '—'}；"
           f"materials/translations/{ds}.labels.jsonl.gz（sha256 {v['labels_sha256'][:16]}…）"
           for ds, v in ((mt.get("translations") or {}).get("frozen") or {}).items() if isinstance(v, dict)]),
        f"- 平台代码指纹：{mt['code_fingerprint']}" + (f"；Git 提交 {mt['git_commit']}" if mt.get("git_commit") else ""), "",
        "## 由谁批准",
        *who, "",
        f"事件哈希链校验：{'通过' if chain['ok'] else '失败（第 ' + str(chain.get('broken_at')) + ' 条起被改动）'}；共 {len(evs)} 条",
        "", "（模拟审批：审批人为演示角色，不涉及真实人员；不构成投资建议或法律结论。）"])
    files["README.md"] = readme.encode("utf-8")
    manifest = {name: {"sha256": _sha(b), "bytes": len(b)} for name, b in sorted(files.items())}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, b in files.items():
            z.writestr(f"{sid}/{name}", b)
    return f"audit_{sid}.zip", buf.getvalue()


def current_fingerprint(sid: str, m: dict) -> str:
    """按提交时的方法重新计算当前材料的关键部分，与提交指纹比较（材料变了就不能完成部署核对）。"""
    s = load(sid)
    mt = s["materials"]                                        # 报告文件已冻结在 materials/，之后生成的新报告不影响
    changed = []
    if (m.get("portfolio") or {}).get("built_at") != mt["portfolio"]["built_at"]:
        changed.append("组合")
    if {p.name: _sha(p.read_bytes()) for p in sorted((CONFIGS / "rules").glob("*.yaml"))} != mt["rule_packs"]:
        changed.append("规则包")
    return s["fingerprint"] if not changed else "changed:" + "、".join(changed)
