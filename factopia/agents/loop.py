"""OBSERVE → DIAGNOSE → PLAN → GATE → ACT → EVALUATE 的显式状态机（Spec 第 18.4 节）。

P0 是同步 CLI：只生成事实、诊断、计划，以及 new_version 的下一版草稿；
不创建子任务、不等待 runner、不接 API / 前端。状态与检查点写入 runtime/agents/<id>/。

与 LangGraph 的对应关系（Spec 第 18.8 节）：
    State=agent.json，Node=本文件的函数，Edge=VALID_TRANSITIONS，
    Checkpoint=每个节点后的原子写，Interrupt=awaiting_human，Resume=runner 重新入队（P1）。
"""
import json

from factopia.agents import llm, observe, policy as policy_mod, report, store, tools
from factopia.lang import llm_language
from factopia.research import store as research_store


class AgentStateError(RuntimeError):
    pass


VALID_TRANSITIONS = {
    "queued": {"observing", "failed"},
    "observing": {"diagnosing", "failed"},
    "diagnosing": {"planning", "failed"},
    "planning": {"awaiting_human", "acting", "done", "stopped", "failed"},
    "awaiting_human": {"acting", "stopped", "failed"},
    "acting": {"evaluating", "waiting_children", "stopped", "failed"},
    "waiting_children": {"evaluating", "stopped", "failed"},
    "evaluating": {"observing", "done", "stopped", "failed"},
    "done": set(),
    "stopped": set(),
    "failed": set(),
}


def _advance(agent: dict, target: str, note: str | None = None) -> None:
    current = agent.get("state")
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise AgentStateError(f"非法状态迁移：{current} → {target}")
    agent["state"] = target
    store.append_trace(agent["agent_id"], {"kind": "state", "from": current, "to": target,
                                           **({"note": note} if note else {})})
    store.save(agent)


def run(research_id: str, version: int, rounds: int = 1, dry_run: bool = False, approve: bool = False,
        policy: dict | None = None, chat=None) -> dict:
    """从一个研究版本启动 Agent。返回最终的 agent 记录。

    dry_run：只做事实、诊断、规划，不执行工具；approve：跳过人工闸门（演示 / 自动化）。
    chat：测试注入，签名 (prompt, task) -> (text, usage, model)。
    """
    spec = research_store.load_version(research_id, version)
    pol = policy or policy_mod.load()
    markets = list(spec.get("markets") or pol.get("markets") or ["hk", "uk", "fr"])
    agent = store.create(research_id, version, pol, markets, int(spec.get("horizon") or 20))
    memory = store.memory(agent["agent_id"])
    chat = chat or llm.chat

    try:
        for n in range(1, int(rounds) + 1):
            reason = policy_mod.should_stop(agent, memory, pol)
            if reason:
                agent["stop_reason"] = reason
                _advance(agent, "stopped")
                break
            agent["current_round"] = n
            store.save(agent)

            _advance(agent, "observing")
            facts = observe.build(research_id, version)
            store.write_round(agent["agent_id"], n, "facts", facts)
            store.append_trace(agent["agent_id"], {"kind": "facts", "round": n, "count": len(facts["facts"])})

            _advance(agent, "diagnosing")
            diagnosis = _diagnose(facts, memory, pol, chat, agent)
            store.write_round(agent["agent_id"], n, "diagnosis", diagnosis)
            store.add_usage(agent, diagnosis.get("usage"))

            _advance(agent, "planning")
            plan = _plan(facts, diagnosis, memory, pol, chat, agent)
            store.write_round(agent["agent_id"], n, "plan", plan)
            store.add_usage(agent, plan.get("usage"))

            if plan["action"] == "stop":
                agent["stop_reason"] = (plan.get("args") or {}).get("reason") or "agent stop"
                _advance(agent, "done")
                break
            if dry_run:
                agent["stop_reason"] = "dry-run：只生成事实、诊断与计划，未执行动作"
                _advance(agent, "stopped")
                break
            if policy_mod.needs_gate(pol, plan["action"]) and not approve:
                agent["stop_reason"] = f"等待人工批准动作 {plan['action']}"
                _advance(agent, "awaiting_human")
                break

            _advance(agent, "acting")
            action = tools.execute(plan["action"], plan.get("args") or {}, {
                "agent_id": agent["agent_id"], "agent": agent, "round": n,
                "research_id": research_id, "version": int(version), "spec": spec,
                "fact_ids": [f["id"] for f in facts["facts"]],
            })
            store.write_round(agent["agent_id"], n, "action", action)
            store.add_usage(agent, action.get("usage"))
            if action.get("child_jobs"):
                agent["child_jobs"] = action["child_jobs"]
                _advance(agent, "waiting_children")
                break

            _advance(agent, "evaluating")
            evaluation = _evaluate(plan, action, n)
            store.write_round(agent["agent_id"], n, "evaluation", evaluation)
            _update_memory(agent, memory, n, plan, evaluation)
            agent["stop_reason"] = ("P0：下一版草稿已生成；运行并验证新版本后再开始下一轮 Agent"
                                    if plan["action"] == "new_version" else "P0：动作完成，本轮结束")
            _advance(agent, "stopped")
            break
    except Exception as e:
        agent["last_error"] = str(e)[:2000]
        if agent.get("state") not in ("done", "stopped", "failed"):
            try:
                _advance(agent, "failed")
            except Exception:
                agent["state"] = "failed"
                store.save(agent)
        raise
    finally:
        report.write(agent)
    return agent


