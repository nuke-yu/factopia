"""OBSERVE → DIAGNOSE → PLAN → GATE → ACT → EVALUATE 的显式状态机（Spec 第 18.4 节）。

P0 是同步 CLI：只生成事实、诊断、计划，以及 new_version 的下一版草稿；
不创建子任务、不等待 runner、不接 API / 前端。状态与检查点写入 runtime/agents/<id>/。

与 LangGraph 的对应关系（Spec 第 18.8 节）：
    State=agent.json，Node=本文件的函数，Edge=VALID_TRANSITIONS，
    Checkpoint=每个节点后的原子写，Interrupt=awaiting_human，Resume=runner 重新入队（P1）。
"""
import json
import re
from pathlib import Path

from factopia.agents import children, llm, observe, policy as policy_mod, report, store, tools
from factopia.lang import llm_language
from factopia.research import store as research_store
from factopia.research import verdict


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
    "evaluating": {"observing", "queued", "done", "stopped", "failed"},
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
    """从一个研究版本启动 Agent（P0/P1 同步入口）。返回最终的 agent 记录。

    dry_run：只做事实、诊断、规划；approve：自动批准原草稿（演示 / 测试）。
    P1 正常流程不带 approve：在 awaiting_human 后用 approve 命令。
    """
    spec = research_store.load_version(research_id, version)
    pol = policy or policy_mod.load()
    markets = list(spec.get("markets") or ["hk", "uk", "fr"])
    agent = store.create(research_id, version, pol, markets, int(spec.get("horizon") or 20), dry_run=dry_run)
    agent["round_limit"] = min(int(rounds), int(pol["max_rounds"]))
    agent["auto_approve"] = bool(approve)
    store.save(agent)
    try:
        while True:
            result = advance(agent, chat=chat)
            agent = result["agent"]
            if result["status"] in ("waiting", "awaiting_human", "done", "stopped", "failed"):
                break
        if agent.get("auto_approve") and agent.get("state") == "waiting_children":
            from factopia.agents import approve as approve_mod
            approve_mod.ensure_job(agent)          # 子任务已创建，交给 runner 等待/继续
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


