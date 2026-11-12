"""研究迭代 Agent 的策略与边界（Spec 第 18.8、18.11 节）。

配置来自 configs/agents/iteration.yaml：轮数、预算、无改进耐心、人工闸门、工具白名单、提示词版本。
策略在启动时固定写入 agent.json；运行中不重新读取配置，保证一次运行可复现。
"""
import os
from pathlib import Path

import yaml

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
if not CONFIGS.exists():                       # 本地直接运行时回退到仓库的 configs/
    CONFIGS = Path(__file__).resolve().parents[2] / "configs"

HUMAN_GATES = ("all", "costly_actions", "none")
DEFAULTS = {
    "max_rounds": 5,
    "budget_usd": 2.0,
    "no_improve_patience": 2,
    "human_gate": "costly_actions",
    "tools": ["new_version", "stop"],
    "costly_tools": ["new_version"],
    "prompts": {
        "critic": "iteration-critic-v1",
        "planner": "iteration-planner-v1",
        "evaluator": "iteration-evaluator-v1",
    },
}


class PolicyError(ValueError):
    pass


def load(path: str | Path | None = None) -> dict:
    """读取并校验策略；文件不存在时使用默认值（P0 可无配置文件运行）。"""
    p = Path(path) if path else CONFIGS / "agents" / "iteration.yaml"
    raw = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise PolicyError(f"策略文件必须是 YAML 对象：{p}")
    policy = {**DEFAULTS, **raw}
    try:
        policy["max_rounds"] = int(policy["max_rounds"])
        policy["budget_usd"] = float(policy["budget_usd"])
        policy["no_improve_patience"] = int(policy["no_improve_patience"])
    except (TypeError, ValueError) as e:
        raise PolicyError(f"策略中的轮数 / 预算 / 耐心必须是数字：{e}") from e
    if policy["max_rounds"] < 1:
        raise PolicyError("max_rounds 必须 ≥ 1")
    if policy["budget_usd"] < 0:
        raise PolicyError("budget_usd 必须 ≥ 0")
    if policy["no_improve_patience"] < 0:
        raise PolicyError("no_improve_patience 必须 ≥ 0")
    if policy["human_gate"] not in HUMAN_GATES:
        raise PolicyError(f"human_gate 必须是 {HUMAN_GATES} 之一")
    if not isinstance(policy["tools"], list) or not all(isinstance(x, str) for x in policy["tools"]):
        raise PolicyError("tools 必须是字符串列表")
    if not isinstance(policy["costly_tools"], list) or not all(isinstance(x, str) for x in policy["costly_tools"]):
        raise PolicyError("costly_tools 必须是字符串列表")
    prompts = policy.get("prompts") or {}
    if not isinstance(prompts, dict):
        raise PolicyError("prompts 必须是对象")
    for key in ("critic", "planner"):
        if not prompts.get(key):
            raise PolicyError(f"prompts.{key} 不能为空")
    policy["prompts"] = {**DEFAULTS["prompts"], **prompts}
    return policy


def available_tools(policy: dict, registry: dict) -> list[str]:
    """策略白名单 ∩ P0 已实现的工具：提示词里只列出真正能执行的工具。"""
    return [name for name in policy.get("tools") or [] if name in registry]


def needs_gate(policy: dict, tool: str) -> bool:
    gate = policy.get("human_gate")
    if gate == "all":
        return True
    if gate == "none":
        return False
    return tool in (policy.get("costly_tools") or [])


def should_stop(agent: dict, memory: dict, policy: dict) -> str | None:
    """达到轮数 / 预算 / 连续无改进时返回停止原因；否则 None。"""
    if int(agent.get("current_round") or 0) >= int(policy["max_rounds"]):
        return f"达到最大轮数 {policy['max_rounds']}"
    if float(agent.get("cost_usd") or 0.0) >= float(policy["budget_usd"]):
        return f"费用 {agent.get('cost_usd', 0):.4f} 美元达到预算上限 {policy['budget_usd']:.4f}"
    patience = int(policy.get("no_improve_patience") or 0)
    if patience:
        tail = [a for a in (memory.get("attempts") or [])[-patience:]
                if a.get("result") in ("not_improved", "inconclusive")]
        if len(tail) >= patience:
            return f"连续 {patience} 轮没有改进"
    return None
