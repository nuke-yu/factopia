"""AI 调研（WF-03）：线索 → 研究立项前的有界调研循环（docs/Agentic-workflow-线索-AI调研.md）。

PLAN → ACT 循环，最多 max_rounds 轮；无子任务、无人工闸门状态：
- plan：Pro LLM 从工具白名单选下一步（search_internal / search_external / evidence / draft / give_up）；
- 检索类动作由确定性代码执行（复用 internal_knowledge / websearch / evidence 证据包），结果编入事实清单 [F#]；
- draft：Pro LLM 复用 research.draft.make_draft 起草研究规格，产出进入现有审核页；
- give_up：Lite LLM 写「暂不可立项」结论（调用失败时代码兜底）。

与 WF-02（agents/loop.py）同款基础设施：文件检查点、trace.jsonl、费用账本、runner 任务取消。
记录：runtime/briefs/<brief_id>/（brief.json、rounds/<n>/、trace.jsonl）。
"""
import json
import math
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from factopia.agents import llm
from factopia.lang import llm_language
from factopia.research import draft, evidence

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
if not RUNTIME.exists():                       # 本地直接运行（不在容器内）时回退到仓库的 runtime/
    RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
BRIEFS_DIR = RUNTIME / "briefs"
BRIEF_ID = re.compile(r"^brf_\d{14}_[0-9a-f]{4}$")
API_UID = int(os.environ.get("FACTOPIA_API_UID", "10001"))   # runner 以 root 运行；新文件交给 API 用户读写

VALID_MARKETS = ("hk", "uk", "fr")
VALID_HORIZONS = (1, 5, 20, 60)
TERMINAL = ("done", "not_researchable", "stopped", "failed")
TEST_YEAR = re.compile(r"\b20(?:2[2-9]|[3-9]\d)\b")

TOOL_DESC = {
    "search_internal": "Search the platform's internal knowledge (factor library, past research, field dictionary) "
                       "with a query. args: {\"query\": \"...\"}",
    "search_external": "Search the web for public material dated before the test period (platform keeps only "
                       "pre-2022, reliably dated sources). args: {\"query\": \"...\"}",
    "evidence": "Assemble the deterministic evidence pack: Alpha158 pre-test findings and past run hypotheses "
                "for given markets. args: {\"markets\": [\"hk\"], \"horizon\": 20}",
    "draft": "Evidence is sufficient: draft the research spec (rs-draft-v4) for the human review page. "
             "args (all optional): {\"hypothesis\": \"...\", \"direction\": \"...\", \"markets\": [\"hk\"], \"horizon\": 20}",
    "give_up": "The idea cannot become a research project with the platform's data: "
               "args: {\"reason\": \"...\", \"suggestion\": \"...\"}",
}


class BriefError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return f"brf_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _dumps(obj, indent: int | None = None) -> str:
    return json.dumps(_json_safe(obj), ensure_ascii=False, indent=indent, allow_nan=False)


def _own(path: Path) -> None:
    """runner 以 root 写文件时，把文件与目录交给 API 用户（10001）。"""
    if os.geteuid() != 0:
        return
    try:
        for p in (path, path.parent):
            os.chown(p, API_UID, API_UID)
            if p.is_dir():
                os.chmod(p, 0o775)
            else:
                os.chmod(p, 0o664)
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    _own(path)


# ---------------------------------------------------------------- 存储

def dir_path(brief_id: str) -> Path:
    if not BRIEF_ID.match(brief_id or ""):
        raise BriefError(f"调研编号无效：{brief_id}")
    return BRIEFS_DIR / brief_id


