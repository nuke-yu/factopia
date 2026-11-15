"""Agent 的 LLM 调用与提示词渲染。

复用 factopia/research/draft.py 的大模型配置与 _chat（同一套 .env、同一本费用账），
不引入第二个 LLM 客户端，也不新增依赖。
"""
import json
import re
from pathlib import Path
from string import Template

PROMPTS = Path(__file__).parent / "prompts"


class LLMError(RuntimeError):
    pass


def chat(prompt: str, task: str, lite: bool = False, role: str = "main") -> tuple[str, dict, str]:
    """返回 (原文, usage, 实际使用的模型名)；usage 含 token 与本次费用（draft._chat 已记账）。

    - role="main"：诊断 / 规划 / 下一版草稿等复杂任务；
    - role="critic"：深度思考的独立 Critic，用 FACTOPIA_LLM_CRITIC_MODEL；
    - lite=True：简单的结构化判断，用 Lite 模型。
    """
    from factopia.research import draft
    cfg = draft.llm_config(lite=lite, role=role)
    text, usage = draft._chat(prompt, cfg, task)
    return text, usage, usage.get("model") or cfg["model"]


def parse_json(text: str) -> dict:
    """解析大模型输出的 JSON；容忍 Markdown 代码块与前后说明文字。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(f"大模型输出不是合法 JSON：{s[:200]}")
        try:
            data = json.loads(s[start:end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"大模型输出不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise LLMError("大模型输出的 JSON 顶层必须是对象")
    return data


def prompt(version: str, **kwargs) -> str:
    """读取提示词模板；用 $name 占位，JSON 的花括号保持原样。"""
    path = PROMPTS / f"{version}.md"
    if not path.exists():
        raise LLMError(f"提示词不存在：{version}")
    return Template(path.read_text(encoding="utf-8")).safe_substitute(**kwargs)