def advance(agent: dict, chat=None) -> dict:
    """按 agent.state 执行阶段，直到等待/终态；每个阶段已写检查点。"""
    chat = chat or llm.chat
    root = agent["root"]
    while True:
        state = agent.get("state")
        n = int(agent.get("current_round") or 0)
        if state == "queued":
            if n >= int(agent.get("round_limit") or agent["policy"]["max_rounds"]):
                agent["stop_reason"] = f"达到最大轮数 {agent.get('round_limit')}"
                _advance(agent, "stopped")
                return _result(agent)
            agent["current_round"] = n + 1
            store.save(agent)
            _advance(agent, "observing")
            continue
        if state == "observing":
            facts = observe.build(root["research_id"], int(agent["current_version"]))
            store.write_round(agent["agent_id"], agent["current_round"], "facts", facts)
            store.append_trace(agent["agent_id"], {"kind": "facts", "round": agent["current_round"],
                                                   "count": len(facts["facts"])})
            _advance(agent, "diagnosing")
            continue
        if state == "diagnosing":
            facts = _round(agent, "facts")
            diagnosis = _diagnose(facts, store.memory(agent["agent_id"]), agent["policy"], chat, agent)
            store.write_round(agent["agent_id"], agent["current_round"], "diagnosis", diagnosis)
            store.add_usage(agent, diagnosis.get("usage"))
            if agent.get("web_search"):
                external = _web_search(agent, diagnosis)
                store.write_round(agent["agent_id"], agent["current_round"], "external", external)
            _advance(agent, "planning")
            continue
        if state == "planning":
            spec = research_store.load_version(root["research_id"], int(agent["current_version"]))
            rule = verdict.rules(spec)
            facts = _round(agent, "facts")
            diagnosis = _round(agent, "diagnosis")
            external = _round(agent, "external") if agent.get("web_search") else None
            plan = _plan(facts, diagnosis, store.memory(agent["agent_id"]), agent["policy"], chat, agent, rule,
                         external=external, deep_think=bool(agent.get("deep_think")))
            store.write_round(agent["agent_id"], agent["current_round"], "plan", plan)
            store.add_usage(agent, plan.get("usage"))
            if plan["action"] == "stop":
                agent["stop_reason"] = (plan.get("args") or {}).get("reason") or "agent stop"
                _advance(agent, "done")
                return _result(agent)
            if agent.get("dry_run"):
                agent["stop_reason"] = "dry-run：只生成事实、诊断与计划，未执行动作"
                _advance(agent, "stopped")
                return _result(agent)
            if plan["action"] == "new_version" and not _round(agent, "next_version_draft"):
                draft_action = tools.execute("new_version", plan.get("args") or {},
                                             _ctx(agent, spec, int(agent["current_version"])))
                store.write_round(agent["agent_id"], agent["current_round"], "draft_action", draft_action)
                store.add_usage(agent, draft_action.get("usage"))
            gate = plan["action"] == "new_version" or policy_mod.needs_gate(agent["policy"], plan["action"])
            if gate and not agent.get("approved"):
                if agent.get("auto_approve"):
                    _advance(agent, "awaiting_human")
                    from factopia.agents import approve as approve_mod
                    approve_mod.auto_approve(agent, run=bool(agent.get("approved_run", True)))
                    agent = store.load(agent["agent_id"])
                    continue
                agent["stop_reason"] = f"等待人工批准动作 {plan['action']}"
                _advance(agent, "awaiting_human")
                return _result(agent)
            _advance(agent, "acting")
            continue
        if state == "awaiting_human":
            if (agent.get("approved") or {}).get("decision") != "approved":
                return _result(agent, status="awaiting_human")
            _advance(agent, "acting")
            continue
        if state == "acting":
            spec = research_store.load_version(root["research_id"], int(agent["current_version"]))
            plan = _round(agent, "plan")
            action = _act(agent, spec, plan, n)
            store.write_round(agent["agent_id"], n, "action", action)
            store.add_usage(agent, action.get("usage"))
            if action.get("child_jobs"):
                agent["child_jobs"] = list(dict.fromkeys(action["child_jobs"]))
                agent["last_action"] = {"action": plan.get("action"), "status": action.get("status"),
                                        "child_jobs": agent["child_jobs"], "at": store.now()}
                store.save(agent)
                _advance(agent, "waiting_children")
                return _result(agent, status="waiting")
            if action.get("status") in ("saved", "done"):
                agent["stop_reason"] = action.get("reason") or "下一版已保存；未自动运行"
                _advance(agent, "done")
                return _result(agent)
            _advance(agent, "evaluating")
            continue
        if state == "waiting_children":
            if not children.ready(agent):
                return _result(agent, status="waiting")
            child_jobs = children.jobs(agent)
            _add_child_costs(agent, child_jobs)
            _diagnose_children(agent, child_jobs)
            new_ids = _ensure_validation(agent, child_jobs)
            if new_ids:
                agent["child_jobs"] = list(dict.fromkeys([*(agent.get("child_jobs") or []), *new_ids]))
                store.save(agent)
                if not children.ready(agent):
                    return _result(agent, status="waiting")
            _advance(agent, "evaluating")
            continue
        if state == "evaluating":
            plan = _round(agent, "plan")
            action = _round(agent, "action")
            evaluation = _evaluate(agent, plan, action)
            spec = research_store.load_version(root["research_id"], int(agent["current_version"]))
            evaluation["precheck"] = _rule_precheck(agent, spec)
            if evaluation.get("verdict") in ("not_improved", "inconclusive"):
                idea = _agent_feedback(agent, evaluation)
                if idea:
                    evaluation["idea_id"] = idea["id"]
            store.write_round(agent["agent_id"], n, "evaluation", evaluation)
            _update_memory(agent, store.memory(agent["agent_id"]), n, plan, evaluation)
            reason = policy_mod.should_stop(agent, store.memory(agent["agent_id"]), agent["policy"])
            if reason:
                agent["stop_reason"] = reason
                _advance(agent, "stopped")
                return _result(agent)
            if n >= int(agent.get("round_limit") or agent["policy"]["max_rounds"]):
                agent["stop_reason"] = f"完成 {n} 轮"
                _advance(agent, "stopped")
                return _result(agent)
            _advance(agent, "queued")
            continue
        return _result(agent)


