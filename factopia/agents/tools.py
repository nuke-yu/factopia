"""ACT：Agent 可调用的工具（Spec 第 18.6 节）。

P0 只实现两个工具：
- new_version：根据诊断生成下一版研究规格草稿（调用 factopia.research.draft.make_draft，
  **不锁定、不保存为正式版本**；研究员审核后再走现有立项 / 审核流程）；
- stop：收敛或放弃，写报告。

工具白名单由策略配置；Planner 只能选 REGISTRY 中的工具，不能发明工具或执行任意代码。
"""
from factopia.agents import store

ALLOWED_CHANGES = {"hypothesis", "direction", "directions", "exclusions", "markets", "horizon", "mandate"}
VALID_MARKETS = ("hk", "uk", "fr")
VALID_HORIZONS = (1, 5, 20, 60)


class ToolError(RuntimeError):
    pass


def _new_version(ctx: dict, args: dict) -> dict:
    changes = args.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ToolError("new_version 需要非空的 changes")
    unknown = set(changes) - ALLOWED_CHANGES
    if unknown:
        raise ToolError(f"new_version 不支持的改动字段：{', '.join(sorted(unknown))}")
    fact_ids = set(ctx.get("fact_ids") or [])
    cites = [c for c in args.get("cites") or [] if c in fact_ids]
    if not cites:
        raise ToolError("new_version 的规划理由必须引用至少一条有效事实（F#）")

    spec = ctx["spec"]
    markets = changes.get("markets") or spec.get("markets") or ctx["agent"]["root"]["markets"]
    if not isinstance(markets, list) or not markets or any(m not in VALID_MARKETS for m in markets):
        raise ToolError(f"markets 必须是 {VALID_MARKETS} 的非空子集")
    try:
        horizon = int(changes.get("horizon") or spec.get("horizon") or ctx["agent"]["root"]["horizon"])
    except (TypeError, ValueError) as e:
        raise ToolError(f"horizon 必须是 {VALID_HORIZONS} 之一") from e
    if horizon not in VALID_HORIZONS:
        raise ToolError(f"horizon 必须是 {VALID_HORIZONS} 之一")
    hypothesis = str(changes.get("hypothesis") or spec.get("hypothesis") or "").strip()
    if not hypothesis:
        raise ToolError("new_version 需要 hypothesis")
    if changes.get("directions") is not None and not isinstance(changes["directions"], list):
        raise ToolError("directions 必须是字符串列表")
    if changes.get("exclusions") is not None and not isinstance(changes["exclusions"], list):
        raise ToolError("exclusions 必须是字符串列表")
    directions = [str(x).strip() for x in changes.get("directions") or [] if str(x).strip()]
    direction = str(changes.get("direction") or "；".join(directions)
                    or "；".join(str(x) for x in spec.get("directions") or [])
                    or spec.get("hypothesis") or "").strip()
    exclusions = [str(x).strip() for x in changes.get("exclusions") or [] if str(x).strip()]
    if exclusions:
        direction += " Avoid these directions (already tested or rejected): " + "; ".join(exclusions)
    mandate = str(changes.get("mandate") or spec.get("mandate") or "demo_crossborder_hk")
    inputs = {
        "hypothesis": hypothesis, "direction": direction, "markets": markets, "horizon": horizon,
        "mandate": mandate, "groups": [], "include_past_runs": True,
        "idea_id": None, "idea_ids": [], "combo_id": None,
        "based_on": f"{ctx['research_id']}.v{ctx['version']}",
    }

    from factopia.research import draft
    try:
        result = draft.make_draft(inputs)
    except Exception as e:
        raise ToolError(f"生成下一版本草稿失败：{e}") from e

    meta = result.get("meta") or {}
    payload = {
        "based_on": inputs["based_on"], "inputs": {k: inputs[k] for k in
                                                    ("hypothesis", "direction", "markets", "horizon", "mandate")},
        "draft": result.get("draft"), "meta": meta,
        "evidence": {"alpha158": result.get("evidence", {}).get("alpha158", []),
                     "past_runs": result.get("evidence", {}).get("past_runs", []),
                     "internal": result.get("evidence", {}).get("internal", [])},
        "created_at": store.now(),
    }
    path = store.write_round(ctx["agent_id"], ctx["round"], "next_version_draft", payload)
    return {"status": "draft_created", "path": str(path), "based_on": inputs["based_on"],
            "next_id": (result.get("draft") or {}).get("id"), "title_zh": (result.get("draft") or {}).get("title_zh"),
            "markets": markets, "horizon": horizon,
            "changed_fields": sorted(k for k, v in changes.items() if v not in (None, "", [], {})),
            "usage": {"prompt_tokens": int(meta.get("prompt_tokens") or 0),
                      "completion_tokens": int(meta.get("completion_tokens") or 0)}}


def _stop(ctx: dict, args: dict) -> dict:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ToolError("stop 需要 reason")
    next_steps = [str(x).strip() for x in args.get("next_steps") or [] if str(x).strip()][:6]
    return {"status": "stopped", "reason": reason[:1000], "next_steps": next_steps}


HANDLERS = {"new_version": _new_version, "stop": _stop}
REGISTRY = {
    "new_version": {
        "description": "Create a draft of the next research version based on the diagnosis. "
                       "It does not lock or save a formal version; a researcher must review it.",
        "costly": True,
        "args": {"changes": {...}, "cites": ["F#"]},
    },
    "stop": {
        "description": "Stop the iteration and write the final report.",
        "costly": False,
        "args": {"reason": "...", "next_steps": ["..."]},
    },
}


def describe(policy: dict, registry: dict) -> str:
    lines = []
    for name in policy.get("tools") or []:
        spec = registry.get(name)
        if spec:
            lines.append(f"- {name}: {spec['description']} (costly={spec['costly']})")
    return "\n".join(lines) or "- none"


def execute(name: str, args: dict, ctx: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"工具 {name} 在 P0 不可用；可用：{', '.join(HANDLERS)}")
    if not isinstance(args, dict):
        raise ToolError(f"{name} 的参数必须是 JSON 对象")
    return handler(ctx, args)
