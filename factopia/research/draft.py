"""大模型起草研究规格（Spec RS-01 至 RS-06）。

研究员给出假设（中 / 英 / 法文）、授权、市场与周期；系统先计算证据包与授权约束，再请大模型起草规格，
输出按 JSON 结构校验；数据缺口、自由文本授权条款与测试期年份由系统补充为待确认事项。
大模型优先使用 .env 中的 FACTOPIA_LLM_BASE_URL / MODEL / API_KEY（OpenAI 兼容接口，如阿里云百炼）；
未设置时回退到 RD-Agent 的 DeepSeek 配置（CHAT_MODEL=deepseek/... 与 DEEPSEEK_API_KEY）。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from factopia.research import evidence, mandates, regions

PROMPT_VERSION = "rs-draft-v3"   # v3：加入内部知识检索结果（因子库、以往研究、字段口径，只含名称与说明）
PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
TEST_YEAR = re.compile(r"20(?:2[2-9]|[3-9]\d)")
CJK = re.compile(r"[\u3400-\u9fff]")
ENGLISH_FIELDS = ("hypothesis", "falsification")
RATIONALE = ("behavioural", "risk_premium", "friction", "flows", "other")
PLATFORM_CONSTRAINTS = ["Factors must have a clear economic rationale that a reviewer can explain."]

# 数据需求类别 → (名称, 各市场现状, 能否用于 RD-Agent 挖掘)
DATA = {
    "price_volume": ("日线行情与成交量", {"hk": "可用", "uk": "可用", "fr": "可用"}, True),
    "benchmark": ("基准指数日线", {"hk": "可用", "uk": "可用", "fr": "可用"}, True),
    "fx": ("汇率", {"hk": "可用（组合层）", "uk": "可用（组合层）", "fr": "可用（组合层）"}, False),
    "fundamentals": ("财务与基本面数据", {"hk": "未接入", "uk": "未接入", "fr": "未接入"}, False),
    "text": ("公告与新闻文本", {"hk": "需授权，目前用模拟数据（情报台，模拟数据，仅用于流程演示）", "uk": "渠道待确认，目前用模拟数据（情报台，模拟数据，仅用于流程演示）",
                               "fr": "AMF 真实公告已接入情报台（2024 年 8 月起）"}, False),
    "alternative": ("另类数据", {"hk": "未接入", "uk": "未接入", "fr": "未接入"}, False),
    "other": ("其他数据", {"hk": "待确认", "uk": "待确认", "fr": "待确认"}, False),
}


class DraftError(RuntimeError):
    pass


def llm_config() -> dict:
    base, model, key = (os.environ.get(f"FACTOPIA_LLM_{k}") for k in ("BASE_URL", "MODEL", "API_KEY"))
    if base and model and key:
        return {"base_url": base.rstrip("/"), "model": model, "api_key": key}
    chat = os.environ.get("CHAT_MODEL", "")
    if chat.startswith("deepseek/") and os.environ.get("DEEPSEEK_API_KEY"):
        return {"base_url": "https://api.deepseek.com", "model": chat.split("/", 1)[1], "api_key": os.environ["DEEPSEEK_API_KEY"]}
    raise DraftError("未配置大模型：在 .env 中设置 CHAT_MODEL=deepseek/... 与 DEEPSEEK_API_KEY，或 FACTOPIA_LLM_BASE_URL / MODEL / API_KEY")


def _extra_body() -> dict:
    """FACTOPIA_LLM_EXTRA_BODY：附加到请求的服务商参数（JSON），如百炼的 {"enable_thinking": false}。"""
    raw = os.environ.get("FACTOPIA_LLM_EXTRA_BODY", "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DraftError(f".env 中 FACTOPIA_LLM_EXTRA_BODY 不是合法 JSON：{e}") from e
    return v if isinstance(v, dict) else {}


def _chat(prompt: str, cfg: dict, task: str = "其他") -> tuple[str, dict]:
    body = json.dumps({"model": cfg["model"], "temperature": 0.3, "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": prompt}], **_extra_body()}).encode()
    req = urllib.request.Request(f"{cfg['base_url']}/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise DraftError(f"大模型接口返回 {e.code}：{e.read()[:300].decode(errors='replace')}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise DraftError(f"无法连接大模型接口：{e}") from e
    usage = data.get("usage") or {}
    from factopia import llm_cost
    usage["usd"] = llm_cost.record(task, cfg["model"], usage)         # 费用记录（runtime/llm_costs.jsonl）
    return data["choices"][0]["message"]["content"], usage


def _evidence_text(items: list[dict]) -> str:
    if not items:
        return "No Alpha158 evaluation is available for this horizon."
    lines = []
    for e in items:
        lines.append(f"{e['market'].upper()} (period {e['period']['start']} to {e['period']['end']}; "
                     f"{e['significant']} factors with |t| > 3):")
        lines += [f"- {f['name']} ({f['desc']}): rank IC {f['ric']:.3f}, t={f['t']:.2f}, cross-market class: {f['label']}"
                  for f in e["factors"]] or ["- none significant"]
    return "\n".join(lines)


def internal_knowledge(query: str, limit: int = 8) -> list[dict]:
    """检索内部知识（BR-25、35）作为证据：因子库、以往研究规格、字段口径。
    提供给 AI 的只有名称与说明；获批信号与以往研究的结果数值来自测试期，不放入（MINE-11）。AI 解释（含测试期数值）不检索。"""
    from factopia.research import semantic
    try:
        res = semantic.search(query, ["factor", "research", "field"], limit=limit * 2)
    except Exception:
        return []
    out = []
    for r in res["results"]:
        if r["id"].endswith(".explain"):
            continue
        text = re.sub(TEST_YEAR, "", r["snippet"])[:200] if r["kind"] == "research" else r["snippet"][:200]
        out.append({"kind": r["kind"], "kind_zh": r["kind_zh"], "id": r["id"], "title": r["title"], "score": r["score"],
                    "prompt": text, "ref": r["ref"]})
        if len(out) >= limit:
            break
    return out


def _strings(x) -> list[str]:
    return [str(v).strip() for v in (x or []) if str(v).strip()]


def _clean_id(value) -> str | None:
    """把 AI 建议的编号整理为合法格式（小写字母、数字、下划线，3–40 个字符，以字母开头）；无法整理时留空由研究员填写。"""
    s = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    s = re.sub(r"_+", "_", s)[:40].rstrip("_")
    if s and not s[0].isalpha():
        s = f"r_{s}"[:40]
    return s if ID_PATTERN.match(s) else None


def _validate(raw: dict, inputs: dict) -> dict:
    """检查必填字段，规范类型；缺少必填字段时报错（RS-02）。"""
    out = {
        "id": _clean_id(raw.get("id")),
        "title_zh": str(raw.get("title_zh") or "").strip(),
        "hypothesis": str(raw.get("hypothesis") or "").strip(),
        "hypothesis_zh": str(raw.get("hypothesis_zh") or "").strip(),
        "rationale": {"category": (raw.get("rationale") or {}).get("category") if (raw.get("rationale") or {}).get("category") in RATIONALE else "other",
                      "note": str((raw.get("rationale") or {}).get("note") or "").strip()},
        "directions": _strings(raw.get("directions"))[:5],
        "exclusions": _strings(raw.get("exclusions")),
        "falsification": str(raw.get("falsification") or "").strip(),
        "data_needs": [], "mandate_interpretations": [],
        "open_questions": _strings(raw.get("open_questions")),
        "leak_flags": _strings(raw.get("leak_flags")),
    }
    missing = [k for k in ("hypothesis", "falsification") if not out[k]] + ([] if out["rationale"]["note"] else ["rationale"])
    if missing or not out["directions"]:
        raise DraftError(f"大模型返回的规格缺少必填内容：{', '.join(missing + ([] if out['directions'] else ['directions']))}")
    english = [out[k] for k in ENGLISH_FIELDS] + [out["rationale"]["note"], *out["directions"], *out["exclusions"]]
    if any(CJK.search(t) for t in english):
        raise DraftError("发给 RD-Agent 的字段须为英文，大模型返回了中文")
    for d in raw.get("data_needs") or []:
        cat = d.get("category") if d.get("category") in DATA else "other"
        name, status, usable = DATA[cat]
        out["data_needs"].append({"category": cat, "name": name, "detail": str(d.get("detail") or "").strip(), "usable": usable,
                                  "status": {m: status[m] for m in inputs["markets"]}})
    # 信号来源：数据需求以公告文本为主（含文本、且不需要量价以外的其他数据）→ 文本信号研究；研究员可在页面上修改
    cats = {n["category"] for n in out["data_needs"]}
    out["signal_source"] = "text" if "text" in cats else "price_volume"
    free_text = inputs.get("_free_text") or []
    for t in raw.get("mandate_interpretations") or []:
        term = str(t.get("term") or "").strip()
        if not any(term and (term in f or f in term) for f in free_text):  # 只保留自由文本条款的解释
            continue
        out["mandate_interpretations"].append({"term": term,
                                               "interpretation_zh": str(t.get("interpretation_zh") or "").strip(),
                                               "constraint_en": (str(t["constraint_en"]).strip() if t.get("constraint_en") else None)})
    return out


def _system_questions(d: dict, inputs: dict) -> list[str]:
    """系统补充的待确认事项：数据缺口、自由文本授权条款的解释、测试期年份（RS-03、RS-04、RS-06）。"""
    qs = []
    rd = d.get("region_diff")
    if rd and len(rd["markets"]) > 1:
        cost = next((r for r in rd["rows"] if r["item"] == "买入单边成本"), None)
        qs.append(f"地区差异清单（{rd['differs']} 项各市场不同，见审核页）：已知悉交易机制、成本"
                  + (f"（买入单边 {'、'.join(f'{m.upper()} {v}' for m, v in cost['values'].items())}）" if cost else "")
                  + "、交收、披露与地区规则的差异，研究按各市场分别回测、分别判定。")
    if d.get("signal_source") == "text":
        qs.append("信号来源为文本（管理层风险表述变化）：开始运行时检验情报台的文本信号，不启动 RD-Agent；"
                  "香港、英国为模拟数据（仅用于流程演示），法国 AMF 真实公告从 2024 年 8 月开始，测试期内数据不足。"
                  "起点为等权持有股票池（不使用文本信号，不是 RD-Agent 的 20 个基础因子），回撤按绝对回撤（扣成本）比较。"
                  "如需改为量价研究，请把信号来源改为量价。")
    for n in d["data_needs"]:
        if not n["usable"] and n["category"] not in ("fx",) and not (n["category"] == "text" and d.get("signal_source") == "text"):
            qs.append(f"研究需要「{n['name']}」（{n['detail']}），但 RD-Agent 目前只能使用日线量价与基准指数；这部分方向不会被实现。")
    for t in d["mandate_interpretations"]:
        qs.append(f"授权条款「{t['term']}」的解释：{t['interpretation_zh']}"
                  + (f"（将作为约束发给 AI：{t['constraint_en']}）" if t["constraint_en"] else "（只在组合层使用）"))
    years = sorted(set(TEST_YEAR.findall(f"{inputs['hypothesis']} {inputs.get('direction', '')}")))
    if years or d["leak_flags"]:
        qs.append(f"输入中提到测试期（2022 年起）的内容{('：' + '、'.join(years)) if years else ''}；保留会提前透露考题（MINE-11）。")
    return qs


def make_draft(inputs: dict) -> dict:
    """inputs：hypothesis、direction、markets、horizon、mandate、groups、include_past_runs。"""
    mandate = mandates.parse(mandates.load(inputs["mandate"]))
    alpha = evidence.alpha158_findings(inputs["markets"], inputs["horizon"], inputs.get("groups") or None)
    past = evidence.past_runs(inputs["markets"], inputs["horizon"]) if inputs.get("include_past_runs", True) else []
    internal = internal_knowledge(inputs["hypothesis"] + " " + (inputs.get("direction") or ""))
    prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
        internal="\n".join(f"- [{x['kind']}] {x['title']}: {x['prompt']}" for x in internal) or "- none",
        markets=", ".join(inputs["markets"]), horizon=inputs["horizon"],
        direction=inputs.get("direction") or "(not given)", hypothesis=inputs["hypothesis"],
        mandate_name=mandate["name"],
        mandate_constraints="\n".join(f"- {c}" for c in mandate["constraints"]) or "- none",
        mandate_free_text="\n".join(f"- {t}" for t in mandate["free_text"]) or "- none",
        evidence=_evidence_text(alpha),
        past_runs="\n".join(f"- {line}" for line in evidence.past_runs_lines(past)) or "- none (runs affected by data leakage are excluded)",
    )
    cfg = llm_config()
    t0 = time.time()
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    inputs = {**inputs, "_free_text": mandate["free_text"]}
    retry_note = ""
    for attempt in range(2):  # JSON 格式、必填字段或语言不合格时，带上原因重试一次
        text, usage = _chat(prompt + retry_note, cfg, "研究规格起草")
        for k in tokens:
            tokens[k] += int(usage.get(k) or 0)
        try:
            draft = _validate(json.loads(text), inputs)
            break
        except (json.JSONDecodeError, DraftError) as e:
            if attempt == 1:
                raise DraftError(f"大模型两次输出都不合格：{e}") from e
            retry_note = f"\n\nYour previous answer was rejected: {e}. Fix it and answer again with the full JSON object."
    draft["region_diff"] = regions.differences(inputs["markets"], mandates.load(inputs["mandate"]), draft["data_needs"])
    draft["system_questions"] = _system_questions(draft, inputs)
    idea_ids = list(dict.fromkeys([*([inputs["idea_id"]] if inputs.get("idea_id") else []), *(inputs.get("idea_ids") or [])]))
    if idea_ids:                                               # 来自研究线索池（第 17 章）：每条来源线索的防泄漏与文档许可须确认
        from factopia import ideas
        for iid in idea_ids:
            draft["system_questions"] += ideas.confirm_questions(iid)
    draft["constraints"] = mandate["constraints"] + [t["constraint_en"] for t in draft["mandate_interpretations"] if t["constraint_en"]] + PLATFORM_CONSTRAINTS
    return {
        "draft": draft,
        "evidence": {"alpha158": alpha, "past_runs": past, "groups": inputs.get("groups") or [], "internal": internal},
        "mandate": mandate,
        "meta": {"model": cfg["model"], "prompt_version": PROMPT_VERSION, **tokens, "idea_id": inputs.get("idea_id"),
                 "idea_ids": idea_ids, "combo_id": inputs.get("combo_id"), "based_on": inputs.get("based_on"),
                 "seconds": round(time.time() - t0, 1), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