def create(idea_id: str, idea: dict, policy: dict) -> dict:
    """新建一条 AI 调研记录；同一线索不能有第二个进行中的调研（幂等）。"""
    if not isinstance(idea, dict) or not idea.get("title"):
        raise BriefError("线索不存在或没有标题")
    for rec in list_briefs():
        if rec.get("idea_id") == idea_id and rec.get("state") not in TERMINAL:
            raise BriefError("该线索已有进行中的 AI 调研")
    ai = idea.get("ai_hypothesis") or {}
    markets = [m for m in (idea.get("markets") or []) if m in VALID_MARKETS] or list(VALID_MARKETS)
    rec = {
        "brief_id": new_id(), "idea_id": idea_id, "state": "queued",
        "created_at": now(), "updated_at": now(),
        "idea": {
            "title": str(idea.get("title") or "").strip()[:200],
            "body": str(idea.get("body") or "").strip()[:2000],
            "channel": idea.get("channel"),
            "channel_zh": idea.get("channel_zh"),
            "markets": markets,
            "hypothesis": str(ai.get("hypothesis") or "").strip()[:3000],
            "direction": str(ai.get("direction") or "").strip()[:500],
        },
        "policy": policy,
        "round": 0,
        "max_rounds": int(policy.get("max_rounds") or 3),
        "facts": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "cost_usd": 0.0,
        "outcome": None,
        "draft": None,
        "conclusion": None,
        "stop_reason": None,
        "last_error": None,
    }
    save(rec)
    append_trace(rec["brief_id"], {"kind": "state", "from": None, "to": "queued", "note": "创建 AI 调研"})
    return rec


def save(rec: dict) -> None:
    rec["updated_at"] = now()
    _atomic_write(dir_path(rec["brief_id"]) / "brief.json", _dumps(rec, indent=1))