def advance_by_id(agent_id: str, chat=None) -> dict:
    """runner 入口：从磁盘加载 Agent 后推进到等待/终态，返回 agent 记录。"""
    return advance(store.load(agent_id), chat=chat)["agent"]


def _result(agent: dict, status: str | None = None) -> dict:
    return {"status": status or agent.get("state"), "agent": agent,
            "child_jobs": agent.get("child_jobs") or []}


def _round(agent: dict, name: str) -> dict | None:
    return store.read_round(agent["agent_id"], int(agent.get("current_round") or 0), name)


def _ctx(agent: dict, spec: dict, version: int) -> dict:
    facts = _round(agent, "facts") or {}
    return {"agent_id": agent["agent_id"], "agent": agent, "round": int(agent.get("current_round") or 0),
            "research_id": agent["root"]["research_id"], "version": int(version), "spec": spec,
            "fact_ids": [f["id"] for f in facts.get("facts") or []]}


def _act(agent: dict, spec: dict, plan: dict, n: int) -> dict:
    """执行本轮计划。new_version 必须先由人工 approve 保存版本。"""
    action = plan.get("action")
    if action == "new_version":
        approved = agent.get("approved") or {}
        version = int(approved.get("saved_version") or 0)
        if not version:
            raise AgentStateError("new_version 需要人工 approve 保存版本后才能运行")
        new_spec = research_store.load_version(agent["root"]["research_id"], version)
        if not approved.get("run", True):
            return {"status": "saved", "version": version,
                    "path": str(research_store.version_path(agent["root"]["research_id"], version)),
                    "reason": "下一版已保存；未自动运行"}
        tool = "run_text" if new_spec.get("signal_source") == "text" else "run_rdagent"
        markets = new_spec.get("markets") or agent["root"]["markets"]
        policy = agent["policy"]
        budget = float(policy.get("child_budget_usd") or
                       max(0.1, float(policy.get("budget_usd") or 2.0) / max(1, len(markets))))
        args = {"version": version, "markets": list(markets),
                "horizon": int(new_spec.get("horizon") or 20),
                "loops": int(policy.get("child_loops") or 2),
                "budget_usd": budget}
        return tools.execute(tool, args, _ctx(agent, new_spec, version))
    return tools.execute(action, plan.get("args") or {}, _ctx(agent, spec, int(agent["current_version"])))


def _add_child_costs(agent: dict, child_jobs: list[dict]) -> None:
    """把子任务费用累加到 Agent（按 job_id 去重），供 policy 预算判断。"""
    costed = set(agent.get("costed_jobs") or [])
    total = float(agent.get("cost_usd") or 0.0)
    for job in child_jobs:
        jid = job.get("job_id")
        if not jid or jid in costed:
            continue
        total += float(job.get("cost_usd") or 0.0)
        costed.add(jid)
    agent["costed_jobs"] = sorted(costed)
    agent["cost_usd"] = round(total, 6)
    store.save(agent)


