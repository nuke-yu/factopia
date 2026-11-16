"""Repository 层：把各域 store 的函数签名映射到 SQL。

约定（与方案 4.1 一致）：

* **不换签名**。`factopia/research/store.py` 等旧模块继续暴露同样的函数名与返回结构，
  只是内部多了一条"走数据库"的分支；调用方（API / runner / exporter）一行都不用改。
* **返回形状与文件时代完全一致**。时间统一输出 UTC ISO 字符串（`+00:00`，精确到秒），
  数字统一 `float`，缺失值统一 `None` —— 否则 `store.now()` 产出的字符串和数据库读回的
  值排序/比较会不一致，前端表格里就会出现"时间倒挂"。
* **JSONB 保真**。冻结件（研究规格、审批材料）整体进出，不做字段拆分；
  拆列只用于"要查询、要排序、要加约束"的字段。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any


def iso(value: Any) -> str | None:
    """数据库时间 → 与 `store.now()` 同格式的 UTC 字符串。"""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


def as_dt(value: Any) -> datetime | None:
    """文件里的 ISO 字符串 → datetime，交给 psycopg 适配成 timestamptz。"""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def fnum(value: Any) -> float | None:
    """numeric → float（JSON 里必须是数字，不能是 Decimal）。"""
    return None if value is None else float(value)


def json_safe(obj: Any) -> Any:
    """递归把 NaN / Infinity 换成 None。

    与 `research/store.py::_json_safe` 同一套规则：RD-Agent 失败时成本汇总会产生 NaN，
    直接写进 JSONB 会被 PostgreSQL 判为非法 JSON（NaN 不是合法 JSON 字面量）。
    """
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def chain_hash(ev: dict) -> str:
    """哈希链的单条哈希：`sha256(除 hash 外全部字段)`，键排序、不转义中文。

    三条链（审批 / 信号 / 线索）在文件时代各写了一遍同样的三行代码。合并到这里是为了
    让**迁移时能验证**：同一份 events.jsonl 搬进数据库后，重的 hash 必须与原文件逐字相同，
    否则审计会认为历史被改动。任何一处实现漂移都会让对账失败，所以只能有一份实现。
    """
    payload = {k: v for k, v in ev.items() if k != "hash"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
