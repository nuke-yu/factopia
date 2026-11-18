"""人工审批：把 Agent 的 vN+1 草稿保存为同一研究的新版本，并唤醒 runner 继续运行。

P1 的人工闸门在 CLI：
    python -m factopia.agents approve <agent_id> [--file <edited.json>] [--run]
    python -m factopia.agents reject <agent_id> --reason ...

Agent 自己不能保存/锁定版本；只有这里（人）可以。
"""
import hashlib
import json
import re
import yaml
from pathlib import Path

from factopia.agents import report, store
from factopia.research import store as research_store

CJK = re.compile(r"[㐀-鿿]")
ENGLISH_FIELDS = ("hypothesis", "falsification")


class ApprovalError(RuntimeError):
    pass


def _payload(agent: dict, file: str | Path | None, draft: dict | None = None) -> dict:
    if isinstance(draft, dict):
        return {"draft": draft}
    if file:
        path = Path(file)
        try:
            text = path.read_text(encoding="utf-8")
            data = yaml.safe_load(text) if path.suffix.lower() in (".yaml", ".yml") else json.loads(text)
        except (OSError, json.JSONDecodeError, yaml.YAMLError) as e:
            raise ApprovalError(f"无法读取审批文件：{e}") from e
    else:
        data = store.read_round(agent["agent_id"], int(agent.get("current_round") or 0), "next_version_draft")
    if not isinstance(data, dict):
        raise ApprovalError("没有可审批的下一版草稿（先让 Agent 生成 new_version）")
    return data


def _draft(payload: dict) -> dict:
    draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else payload
    if not isinstance(draft, dict):
        raise ApprovalError("草稿必须是 JSON 对象")
    return draft


def _validate(draft: dict) -> dict:
    missing = [k for k in ("hypothesis", "falsification") if not str(draft.get(k) or "").strip()]
    directions = draft.get("directions")
    if not isinstance(directions, list) or not directions:
        missing.append("directions")
    rationale = draft.get("rationale")
    if not isinstance(rationale, dict) or not str(rationale.get("note") or "").strip():
        missing.append("rationale.note")
    if missing:
        raise ApprovalError("草稿缺少必填内容：" + "、".join(missing))
    english = [str(draft.get(k) or "") for k in ENGLISH_FIELDS] + \
              [str(x) for x in directions] + [str(rationale["note"])]
    if any(CJK.search(x) for x in english):
        raise ApprovalError("发给 Factopia-Agent 的字段须为英文；请修改 hypothesis / falsification / directions / rationale.note")
    return draft


def _job_for_agent(agent: dict) -> dict:
    jobs = [j for j in research_store.list_jobs()
            if j.get("kind") == "agent" and j.get("agent_id") == agent["agent_id"]]
    if jobs:
        return jobs[-1]
    return research_store.create_agent_job(agent["agent_id"], agent["root"]["research_id"],
                                           agent["current_version"], agent["root"]["markets"],
                                           agent["root"]["horizon"],
                                           float(agent["policy"].get("budget_usd") or 2.0))


def _queue(agent: dict) -> dict:
    job = _job_for_agent(agent)
    job.update(state="queued", error=None, ended_at=None, agent_state=agent["state"],
               child_jobs=agent.get("child_jobs") or [], cost_usd=agent.get("cost_usd") or 0.0)
    research_store.write_job(job)
    return job


def ensure_job(agent: dict) -> dict:
    """确保 Agent 有一个 runner job（CLI 同步跑完后交给 runner 继续等待/执行）。"""
    return _queue(agent)