def _diagnose_children(agent: dict, child_jobs: list[dict]) -> None:
    """导出完成后补跑诊断；失败不影响评估（facts 会标记数据缺失）。"""
    results = research_store.RUNTIME / "results" / "runs"
    for job in child_jobs:
        rid = job.get("run_id")
        if not rid or job.get("kind") == "validate":
            continue
        try:
            from factopia.diagnostics import diagnose_run
            n = diagnose_run(results / rid)
            store.append_trace(agent["agent_id"], {"kind": "diagnose", "run_id": rid, "rounds": n})
        except Exception as e:
            store.append_trace(agent["agent_id"], {"kind": "diagnose_error", "run_id": rid,
                                                   "error": str(e)[:500]})


def _ensure_validation(agent: dict, child_jobs: list[dict]) -> list[str]:
    """为 RD-Agent 子运行创建/复用可信度检查 job；文本研究已自带，跳过。"""
    results = research_store.RUNTIME / "results" / "runs"
    ids = []
    for job in child_jobs:
        if job.get("kind") in ("validate", "text") or not job.get("run_id"):
            continue
        run_id = job["run_id"]
        run = _read_json(results / run_id / "run.json")
        rounds = _read_json(results / run_id / "rounds.json") or []
        if not isinstance(run, dict) or not isinstance(rounds, list):
            continue
        focus = verdict.focus_round(rounds)
        if not focus:
            continue
        existing = [j for j in research_store.list_jobs()
                    if j.get("kind") == "validate" and j.get("target_run") == run_id
                    and j.get("loop") == focus["loop"]]
        if existing:
            ids.append(existing[-1]["job_id"])
            continue
        try:
            ids.append(research_store.create_validate_job(run, focus["loop"])["job_id"])
        except Exception:
            continue
    return ids


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _diagnose(facts: dict, memory: dict, policy: dict, chat, agent: dict) -> dict:
    version = policy["prompts"]["critic"]
    prompt = llm.prompt(version, language=llm_language(), facts=_facts_text(facts["facts"]), memory=_memory_text(memory))
    note = ""
    for attempt in range(2):
        raw, usage, model = chat(prompt + note, "研究迭代诊断")
        store.append_trace(agent["agent_id"], {"kind": "llm", "role": "critic", "round": agent.get("current_round"),
                                               "model": model, "usage": usage,
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


def _external_text(external: dict | None, start: int = 1) -> str:
    if not external:
        return "- none"
    lines = []
    for i, r in enumerate(external.get("results") or [], start):
        lines.append(f"- [W{i}] {r.get('title')} ({r.get('published') or 'date unknown'}; {r.get('url')}) "
                     f"{str(r.get('snippet') or '')[:500]}")
    if external.get("error"):
        lines.append(f"- web search unavailable: {external['error']}")
    return "\n".join(lines) or "- none"


def _web_search(agent: dict, diagnosis: dict) -> dict:
    """联网检索（仅当启动时勾选）：查询由研究标题、假设与诊断构成；失败不阻断迭代。"""
    from factopia.research import websearch
    root = agent["root"]
    try:
        spec = research_store.load_version(root["research_id"], int(agent["current_version"]))
    except Exception:
        spec = {}
    parts = [spec.get("title_zh"), spec.get("hypothesis"), (diagnosis.get("primary_cause") or {}).get("text")]
    query = " ".join(str(x).strip() for x in parts if x)[:300]
    try:
        out = websearch.search(query)
    except Exception as e:
        out = {"configured": False, "provider": None, "query": query, "before": None,
               "results": [], "error": str(e)[:500], "raw_count": 0, "excluded": 0}
    out = {**out, "at": store.now()}
    store.append_trace(agent["agent_id"], {"kind": "web_search", "query": query, "provider": out.get("provider"),
                                           "configured": out.get("configured"), "error": out.get("error"),
                                           "count": len(out.get("results") or [])})
    return out


def _revision_query(agent: dict, critique: dict) -> str:
    """Critic 要求补证时生成一条短查询；去掉测试期年份和 F#/W# 标记。"""
    q = str(critique.get("search_query") or "").strip()
    if not q:
        parts = []
        try:
            root = agent["root"]
            spec = research_store.load_version(root["research_id"], int(agent["current_version"]))
            parts += [spec.get("title_zh"), spec.get("hypothesis")]
        except Exception:
            pass
        parts += [str(x) for x in (critique.get("required_changes") or [])[:2]]
        q = " ".join(str(x).strip() for x in parts if str(x).strip())
    q = re.sub(r"\b20(?:2[2-9]|[3-9]\d)\b", " ", q)      # 不把测试期年份带进搜索
    q = re.sub(r"\[[FW]\d+\]", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:300]


def _revision_search(agent: dict, critique: dict, name: str = "external_revision") -> dict | None:
    """Critic 明确要求补充外部证据时，平台执行一次受控检索。

    只有 Agent 启动时勾选联网才执行；结果写入 rounds/<n>/<name>.json（辩论多轮时
    每轮一个文件，默认 external_revision / external_revision_2 / …），
    并注入 Planner 修订 prompt。失败不阻断修订。
    """
    if not agent.get("web_search") or not critique.get("needs_external_search"):
        return None
    query = _revision_query(agent, critique)
    if not query:
        return None
    from factopia.research import websearch
    try:
        out = websearch.search(query)
    except Exception as e:
        out = {"configured": False, "provider": None, "query": query, "before": None,
               "results": [], "error": str(e)[:500], "raw_count": 0, "excluded": 0}
    round_no = int(agent.get("current_round") or 0)
    out = {**out, "at": store.now(), "phase": "revision"}
    try:
        store.write_round(agent["agent_id"], round_no, name, out)
    except Exception:
        pass
    store.append_trace(agent["agent_id"], {"kind": "web_search", "phase": "revision",
                                           "round": round_no, "query": query,
                                           "provider": out.get("provider"),
                                           "configured": out.get("configured"),
                                           "error": out.get("error"),
                                           "count": len(out.get("results") or [])})
    return out


def _plan(facts: dict, diagnosis: dict, memory: dict, policy: dict, chat, agent: dict, rule: dict,
          external: dict | None = None, deep_think: bool = False) -> dict:
    version = policy["prompts"]["planner"]
    available = policy_mod.available_tools(policy, tools.REGISTRY)
    prompt = llm.prompt(
        version, language=llm_language(), facts=_facts_text(facts["facts"]),
        diagnosis=_diagnosis_text(diagnosis),
        memory=_memory_text(memory), tools=tools.describe(policy, tools.REGISTRY),
        policy=json.dumps({k: policy.get(k) for k in ("max_rounds", "budget_usd", "no_improve_patience", "human_gate")}),
        external=_external_text(external),
    )
    fact_ids = {f["id"] for f in facts["facts"]}
    plan = _plan_call(prompt, fact_ids, available, rule, chat, agent, "planner", "研究迭代规划", version)
    if not deep_think:
        return plan
    # 深度思考（有界辩论）：Planner 草案 → 独立 Critic 评审 → Planner 修订，循环最多 debate_rounds 轮；
    # Critic accept（无反驳）或评审 / 修订失败时提前结束。每轮最多一次评审 + 一次修订。
    max_debate = max(1, int(policy.get("debate_rounds") or 3))
    critiques: list[dict] = []
    revisions = 0
    revision_external = None
    for debate_round in range(1, max_debate + 1):
        critique = _critique_plan(prompt, plan, facts, diagnosis, external, policy, chat, agent)
        if not critique:
            break                                        # 评审失败：保留当前计划，提前结束
        critiques.append(critique)
        store.write_round(agent["agent_id"], int(agent.get("current_round") or 0), "plan_critique", critiques)
        if critique.get("verdict") != "revise":
            break                                        # accept：辩论提前结束
        round_external = None
        if agent.get("web_search") and critique.get("needs_external_search"):
            round_external = _revision_search(agent, critique,
                                              name="external_revision" if debate_round == 1
                                              else f"external_revision_{debate_round}")
        if round_external:
            revision_external = round_external
        revision_prompt = (
            prompt
            + "\n\n## Your draft plan (JSON)\n" + json.dumps(plan, ensure_ascii=False)
            + "\n\n## Independent critique (untrusted suggestion)\n" + json.dumps(critique, ensure_ascii=False)
            + "\n\nRevise the plan using the critique. Keep the same JSON schema and tool allowlist. "
              "Output the full plan JSON only; do not include the critique."
        )
        if round_external:
            start = len((external or {}).get("results") or []) + 1
            revision_prompt += (
                "\n\n## Additional external evidence found after critique "
                "(untrusted; optional; cite as [W#])\n" + _external_text(round_external, start=start)
            )
        try:
            plan = _plan_call(revision_prompt, fact_ids, available, rule, chat, agent,
                              "planner_revise", "研究迭代计划修订", version)
            revisions += 1
        except store.AgentError:
            break                                        # 修订失败：保留当前计划，提前结束
    plan["deep_think"] = {"critiques": critiques, "revisions": revisions,
                          "critique": critiques[-1] if critiques else None,   # 兼容旧字段：最后一次评审
                          "revised": revisions > 0,
                          "revision_external": revision_external,
                          "debate_rounds": max_debate}
    return plan


def _plan_call(prompt: str, fact_ids: set[str], available: list[str], rule: dict | None,
               chat, agent: dict, role: str, task: str, version: str) -> dict:
    note = ""
    for attempt in range(2):
        raw, usage, model = chat(prompt + note, task)
        store.append_trace(agent["agent_id"], {"kind": "llm", "role": role, "round": agent.get("current_round"),
                                               "model": model, "usage": usage,
                                               "prompt_version": version, "prompt": prompt + note, "response": raw})
        try:
            data = llm.parse_json(raw)
            out = _validate_plan(data, fact_ids, available, rule)
            out.update({"model": model, "prompt_version": version, "usage": usage, "at": store.now()})
            return out
        except (llm.LLMError, AgentStateError) as e:
            note = f"\n\nYour previous answer was rejected: {e}. Answer again with the required JSON object."
    raise store.AgentError(f"{task}两次都没有给出合法动作")


def _critique_plan(prompt: str, plan: dict, facts: dict, diagnosis: dict, external: dict | None,
                   policy: dict, chat, agent: dict) -> dict | None:
    """独立计划评审；失败时返回 None，不影响本轮（保留原计划）。"""
    version = policy["prompts"].get("plan_critic", "iteration-plan-critic-v1")
    critic_prompt = llm.prompt(
        version, language=llm_language(), facts=_facts_text(facts["facts"]),
        diagnosis=_diagnosis_text(diagnosis), plan=json.dumps(plan, ensure_ascii=False),
        external=_external_text(external),
    )
    for attempt in range(2):
        raw, usage, model = chat(critic_prompt, "研究迭代计划评审", role="critic")
        store.append_trace(agent["agent_id"], {"kind": "llm", "role": "plan_critic", "round": agent.get("current_round"),
                                               "model": model, "usage": usage, "prompt_version": version,
                                               "prompt": critic_prompt, "response": raw})
        try:
            data = llm.parse_json(raw)
            verdict = str(data.get("verdict") or "").lower()
            if verdict not in ("accept", "revise"):
                raise llm.LLMError("critique.verdict 必须是 accept 或 revise")
            issues = []
            for x in (data.get("issues") or [])[:5]:
                if not isinstance(x, dict) or not str(x.get("text") or "").strip():
                    continue
                issues.append({"severity": "block" if x.get("severity") == "block" else "warn",
                               "text": str(x["text"])[:1000],
                               "cites": [str(c) for c in (x.get("cites") or [])][:6]})
            needs_search = verdict == "revise" and str(
                data.get("needs_external_search")).lower() in ("1", "true", "yes")
            search_query = data.get("search_query")
            search_query = str(search_query).strip()[:300] if isinstance(search_query, str) else ""
            return {"verdict": verdict, "issues": issues,
                    "required_changes": [str(x)[:500] for x in (data.get("required_changes") or [])][:6],
                    "alternative_action": data.get("alternative_action"),
                    "needs_external_search": needs_search,
                    "search_query": search_query if needs_search else "",
                    "model": model, "prompt_version": version, "usage": usage, "at": store.now()}
        except (llm.LLMError, AgentStateError):
            break
    return None


def _validate_plan(data: dict, fact_ids: set[str], available: list[str], rule: dict | None = None) -> dict:
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
    if rule and "rank_ic" in str(criteria.get("metric") or "").lower():
        sign = int(rule.get("ric_sign") or 1)
        allowed = ("increase", "positive") if sign > 0 else ("decrease", "negative")
        if direction not in allowed:
            raise AgentStateError(
                f"success_criteria.direction={direction!r} 与假设的 rank IC 方向"
                f"（{'正' if sign > 0 else '负'}）不一致；应为 {' / '.join(allowed)}")
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


def _focus_for(run_id: str) -> dict | None:
    """读取一个运行采纳轮次的指标、可证伪判定与可信度状态（评估用，给人看）。"""
    run = _read_json(children.RESULTS / "runs" / run_id / "run.json")
    rounds = _read_json(children.RESULTS / "runs" / run_id / "rounds.json") or []
    if not isinstance(run, dict) or not isinstance(rounds, list):
        return None
    rs = run.get("research") or {}
    if not rs.get("id") or rs.get("version") is None:
        return None
    try:
        spec = research_store.load_version(rs["id"], int(rs["version"]))
        rule = verdict.rules(spec)
    except Exception:
        return None
    focus = verdict.focus_round(rounds)
    if not focus:
        return {"run_id": run_id, "market": run.get("market"), "metrics": None,
                "passed": False, "credibility": "unchecked", "baseline": {}}
    judged = {v["loop"]: v for v in verdict.judge_rounds(run, rounds, rule)}
    val = _read_json(children.RESULTS / "runs" / run_id / "validation" / f"{focus['loop']}.json")
    try:
        cred = verdict.credibility(val, None, rule, None) if val else {"state": "unchecked"}
    except Exception:
        cred = {"state": "unknown"}
    return {"run_id": run_id, "market": run.get("market"), "focus_loop": focus["loop"],
            "metrics": focus.get("metrics") or {}, "passed": judged.get(focus["loop"], {}).get("passed"),
            "credibility": cred.get("state"),
            "baseline": (run.get("baseline") or {}).get("metrics") or {}}


def _parent_focus(agent: dict, market: str) -> dict | None:
    version = int(agent.get("parent_version") or agent["root"]["version"])
    rid = agent["root"]["research_id"]
    d = children.RESULTS / "runs"
    if not d.exists():
        return None
    for path in sorted(d.glob("*/run.json"), reverse=True):
        run = _read_json(path)
        if not isinstance(run, dict) or run.get("market") != market:
            continue
        rs = run.get("research") or {}
        if rs.get("id") == rid and rs.get("version") == version:
            out = _focus_for(run["run_id"])
            if out and out.get("metrics"):
                return out
    return None


def _rule_precheck(agent: dict, spec: dict) -> dict:
    """评估后的确定性 setup 规则预检；只保存摘要，完整结果仍在风控台。"""
    try:
        from factopia import rules
        from factopia.rules import context as rule_ctx
        res = rules.evaluate(rule_ctx.setup_context(spec), ["setup"])
        return {"blocked": res.get("blocked"), "counts": res.get("counts"),
                "approval_path": res.get("approval_path"),
                "hits": [{k: h.get(k) for k in ("rule_id", "title", "severity", "severity_zh", "market",
                                                 "reason", "pack_version")}
                         for h in res.get("hits") or []]}
    except Exception as e:
        return {"error": str(e)[:500]}


def _agent_feedback(agent: dict, evaluation: dict) -> dict | None:
    """未改进 / 无法判定时自动写一张「研究 Agent 反馈」线索；失败不影响评估。"""
    try:
        from factopia import ideas
        run_ids = [j["run_id"] for j in children.jobs(agent)
                   if j.get("run_id") and j.get("kind") != "validate"]
        return ideas.agent_feedback(agent["agent_id"], agent["root"]["research_id"],
                                    int(agent["current_version"]), int(agent.get("current_round") or 0),
                                    str(evaluation.get("verdict") or ""), str(evaluation.get("reason") or ""),
                                    agent["root"]["markets"], run_ids[0] if run_ids else None)
    except Exception:
        return None


def _evaluate(agent: dict, plan: dict, action: dict | None) -> dict:
    """按预注册标准比较 parent / child；评估给人看，可以含具体数值。"""
    n = int(agent.get("current_round") or 0)
    evidence = (plan.get("rationale") or {}).get("cites") or []
    if plan.get("action") != "new_version":
        return {"round": n, "verdict": "stopped",
                "reason": (plan.get("args") or {}).get("reason") or "agent stop",
                "success_criteria": plan.get("success_criteria"), "evidence": evidence, "at": store.now()}
    if (action or {}).get("status") == "saved":
        return {"round": n, "verdict": "inconclusive", "reason": "下一版已保存但未运行",
                "success_criteria": plan.get("success_criteria"), "evidence": evidence, "at": store.now()}
    child_jobs = children.jobs(agent)
    child_runs = [j for j in child_jobs if j.get("run_id") and j.get("kind") != "validate"]
    if not child_runs:
        return {"round": n, "verdict": "inconclusive", "reason": "没有子运行结果",
                "success_criteria": plan.get("success_criteria"), "evidence": evidence, "at": store.now()}
    spec = research_store.load_version(agent["root"]["research_id"], int(agent["current_version"]))
    rule = verdict.rules(spec)
    primary = rule["primary"]
    child = next((x for x in (_focus_for(j["run_id"]) for j in child_runs) if x and x.get("market") == primary), None)
    parent = _parent_focus(agent, primary)
    criteria = plan.get("success_criteria") or {}
    sign = int(rule.get("ric_sign") or 1)
    try:
        delta = float(criteria.get("min_delta") or 0.0)
    except (TypeError, ValueError):
        delta = 0.0
    c_ric = (child or {}).get("metrics", {}).get("rank_ic") if child else None
    p_ric = (parent or {}).get("metrics", {}).get("rank_ic") if parent else None
    aligned = None if c_ric is None else c_ric * sign
    p_aligned = None if p_ric is None else p_ric * sign
    metric_ok = aligned is not None and p_aligned is not None and aligned >= p_aligned + delta
    cred_ok = (child or {}).get("credibility") not in ("doubtful",)
    passed = (child or {}).get("passed")
    if child is None or c_ric is None:
        outcome, reason = "inconclusive", "子运行没有采纳轮次或缺少指标"
    elif passed is False:
        outcome, reason = "not_improved", "子版本未通过预注册证伪检验"
    elif p_aligned is None:
        outcome, reason = "inconclusive", "缺少父版本对照指标"
    elif metric_ok and cred_ok:
        outcome, reason = "improved", "预注册指标达到门槛且可信度未见否决项"
    else:
        outcome, reason = "not_improved", "预注册指标未达到门槛或可信度存疑"
    return {"round": n, "verdict": outcome, "reason": reason,
            "success_criteria": criteria, "parent": parent, "child": child,
            "evidence": evidence,
            "note": "P1 用平台测试期 verdict/credibility 对照；验证期组合指标尚不可得",
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