def _diagnose(facts: dict, memory: dict, policy: dict, chat, agent: dict) -> dict:
    version = policy["prompts"]["critic"]
    prompt = llm.prompt(version, language=llm_language(), facts=_facts_text(facts["facts"]), memory=_memory_text(memory))
    note = ""
    for attempt in range(2):
        raw, usage, model = chat(prompt + note, "研究迭代诊断")
        store.append_trace(agent["agent_id"], {"kind": "llm", "role": "critic", "model": model, "usage": usage,
                                               "prompt_version": version, "prompt": prompt + note, "response": raw})
        try:
            data = llm.parse_json(raw)
            out = _filter_diagnosis(data, {f["id"] for f in facts["facts"]})
            if out["primary_cause"] or out["market_findings"] or out["risks"]:
                out.update({"model": model, "prompt_version": version, "usage": usage, "at": store.now()})
                return out
            note = "\n\nYour previous answer had no claim with a valid fact citation. Answer again and cite F# ids."
        except llm.LLMError as e:
            note = f"\n\nYour previous answer could not be parsed: {e}. Return the JSON object only."
    raise store.AgentError("诊断两次都没有给出带有效引用的事实结论")


def _filter_diagnosis(data: dict, fact_ids: set[str]) -> dict:
    """只保留引用了存在事实的结论；无引用或引用不存在的条目丢弃（AGT-03）。"""
    dropped = 0
    primary = _keep(data.get("primary_cause"), fact_ids)
    if primary is None:
        dropped += 1
    market_findings = []
    for item in data.get("market_findings") or []:
        kept = _keep(item, fact_ids)
        if kept is None or not str(item.get("market") or "").strip():
            dropped += 1
            continue
        market_findings.append({"market": str(item["market"]).strip().lower(), **kept})
    risks = []
    for item in data.get("risks") or []:
        kept = _keep(item, fact_ids)
        if kept is None:
            dropped += 1
        else:
            risks.append(kept)
    confidence = str(data.get("confidence") or "low").lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    return {"primary_cause": primary, "market_findings": market_findings, "risks": risks,
            "confidence": confidence,
            "what_would_change_my_mind": str(data.get("what_would_change_my_mind") or "").strip()[:1000],
            "dropped": dropped}


def _keep(item, fact_ids: set[str]) -> dict | None:
    if not isinstance(item, dict):
        return None
    cites = [str(c) for c in item.get("cites") or [] if str(c) in fact_ids]
    text = str(item.get("text") or "").strip()
    if not cites or not text:
        return None
    return {"text": text[:2000], "cites": cites}


def _plan(facts: dict, diagnosis: dict, memory: dict, policy: dict, chat, agent: dict) -> dict:
    version = policy["prompts"]["planner"]
    available = policy_mod.available_tools(policy, tools.REGISTRY)
    prompt = llm.prompt(
        version, language=llm_language(), facts=_facts_text(facts["facts"]),
        diagnosis=_diagnosis_text(diagnosis),
        memory=_memory_text(memory), tools=tools.describe(policy, tools.REGISTRY),
        policy=json.dumps({k: policy.get(k) for k in ("max_rounds", "budget_usd", "no_improve_patience", "human_gate")}),
    )
    note = ""
    for attempt in range(2):
        raw, usage, model = chat(prompt + note, "研究迭代规划")
        store.append_trace(agent["agent_id"], {"kind": "llm", "role": "planner", "model": model, "usage": usage,
                                               "prompt_version": version, "prompt": prompt + note, "response": raw})
        try:
            data = llm.parse_json(raw)
            out = _validate_plan(data, {f["id"] for f in facts["facts"]}, available)
            out.update({"model": model, "prompt_version": version, "usage": usage, "at": store.now()})
            return out
        except (llm.LLMError, AgentStateError) as e:
            note = f"\n\nYour previous answer was rejected: {e}. Answer again with the required JSON object."
    raise store.AgentError("规划两次都没有给出合法动作")