def approve(agent_id: str, file: str | Path | None = None, run: bool = True,
            by: str = "研究员（未登录）", queue: bool = True, draft: dict | None = None) -> dict:
    """保存人工审改后的草稿为 vN+1，并把 agent job 重新排队。"""
    agent = store.load(agent_id)
    if agent.get("state") != "awaiting_human":
        raise ApprovalError(f"只有 awaiting_human 的 Agent 可以批准（当前 {agent.get('state')}）")
    term = research_store.termination((agent.get("root") or {}).get("research_id"))
    if term:
        raise ApprovalError(f"研究已人工终止（{term.get('by')}，{term.get('at')}），不能再生成新版本")
    payload = _payload(agent, file, draft)
    draft = _validate(_draft(payload))
    parent = research_store.load_version(agent["root"]["research_id"], int(agent["current_version"]))
    markets = draft.get("markets") or parent.get("markets") or agent["root"]["markets"]
    if not isinstance(markets, list) or not markets or any(m not in ("hk", "uk", "fr") for m in markets):
        raise ApprovalError("markets 必须是 hk / uk / fr 的非空子集")
    try:
        horizon = int(draft.get("horizon") or parent.get("horizon") or agent["root"]["horizon"])
    except (TypeError, ValueError) as e:
        raise ApprovalError("horizon 必须是 1 / 5 / 20 / 60") from e
    if horizon not in (1, 5, 20, 60):
        raise ApprovalError("horizon 必须是 1 / 5 / 20 / 60")
    digest = hashlib.sha256(json.dumps(draft, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    spec = {
        **draft,
        "id": agent["root"]["research_id"],
        "markets": markets,
        "horizon": horizon,
        "mandate": draft.get("mandate") or parent.get("mandate"),
        "based_on": f"{agent['root']['research_id']}.v{agent['current_version']}",
        "ai_draft": {"agent_id": agent["agent_id"], "round": int(agent.get("current_round") or 0),
                     "model": (payload.get("meta") or {}).get("model"),
                     "prompt_version": (payload.get("meta") or {}).get("prompt_version"),
                     "draft_sha256": digest},
        "locked_by": by,
    }
    spec.pop("version", None)
    # 先找这个 Agent / 轮次 / 草稿哈希是否已经保存过版本：上次可能在写 approval.json 时失败，
    # 重试不能又建一个 vN+1（用户会看到 v2 失败、v3 成功这种重复版本）。
    existing = None
    for v in research_store.versions(agent["root"]["research_id"]):
        try:
            candidate = research_store.load_version(agent["root"]["research_id"], v)
        except Exception:
            continue
        ad = candidate.get("ai_draft") or {}
        if (ad.get("agent_id") == agent["agent_id"] and int(ad.get("round") or -1) == int(agent.get("current_round") or 0)
                and ad.get("draft_sha256") == digest):
            existing = (v, candidate)
            break
    saved = ({**existing[1], "version": existing[0]} if existing else research_store.save_version(spec))
    if not existing:                  # 复用上次已保存的版本时保留原来的父版本，评估对照不能跳到自身
        agent["parent_version"] = int(agent["current_version"])
    agent["current_version"] = int(saved["version"])
    agent["child_version"] = int(saved["version"])
    agent["approved"] = {"decision": "approved", "by": by, "at": store.now(), "run": bool(run),
                         "saved_version": int(saved["version"]), "draft_sha256": digest}
    agent["approved_run"] = bool(run)
    store.save(agent)                 # 先落审批记录，再写 approval.json / trace / 排队；失败可重试
    store.write_round(agent["agent_id"], int(agent.get("current_round") or 0), "approval", agent["approved"])
    store.append_trace(agent["agent_id"], {"kind": "approval", **agent["approved"]})
    job = _queue(agent) if queue else None
    return {"agent": agent, "saved": saved, "job": job}


def reject(agent_id: str, reason: str, by: str = "研究员（未登录）") -> dict:
    agent = store.load(agent_id)
    if agent.get("state") != "awaiting_human":
        raise ApprovalError(f"只有 awaiting_human 的 Agent 可以拒绝（当前 {agent.get('state')}）")
    agent["approved"] = {"decision": "rejected", "by": by, "at": store.now(),
                         "reason": str(reason)[:1000]}
    agent["state"] = "stopped"
    agent["stop_reason"] = f"人工拒绝：{reason}"[:1000]
    store.write_round(agent["agent_id"], int(agent.get("current_round") or 0), "approval", agent["approved"])
    store.append_trace(agent["agent_id"], {"kind": "approval", **agent["approved"]})
    store.save(agent)
    job = _job_for_agent(agent)
    job.update(state="stopped", ended_at=store.now(), error=agent["stop_reason"], agent_state="stopped")
    research_store.write_job(job)
    return {"agent": agent, "job": job}


def auto_approve(agent: dict, run: bool = True, by: str = "CLI --yes") -> dict:
    """--yes 展示用：不编辑，直接批准原草稿；job 由 loop.run 在创建子任务后再排队。"""
    return approve(agent["agent_id"], run=run, by=by, queue=False)


def stop(agent_id: str, reason: str = "人工停止", by: str = "研究员（未登录）") -> dict:
    """人工停止：Agent 与仍在跑的 job / 子任务都置为取消。"""
    agent = store.load(agent_id)
    if agent.get("state") in ("done", "stopped", "failed", "cancelled"):
        raise ApprovalError(f"Agent 已结束（{agent.get('state')}），无需停止")
    agent["state"] = "stopped"
    agent["stop_reason"] = f"人工停止：{reason}"[:1000]
    store.append_trace(agent["agent_id"], {"kind": "stop", "by": by, "reason": reason[:1000]})
    store.save(agent)
    jobs = [j for j in research_store.list_jobs()
            if j.get("kind") == "agent" and j.get("agent_id") == agent_id]
    for job in jobs:
        if job["state"] not in research_store.FINAL_STATES:
            for cid in agent.get("child_jobs") or []:
                research_store.request_cancel(cid)
            job.update(state="cancelled", ended_at=store.now(), error="人工停止", agent_state="stopped")
            research_store.write_job(job)
    report.write(agent)
    return {"agent": agent, "jobs": jobs}


def terminate_research(research_id: str, version: int | None = None, reason: str = "人工终止当前研究",
                       by: str = "研究员（未登录）") -> dict:
    """人工终止整个研究：写终止标记，并停止该研究下所有仍在活动的 Agent（含其他版本）。

    终止后 POST /api/agents 会拒绝任何新 Agent；人工仍可生成新版本、继续研究或提交审批。
    """
    rec = research_store.terminate(research_id, version, reason, by)   # 先落终止标记，阻止新 Agent
    stopped = []
    for agent in store.list_agents():
        if ((agent.get("root") or {}).get("research_id")) != research_id:
            continue
        if agent.get("state") in ("done", "stopped", "failed", "cancelled"):
            continue
        try:
            stopped.append(stop(agent["agent_id"], reason, by))
        except ApprovalError:               # Agent 在扫描与停止之间结束了：忽略
            continue
    return {"terminated": rec, "agents": stopped}