def load(brief_id: str) -> dict:
    path = dir_path(brief_id) / "brief.json"
    if not path.exists():
        raise BriefError(f"AI 调研不存在：{brief_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_briefs() -> list[dict]:
    if not BRIEFS_DIR.exists():
        return []
    out = []
    for path in sorted(BRIEFS_DIR.glob("brf_*/brief.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda b: b.get("created_at") or "", reverse=True)


def latest(idea_id: str) -> dict | None:
    for rec in list_briefs():
        if rec.get("idea_id") == idea_id:
            return rec
    return None


def round_dir(brief_id: str, n: int) -> Path:
    return dir_path(brief_id) / "rounds" / f"{int(n):03d}"


def write_round(brief_id: str, n: int, name: str, data) -> Path:
    path = round_dir(brief_id, n) / f"{name}.json"
    _atomic_write(path, _dumps(data, indent=1))
    return path


def read_round(brief_id: str, n: int, name: str) -> dict | None:
    path = round_dir(brief_id, n) / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def append_trace(brief_id: str, entry: dict) -> None:
    record = {"at": now(), **entry}
    path = dir_path(brief_id) / "trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(_dumps(record) + "\n")
    _own(path)


def add_usage(rec: dict, usage: dict | None) -> float:
    """累计 token 与费用；draft._chat 的 usage 含 usd，make_draft 的 meta 只有 token（按单价换算）。"""
    usage = usage or {}
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    usd = float(usage.get("usd") or 0.0)
    if not usd and (pt or ct):
        from factopia import llm_cost
        usd = llm_cost.usd(pt, ct)
    rec["usage"]["prompt_tokens"] += pt
    rec["usage"]["completion_tokens"] += ct
    rec["cost_usd"] = round(float(rec.get("cost_usd") or 0.0) + usd, 6)
    save(rec)
    return usd


def _advance(rec: dict, target: str, note: str | None = None) -> None:
    current = rec.get("state")
    rec["state"] = target
    if target in TERMINAL and note:
        rec["stop_reason"] = note
    save(rec)
    append_trace(rec["brief_id"], {"kind": "state", "from": current, "to": target,
                                   **({"note": note} if note else {})})


# ---------------------------------------------------------------- 事实清单

def _facts_text(facts: list[dict]) -> str:
    if not facts:
        return "- none"
    lines = []
    for f in facts:
        when = f" ({f.get('published')})" if f.get("published") else ""
        url = f" — {f.get('url')}" if f.get("url") else ""
        lines.append(f"[{f['id']}] ({f.get('source')}{when}) {f.get('title')}: {f.get('text')}{url}")
    return "\n".join(lines)


def _idea_text(idea: dict) -> str:
    lines = [f"title: {idea['title']}",
             f"channel: {idea.get('channel_zh') or idea.get('channel')}",
             f"markets: {', '.join(idea['markets'])}"]
    if idea.get("hypothesis"):
        lines.append(f"ai_hypothesis: {idea['hypothesis']}")
    if idea.get("direction"):
        lines.append(f"direction: {idea['direction']}")
    if idea.get("body"):
        lines.append(f"body: {idea['body']}")
    return "\n".join(lines)


def _data_text() -> str:
    """平台数据可得性（复用研究规格起草的 DATA 表）；Planner 不能围绕没有的数据做规划。"""
    lines = []
    for cat, (name, _status, usable) in draft.DATA.items():
        lines.append(f"- {name} ({cat}): {'available' if usable else 'NOT available'}")
    return "\n".join(lines)


def _add_facts(rec: dict, items: list[dict]) -> list[str]:
    """把新事实编入 [F#]；按（来源、标题、正文前缀）去重。"""
    added = []
    seen = {(f.get("source"), str(f.get("title") or "")[:80], str(f.get("text") or "")[:120])
            for f in rec["facts"]}
    for it in items:
        title = str(it.get("title") or "").strip()
        text = str(it.get("text") or "").strip()
        if not title and not text:
            continue
        key = (it.get("source"), title[:80], text[:120])
        if key in seen:
            continue
        seen.add(key)
        rec["facts"].append({"id": f"F{len(rec['facts']) + 1}", "at": now(), **it})
        added.append(rec["facts"][-1]["id"])
    if added:
        save(rec)
    return added


# ---------------------------------------------------------------- 工具执行（确定性代码）

def _run_internal(rec: dict, query: str) -> tuple[dict, list[dict]]:
    rows = draft.internal_knowledge(query, limit=8)
    items = [{"source": "internal", "kind": x.get("kind"), "kind_zh": x.get("kind_zh"),
              "title": x.get("title"), "text": x.get("prompt"), "ref": x.get("ref")} for x in rows]
    return {"results": len(items)}, items


def _run_external(rec: dict, query: str) -> tuple[dict, list[dict]]:
    from factopia.research import websearch
    try:
        out = websearch.search(query)
    except Exception as e:
        out = {"configured": False, "provider": None, "results": [], "error": str(e)[:500]}
    items = [{"source": "external", "kind": "web", "title": r.get("title"),
              "text": str(r.get("snippet") or "")[:500],
              "url": r.get("url"), "published": r.get("published")} for r in out.get("results") or []]
    summary = {"provider": out.get("provider"), "configured": out.get("configured"),
               "error": out.get("error"), "warning": out.get("warning"), "results": len(items)}
    return summary, items


def _run_evidence(rec: dict, markets: list[str] | None, horizon: int | None) -> tuple[dict, list[dict]]:
    ms = [m for m in (markets or []) if m in VALID_MARKETS] or rec["idea"]["markets"]
    hz = int(horizon or rec["policy"].get("horizon") or 20)
    items = []
    for e in evidence.alpha158_findings(ms, hz):
        factors = e.get("factors") or []
        lines = [f"{e['market'].upper()} (period {e['period']['start']} to {e['period']['end']}; "
                 f"{e['significant']} significant factors with |t| > 3):"]
        lines += [f"- {f['name']} ({f['desc']}): rank IC {f['ric']:.3f}, t={f['t']:.2f}" for f in factors] or \
                 ["- none significant"]
        items.append({"source": "evidence", "kind": "alpha158", "title": f"Alpha158 测试期前发现（{e['market']}）",
                      "text": "\n".join(lines), "ref": e.get("source")})
    past = evidence.past_runs(ms, hz)
    if past:
        items.append({"source": "evidence", "kind": "past_runs", "title": "以往运行（只含方向与是否采纳）",
                      "text": "\n".join(evidence.past_runs_lines(past))})
    return {"alpha158_markets": len([x for x in items if x["kind"] == "alpha158"]), "past_runs": len(past)}, items


# ---------------------------------------------------------------- Planner

def _tools_text(policy: dict) -> tuple[str, list[str]]:
    available = [t for t in (policy.get("tools") or []) if t in TOOL_DESC]
    return "\n".join(f"- {t}: {TOOL_DESC[t]}" for t in available), available


def _clean_query(q) -> str:
    q = re.sub(TEST_YEAR, " ", str(q or ""))      # 不把测试期年份带进检索
    q = re.sub(r"\[[FWI]\d+\]", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:300]


def _validate_plan(data: dict, fact_ids: set[str], available: list[str]) -> dict:
    action = str(data.get("action") or "").strip()
    if action not in available:
        raise BriefError(f"动作 {action!r} 不在白名单中；可用：{', '.join(available)}")
    rationale = data.get("rationale")
    if isinstance(rationale, str):
        rationale = {"text": rationale, "cites": data.get("cites") or []}
    if not isinstance(rationale, dict) or not str(rationale.get("text") or "").strip():
        raise BriefError("rationale.text 不能为空")
    cites = [str(c) for c in (rationale.get("cites") or []) if str(c) in fact_ids or str(c) == "I1"]
    kept = {"text": str(rationale["text"]).strip()[:1000], "cites": cites}
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise BriefError("args 必须是 JSON 对象")
    if action in ("search_internal", "search_external"):
        if not cites:
            raise BriefError("rationale 必须引用 [I1] 或有效事实 [F#]")
        query = _clean_query(args.get("query"))
        if not query:
            raise BriefError(f"{action} 需要 args.query")
        return {"action": action, "rationale": kept, "args": {"query": query}}
    if action == "evidence":
        if not cites:
            raise BriefError("rationale 必须引用 [I1] 或有效事实 [F#]")
        markets = [m for m in (args.get("markets") or []) if m in VALID_MARKETS]
        horizon = args.get("horizon")
        if horizon is not None:
            try:
                horizon = int(horizon)
            except (TypeError, ValueError):
                raise BriefError("args.horizon 必须是数字") from None
            if horizon not in VALID_HORIZONS:
                raise BriefError(f"horizon 必须是 {VALID_HORIZONS} 之一")
        return {"action": "evidence", "rationale": kept, "args": {"markets": markets, "horizon": horizon}}
    if action == "draft":
        f_cites = [c for c in cites if c in fact_ids]
        if not f_cites:
            raise BriefError("draft 必须引用至少一条有效事实 [F#]（先检索一轮再起草）")
        out_args = {}
        for k in ("hypothesis", "direction"):
            v = str(args.get(k) or "").strip()
            if v:
                out_args[k] = v[:1000]
        markets = [m for m in (args.get("markets") or []) if m in VALID_MARKETS]
        if markets:
            out_args["markets"] = markets
        horizon = args.get("horizon")
        if horizon is not None:
            try:
                horizon = int(horizon)
            except (TypeError, ValueError):
                raise BriefError("args.horizon 必须是数字") from None
            if horizon not in VALID_HORIZONS:
                raise BriefError(f"horizon 必须是 {VALID_HORIZONS} 之一")
            out_args["horizon"] = horizon
        return {"action": "draft", "rationale": kept, "args": out_args}
    if action == "give_up":
        reason = str(args.get("reason") or "").strip()
        if not reason:
            raise BriefError("give_up 需要 args.reason")
        out_args = {"reason": reason[:1000]}
        suggestion = str(args.get("suggestion") or "").strip()
        if suggestion:
            out_args["suggestion"] = suggestion[:500]
        return {"action": "give_up", "rationale": kept, "args": out_args}
    raise BriefError(f"未知动作 {action!r}")


def _plan(rec: dict, chat) -> dict:
    version = rec["policy"]["prompts"]["planner"]
    tools_text, available = _tools_text(rec["policy"])
    prompt = llm.prompt(
        version, language=llm_language(), idea=_idea_text(rec["idea"]),
        tools=tools_text, facts=_facts_text(rec["facts"]), data=_data_text(),
        round=rec["round"], max_rounds=rec["max_rounds"],
        remaining=max(0, int(rec["max_rounds"]) - int(rec["round"])),
    )
    note = ""
    for attempt in range(2):
        raw, usage, model = chat(prompt + note, "AI 调研规划")
        append_trace(rec["brief_id"], {"kind": "llm", "role": "planner", "round": rec["round"],
                                       "model": model, "usage": usage, "prompt_version": version,
                                       "prompt": prompt + note, "response": raw})
        try:
            data = llm.parse_json(raw)
            out = _validate_plan(data, {f["id"] for f in rec["facts"]}, available)
            out.update({"model": model, "prompt_version": version, "usage": usage, "at": now()})
            return out
        except (llm.LLMError, BriefError) as e:
            note = f"\n\nYour previous answer was rejected: {e}. Answer again with the required JSON object."
    raise BriefError(f"Planner 两次都没有给出合法计划：{note.strip()}")


# ---------------------------------------------------------------- 起草与结论

def _draft(rec: dict, plan: dict, chat) -> None:
    """复用 make_draft 起草研究规格；外部调研事实并入 external_evidence，产出进入现有审核页。"""
    args = plan.get("args") or {}
    idea = rec["idea"]
    hypothesis = str(args.get("hypothesis") or idea.get("hypothesis") or idea.get("title") or "").strip()
    if not hypothesis:
        _give_up(rec, chat, "没有可起草的假设（线索未提供假设或标题）")
        return
    direction = str(args.get("direction") or idea.get("direction") or idea.get("title") or "").strip()
    markets = [m for m in (args.get("markets") or []) if m in VALID_MARKETS] or idea["markets"]
    horizon = int(args.get("horizon") or rec["policy"].get("horizon") or 20)
    external = [{"title": f.get("title"), "published": f.get("published"), "url": f.get("url"),
                 "snippet": f.get("text")} for f in rec["facts"] if f.get("source") == "external"][:8]
    inputs = {"hypothesis": hypothesis, "direction": direction, "markets": markets, "horizon": horizon,
              "mandate": rec["policy"].get("mandate") or "demo_crossborder_hk", "groups": [],
              "include_past_runs": True, "idea_id": rec["idea_id"], "idea_ids": [],
              "combo_id": None, "based_on": None, "external_evidence": external}

    def on_llm(prompt, response, usage, model):
        append_trace(rec["brief_id"], {"kind": "llm", "role": "draft", "round": rec["round"],
                                       "model": model, "usage": usage,
                                       "prompt_version": draft.PROMPT_VERSION,
                                       "prompt": prompt, "response": response})

    result = draft.make_draft(inputs, on_llm=on_llm)
    meta = result.get("meta") or {}
    write_round(rec["brief_id"], rec["round"], "draft", result)
    add_usage(rec, {"prompt_tokens": meta.get("prompt_tokens"), "completion_tokens": meta.get("completion_tokens")})
    rec["draft"] = {"inputs": {k: inputs[k] for k in ("hypothesis", "direction", "markets", "horizon", "mandate")},
                    "draft": result.get("draft"), "meta": meta,
                    "evidence": {"alpha158": result.get("evidence", {}).get("alpha158", []),
                                 "past_runs": result.get("evidence", {}).get("past_runs", []),
                                 "internal": result.get("evidence", {}).get("internal", [])}}
    rec["outcome"] = "draft"
    _advance(rec, "done", f"调研完成：{len(rec['facts'])} 条事实，产出研究规格草稿")


def _give_up(rec: dict, chat, reason: str) -> None:
    """Lite LLM 写「暂不可立项」结论；调用失败或预算已超限时用代码兜底，不阻断收尾。"""
    version = rec["policy"]["prompts"]["conclusion"]
    prompt = llm.prompt(version, language=llm_language(), idea=_idea_text(rec["idea"]),
                        facts=_facts_text(rec["facts"]), reason=reason)
    conclusion = {"reason": reason, "evidence_gaps": [], "suggestion": "",
                  "model": None, "at": now()}
    if not _over_budget(rec):                      # 预算已超限：不再产生新的 LLM 费用
        try:
            raw, usage, model = chat(prompt, "AI 调研结论", lite=True)
            append_trace(rec["brief_id"], {"kind": "llm", "role": "conclusion", "round": rec["round"],
                                           "model": model, "usage": usage, "prompt_version": version,
                                           "prompt": prompt, "response": raw})
            data = llm.parse_json(raw)
            conclusion = {"reason": str(data.get("reason") or reason)[:1000],
                          "evidence_gaps": [str(x)[:300] for x in (data.get("evidence_gaps") or [])][:6],
                          "suggestion": str(data.get("suggestion") or "")[:500],
                          "model": model, "prompt_version": version, "usage": usage, "at": now()}
        except Exception as e:
            append_trace(rec["brief_id"], {"kind": "conclusion_error", "round": rec["round"],
                                           "error": str(e)[:500]})
    write_round(rec["brief_id"], rec["round"], "conclusion", conclusion)
    add_usage(rec, conclusion.get("usage"))
    rec["conclusion"] = {k: conclusion.get(k) for k in ("reason", "evidence_gaps", "suggestion", "model", "at")}
    rec["outcome"] = "not_researchable"
    _advance(rec, "not_researchable", "调研结论：暂不可立项")


def _over_budget(rec: dict) -> bool:
    return float(rec.get("cost_usd") or 0.0) >= float(rec["policy"].get("budget_usd") or 0.0)


# ---------------------------------------------------------------- 主循环

def advance(rec: dict, chat=None, should_stop=None) -> dict:
    """推进调研循环直到终态；每个阶段已写检查点，中断后重新排队可续跑。"""
    chat = chat or llm.chat
    try:
        while rec.get("state") not in TERMINAL:
            if should_stop and should_stop():
                _advance(rec, "stopped", "已取消")
                break
            state = rec.get("state")
            if state == "queued":
                _advance(rec, "planning")
                continue
            if state == "planning":
                if _over_budget(rec):
                    _advance(rec, "closing", "费用达到预算上限，收尾")
                    continue
                rec["round"] = int(rec.get("round") or 0) + 1
                save(rec)
                try:
                    plan = _plan(rec, chat)
                except BriefError as e:
                    write_round(rec["brief_id"], rec["round"], "plan", {"error": str(e)})
                    _advance(rec, "closing", f"规划失败：{e}")
                    continue
                write_round(rec["brief_id"], rec["round"], "plan", plan)
                add_usage(rec, plan.get("usage"))
                _advance(rec, "acting")
                continue
            if state == "acting":
                plan = read_round(rec["brief_id"], rec["round"], "plan")
                action = _act(rec, plan, chat)
                write_round(rec["brief_id"], rec["round"], "action", action)
                if rec.get("state") in TERMINAL:
                    continue
                if int(rec["round"]) >= int(rec["max_rounds"]):
                    _advance(rec, "closing", "达到最大轮数，收尾")
                    continue
                _advance(rec, "planning")
                continue
            if state == "closing":
                _close(rec, chat)
                continue
    except Exception as e:
        rec["last_error"] = str(e)[:2000]
        if rec.get("state") not in TERMINAL:
            try:
                _advance(rec, "failed")
            except Exception:
                rec["state"] = "failed"
                save(rec)
        raise
    save(rec)
    return rec


def advance_by_id(brief_id: str, chat=None, should_stop=None) -> dict:
    """runner 入口：从磁盘加载调研记录后推进到终态。"""
    return advance(load(brief_id), chat=chat, should_stop=should_stop)


def _act(rec: dict, plan: dict, chat) -> dict:
    """执行本轮计划；检索失败不阻断循环（记录错误、消耗本轮），draft / give_up 直接终态。"""
    action = plan["action"]
    args = plan.get("args") or {}
    added, summary, error = [], {}, None
    try:
        if action == "search_internal":
            summary, items = _run_internal(rec, args["query"])
            added = _add_facts(rec, items)
        elif action == "search_external":
            summary, items = _run_external(rec, args["query"])
            added = _add_facts(rec, items)
        elif action == "evidence":
            summary, items = _run_evidence(rec, args.get("markets"), args.get("horizon"))
            added = _add_facts(rec, items)
        elif action == "draft":
            _draft(rec, plan, chat)
            return {"action": "draft", "added_facts": [], "at": now()}
        elif action == "give_up":
            _give_up(rec, chat, args.get("reason"))
            return {"action": "give_up", "added_facts": [], "at": now()}
    except Exception as e:
        error = str(e)[:800]
    out = {"action": action, "added_facts": added, "summary": summary, "error": error, "at": now()}
    append_trace(rec["brief_id"], {"kind": "tool", "round": rec["round"], "action": action,
                                   "added_facts": added, "error": error})
    return out


def _close(rec: dict, chat) -> None:
    """满轮 / 预算 / 规划失败后的收尾：有事实且假设可起草 → draft；否则 give_up。"""
    if _over_budget(rec):
        _give_up(rec, chat, "费用达到预算上限，收尾")
        return
    if not rec["facts"]:
        _give_up(rec, chat, "没有收集到可立项的证据")
        return
    _draft(rec, {"action": "draft", "args": {}}, chat)