def _validate_plan(data: dict, fact_ids: set[str], available: list[str]) -> dict:
    action = str(data.get("action") or "").strip()
    if action not in available:
        raise AgentStateError(f"动作 {action!r} 不在白名单中；可用：{', '.join(available)}")
    rationale = data.get("rationale")
    if isinstance(rationale, str):
        rationale = {"text": rationale, "cites": data.get("cites") or []}
    kept = _keep(rationale, fact_ids)
    if kept is None:
        raise AgentStateError("规划理由必须引用至少一条有效事实（F#）")
    args = data.get("args")
    if not isinstance(args, dict):
        raise AgentStateError("args 必须是 JSON 对象")
    if action == "stop":
        if not str(args.get("reason") or "").strip():
            raise AgentStateError("stop 需要 args.reason")
        return {"action": "stop", "rationale": kept, "args": {
            "reason": str(args["reason"])[:1000],
            "next_steps": [str(x)[:500] for x in args.get("next_steps") or []][:6]},
            "expected_outcome": None, "success_criteria": None, "alternatives_rejected": [],
            "stop_condition": "stopped"}
    changes = args.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise AgentStateError("new_version 需要 args.changes")
    criteria = data.get("success_criteria")
    if not isinstance(criteria, dict) or not criteria.get("metric"):
        raise AgentStateError("new_version 需要 success_criteria.metric")
    direction = str(criteria.get("direction") or "").lower()
    if direction not in ("increase", "decrease", "maintain", "positive", "negative"):
        raise AgentStateError("success_criteria.direction 必须是 increase / decrease / maintain / positive / negative")
    expected = str(data.get("expected_outcome") or "").strip()
    stop_condition = str(data.get("stop_condition") or "").strip()
    if not expected or not stop_condition:
        raise AgentStateError("new_version 需要 expected_outcome 与 stop_condition")
    min_delta = criteria.get("min_delta")
    try:
        min_delta = None if min_delta is None else float(min_delta)
    except (TypeError, ValueError):
        raise AgentStateError("success_criteria.min_delta 必须是数字或 null")
    args = {**args, "cites": kept["cites"]}
    return {"action": "new_version", "rationale": kept, "args": args,
            "expected_outcome": expected[:1000],
            "success_criteria": {"metric": str(criteria["metric"]), "direction": direction,
                                 "min_delta": min_delta,
                                 "guardrails": criteria.get("guardrails") if isinstance(criteria.get("guardrails"), dict) else {}},
            "alternatives_rejected": [{"action": str(x.get("action")), "why": str(x.get("why"))}
                                      for x in data.get("alternatives_rejected") or [] if isinstance(x, dict)][:6],
            "stop_condition": stop_condition[:1000]}


def _evaluate(plan: dict, action: dict, n: int) -> dict:
    if plan["action"] == "new_version":
        return {"round": n, "verdict": "inconclusive",
                "reason": "P0 只生成下一版草稿，没有运行新回测；需运行并验证后才能判定是否改进。",
                "success_criteria": plan.get("success_criteria"), "evidence": plan["rationale"]["cites"],
                "at": store.now()}
    return {"round": n, "verdict": "stopped", "reason": (plan.get("args") or {}).get("reason") or "agent stop",
            "success_criteria": plan.get("success_criteria"), "evidence": plan["rationale"]["cites"],
            "at": store.now()}


def _update_memory(agent: dict, memory: dict, n: int, plan: dict, evaluation: dict) -> None:
    memory.setdefault("attempts", []).append({
        "round": n, "action": plan["action"], "expected": plan.get("expected_outcome"),
        "result": evaluation.get("verdict"), "reason": evaluation.get("reason"), "at": store.now(),
    })
    exclusions = memory.setdefault("exclusions", [])
    for x in (plan.get("args") or {}).get("changes", {}).get("exclusions") or []:
        if x not in exclusions:
            exclusions.append(str(x)[:500])
    store.save_memory(agent["agent_id"], memory)


def _facts_text(facts: list[dict]) -> str:
    return "\n".join(f"[{f['id']}] ({f['period']}) {f['text']}" for f in facts)


def _memory_text(memory: dict) -> str:
    lines = []
    for a in (memory.get("attempts") or [])[-5:]:
        lines.append(f"- round {a.get('round')}: {a.get('action')} -> {a.get('result')}; "
                     f"expected: {a.get('expected')}; reason: {a.get('reason')}")
    ex = memory.get("exclusions") or []
    if ex:
        lines.append("- excluded directions: " + "; ".join(map(str, ex)))
    return "\n".join(lines) or "- none"


def _diagnosis_text(diagnosis: dict) -> str:
    parts = []
    primary = diagnosis.get("primary_cause")
    if primary:
        parts.append("Primary cause: " + primary["text"] + " (" + ", ".join(primary["cites"]) + ")")
    for item in diagnosis.get("market_findings") or []:
        parts.append(f"{item.get('market')}: {item['text']} ({', '.join(item['cites'])})")
    for item in diagnosis.get("risks") or []:
        parts.append("Risk: " + item["text"] + " (" + ", ".join(item["cites"]) + ")")
    parts.append("Confidence: " + str(diagnosis.get("confidence")))
    return "\n".join(parts)
