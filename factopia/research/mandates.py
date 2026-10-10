"""模拟客户授权的读取与解析（Spec 第 16.5 节）。

结构化条款由系统直接转换为挖掘约束；自由文本条款（如"避免争议性行业"）交给 AI 解释，解释结果列入待确认事项（RS-03）。
风险预算、可持续偏好、币种等在因子挖掘阶段不使用，标注为"组合层使用"。
"""
import os
from pathlib import Path

import yaml

MANDATES_DIR = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs")) / "mandates"

TERMS = [  # (字段, 界面名称, 挖掘阶段的用法)
    ("service_region", "客户服务地区", "不影响挖掘"),
    ("target_markets", "目标投资市场", "决定在哪些市场运行"),
    ("base_currency", "基础币种", "组合层使用"),
    ("report_currency", "报告币种", "组合层使用"),
    ("benchmark", "投资基准", "回测基准指数（各市场本地指数）"),
    ("long_only", "是否允许卖空", "只做多时告诉 AI，回测也只做多"),
    ("restricted_securities", "受限证券", "回测股票池剔除"),
    ("risk_budget", "风险预算", "组合层使用"),
    ("sustainability", "可持续投资偏好", "组合层使用"),
    ("data_visibility", "数据可见范围", "决定证据包与数据来源"),
    ("report_language", "报告语言", "不影响挖掘"),
]
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}


def load_all() -> list[dict]:
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(MANDATES_DIR.glob("*.yaml"))]


def load(mandate_id: str) -> dict:
    for m in load_all():
        if m["id"] == mandate_id:
            return m
    raise KeyError(mandate_id)


def _display(key: str, value) -> str:
    if key == "target_markets":
        return "、".join(MARKET_ZH.get(m, m) for m in value)
    if key == "long_only":
        return "不允许（只做多）" if value else "允许"
    if key == "restricted_securities":
        return "、".join(value) if value else "无"
    return str(value)


def parse(mandate: dict) -> dict:
    """返回界面显示的条款、转换出的挖掘约束（英文，发给 AI）与需要 AI 解释的自由文本条款。"""
    terms = [{"key": k, "name": name, "value": _display(k, mandate.get(k)), "usage": usage}
             for k, name, usage in TERMS if k in mandate]
    constraints = []
    if mandate.get("long_only"):
        constraints.append("Long-only portfolio; short selling is not allowed.")
    if mandate.get("restricted_securities"):
        constraints.append("Exclude these restricted securities: " + ", ".join(mandate["restricted_securities"]) + ".")
    return {"id": mandate["id"], "name": mandate["name"], "markets": mandate.get("target_markets", []),
            "terms": terms, "constraints": constraints, "free_text": mandate.get("free_text") or []}
