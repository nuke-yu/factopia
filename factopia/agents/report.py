"""迭代报告：把 agent.json、每轮事实 / 诊断 / 计划 / 动作 / 评估整理成中文 Markdown。

报告是给人看的摘要；完整证据与提示词在 rounds/<n>/ 与 trace.jsonl。
"""
from factopia.agents import store


def build(agent: dict) -> str:
    root = agent.get("root") or {}
    lines = [
        f"# 研究迭代报告 · {agent.get('agent_id')}",
        "",
        f"- 根研究：{root.get('research_id')} v{root.get('version')}",
        f"- 市场：{', '.join(root.get('markets') or [])}；预测周期：{root.get('horizon')} 个交易日",
        f"- 状态：{agent.get('state')}；停止原因：{agent.get('stop_reason') or '—'}",
        f"- 费用：${float(agent.get('cost_usd') or 0):.4f}；tokens：{agent.get('usage')}",
        f"- 创建 / 更新：{agent.get('created_at')} / {agent.get('updated_at')}",
        "",
    ]
    for n in range(1, int(agent.get("current_round") or 0) + 1):
        facts = store.read_round(agent["agent_id"], n, "facts")
        diagnosis = store.read_round(agent["agent_id"], n, "diagnosis")
        plan = store.read_round(agent["agent_id"], n, "plan")
        draft = store.read_round(agent["agent_id"], n, "next_version_draft")
        approval = store.read_round(agent["agent_id"], n, "approval")
        action = store.read_round(agent["agent_id"], n, "action")
        evaluation = store.read_round(agent["agent_id"], n, "evaluation")
        lines += [f"## 第 {n} 轮", ""]
        if facts:
            lines.append(f"- 事实：{len(facts.get('facts') or [])} 条；平台结论：{facts.get('summary', {}).get('overall', {}).get('label')}；"
                         f"迁移分类：{facts.get('summary', {}).get('transfer', {}).get('label')}")
        if diagnosis:
            primary = diagnosis.get("primary_cause") or {}
            lines.append(f"- 诊断（{diagnosis.get('confidence')}）：{primary.get('text') or '—'}")
            for item in diagnosis.get("market_findings") or []:
                lines.append(f"  - {item.get('market')}：{item.get('text')}")
            for item in diagnosis.get("risks") or []:
                lines.append(f"  - 风险：{item.get('text')}")
        if plan:
            lines.append(f"- 计划：{plan.get('action')}；预期：{plan.get('expected_outcome') or '—'}")
            if plan.get("success_criteria"):
                lines.append(f"  - 预注册成功标准：{plan['success_criteria']}")
            if plan.get("rationale"):
                lines.append(f"  - 理由：{plan['rationale'].get('text')}（引用 {', '.join(plan['rationale'].get('cites') or [])}）")
            if plan.get("alternatives_rejected"):
                lines.append("  - 未选择的动作：" + "；".join(f"{x.get('action')}（{x.get('why')}）" for x in plan["alternatives_rejected"]))
        if draft:
            lines.append(f"- 下一版草稿：{((draft.get('draft') or {}).get('title_zh')) or '—'}")
        if approval:
            lines.append(f"- 人工审批：{approval.get('decision')}（{approval.get('by')}）；"
                         f"版本 {approval.get('saved_version') or '—'}；自动运行 {approval.get('run')}")
        if action:
            lines.append(f"- 动作结果：{action.get('status')}；草稿：{action.get('path') or '—'}")
        if evaluation:
            lines.append(f"- 评估：{evaluation.get('verdict')}；{evaluation.get('reason')}")
            if evaluation.get("parent") or evaluation.get("child"):
                lines.append(f"  - 对照：parent={evaluation.get('parent')}；child={evaluation.get('child')}")
        lines.append("")

    memory = store.memory(agent["agent_id"])
    if memory.get("attempts") or memory.get("exclusions"):
        lines += ["## 记忆", ""]
        for a in memory.get("attempts") or []:
            lines.append(f"- 第 {a.get('round')} 轮：{a.get('action')} → {a.get('result')}；{a.get('reason')}")
        if memory.get("exclusions"):
            lines.append("- 已排除方向：" + "；".join(map(str, memory["exclusions"])))
        lines.append("")

    lines += ["## 下一步", ""]
    if agent.get("state") == "awaiting_human":
        lines.append("人工批准计划后继续；批准 / 拒绝需写入 trace。")
    elif agent.get("last_error"):
        lines.append(f"本轮失败：{agent['last_error']}。查看 trace.jsonl 与 rounds/ 后重试。")
    elif any((store.read_round(agent["agent_id"], n, "plan") or {}).get("action") == "new_version"
             for n in range(1, int(agent.get("current_round") or 0) + 1)):
        lines.append("审核下一版草稿（rounds/<n>/next_version_draft.json）；锁定后按现有流程运行，再启动下一轮 Agent。")
    else:
        lines.append("按计划继续或交人工复核。")
    lines += ["", f"完整记录：{store.dir_path(agent['agent_id'])}", ""]
    return "\n".join(lines)


def write(agent: dict) -> None:
    """写报告；报告失败不掩盖主流程异常。"""
    try:
        path = store.dir_path(agent["agent_id"]) / "report.md"
        store._atomic_write(path, build(agent))
    except Exception:
        pass
