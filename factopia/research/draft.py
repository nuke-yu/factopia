"""大模型起草研究规格（Spec RS-01 至 RS-06）。

研究员给出假设（中 / 英 / 法文）、授权、市场与周期；系统先计算证据包与授权约束，再请大模型起草规格，
输出按 JSON 结构校验；数据缺口、自由文本授权条款与测试期年份由系统补充为待确认事项。
大模型优先使用 .env 中的 FACTOPIA_LLM_BASE_URL / MODEL / API_KEY（OpenAI 兼容接口，如阿里云百炼）；
未设置时回退到 RD-Agent 的 DeepSeek 配置（CHAT_MODEL=deepseek/... 与 DEEPSEEK_API_KEY）。
大模型使用与 RD-Agent 相同的 .env 配置（CHAT_MODEL=litellm_proxy/... + LITELLM_PROXY_API_KEY 与
LITELLM_PROXY_API_BASE，硅基流动等 OpenAI 兼容端点），可用 FACTOPIA_LLM_* 覆盖。
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
from factopia.research import seeds as seedlib
from factopia.lang import llm_language
from factopia import lang

PROMPT_VERSION = "rs-draft-v4"   # v4：起点因子（因子库中已有因子做变体 / 组合）+ 可选外部资料（仅测试期前、带引用）
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
    "text": ("公告与新闻文本", {"hk": "需授权，目前用模拟数据（情报台，模拟数据，仅用于流程验证）", "uk": "渠道待确认，目前用模拟数据（情报台，模拟数据，仅用于流程验证）",
                               "fr": "AMF 真实公告已接入情报台（2024 年 8 月起）"}, False),
    "alternative": ("另类数据", {"hk": "未接入", "uk": "未接入", "fr": "未接入"}, False),
    "other": ("其他数据", {"hk": "待确认", "uk": "待确认", "fr": "待确认"}, False),
}


class DraftError(RuntimeError):
    pass


def llm_config(lite: bool = False, role: str = "main") -> dict:
    """大模型配置。

    - role="main"（默认）：Planner / 起草 / 标注等复杂任务，用 FACTOPIA_LLM_MODEL；
    - role="critic"：深度思考的独立 Critic，优先 FACTOPIA_LLM_CRITIC_MODEL；
      未配置或 403 时回退主模型（不会回退到 Lite）；
    - lite=True：简单任务，用 FACTOPIA_LLM_LITE_MODEL。

    返回的 lite_model 供 _chat 在“大模型无权限（403）”时自动降级：main 回退 Lite，
    critic 回退主模型；Lite 不自动回退。
    """
    base = os.environ.get("FACTOPIA_LLM_BASE_URL")
    model = os.environ.get("FACTOPIA_LLM_MODEL")
    key = os.environ.get("FACTOPIA_LLM_API_KEY")
    lite_model = (os.environ.get("FACTOPIA_LLM_LITE_MODEL") or "").strip()
    critic_model = (os.environ.get("FACTOPIA_LLM_CRITIC_MODEL") or "").strip()
    if lite:
        chosen, fallback = (lite_model or model), (lite_model or None)
    elif role == "critic":
        chosen, fallback = (critic_model or model), (model if critic_model else None)
    else:
        chosen, fallback = model, (lite_model or None)
    if base and key and chosen:
        return {"base_url": base.rstrip("/"), "model": chosen, "api_key": key, "lite_model": fallback}
    chat = os.environ.get("CHAT_MODEL", "")
    if chat.startswith("litellm_proxy/") and os.environ.get("LITELLM_PROXY_API_KEY") and os.environ.get("LITELLM_PROXY_API_BASE"):
        return {"base_url": os.environ["LITELLM_PROXY_API_BASE"].rstrip("/"), "model": chat.split("/", 1)[1],
                "api_key": os.environ["LITELLM_PROXY_API_KEY"], "lite_model": None}
    if chat.startswith("zhipu/") and os.environ.get("ZHIPUAI_API_KEY"):
        return {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": chat.split("/", 1)[1],
                "api_key": os.environ["ZHIPUAI_API_KEY"], "lite_model": None}
    if chat.startswith("deepseek/") and os.environ.get("DEEPSEEK_API_KEY"):
        return {"base_url": "https://api.deepseek.com", "model": chat.split("/", 1)[1],
                "api_key": os.environ["DEEPSEEK_API_KEY"], "lite_model": None}
    raise DraftError(lang.t("未配置大模型：在 .env 中设置 CHAT_MODEL=litellm_proxy/... 与 LITELLM_PROXY_API_KEY"
                            "（外加 LITELLM_PROXY_API_BASE，如硅基流动），或 FACTOPIA_LLM_BASE_URL / MODEL / API_KEY"))


def _extra_body() -> dict:
    """FACTOPIA_LLM_EXTRA_BODY：附加到请求的服务商参数（JSON），如百炼的 {"enable_thinking": false}。"""
    raw = os.environ.get("FACTOPIA_LLM_EXTRA_BODY", "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DraftError(lang.t(".env 中 FACTOPIA_LLM_EXTRA_BODY 不是合法 JSON：{e}", e=e)) from e
    return v if isinstance(v, dict) else {}


def _model_access_denied(message: str) -> bool:
    s = (message or "").lower()
    return "403" in s or "access_denied" in s or "model access denied" in s


def _chat_once(prompt: str, cfg: dict, task: str = "其他") -> tuple[str, dict]:
    body = json.dumps({"model": cfg["model"], "temperature": 0.3, "response_format": {"type": "json_object"},
                       "messages": [{"role": "user", "content": prompt}], **_extra_body()}).encode()
    req = urllib.request.Request(f"{cfg['base_url']}/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"})
    for attempt in range(3):                 # 网络抖动 / 网关 5xx：短暂重试
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise DraftError(lang.t("大模型接口返回 {code}：{body}", code=e.code,
                                    body=e.read()[:300].decode(errors="replace"))) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise DraftError(lang.t("无法连接大模型接口：{e}", e=e)) from e
    usage = data.get("usage") or {}
    from factopia import llm_cost
    usage["usd"] = llm_cost.record(task, cfg["model"], usage)         # 费用记录（runtime/llm_costs.jsonl）
    usage["model"] = cfg["model"]
    return data["choices"][0]["message"]["content"], usage


def _chat(prompt: str, cfg: dict, task: str = "其他") -> tuple[str, dict]:
    """调用大模型；复杂任务用的模型无权限（403）时，自动退回 FACTOPIA_LLM_LITE_MODEL。"""
    try:
        return _chat_once(prompt, cfg, task)
    except DraftError as e:
        lite = cfg.get("lite_model")
        if lite and lite != cfg.get("model") and _model_access_denied(str(e)):
            text, usage = _chat_once(prompt, {**cfg, "model": lite}, f"{task}（轻量模型回退）")
            usage["fallback_from"] = cfg.get("model")
            return text, usage
        raise


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
        raise DraftError(lang.t("大模型返回的规格缺少必填内容：{fields}",
                                fields=', '.join(missing + ([] if out['directions'] else ['directions']))))
    english = [out[k] for k in ENGLISH_FIELDS] + [out["rationale"]["note"], *out["directions"], *out["exclusions"]]
    if any(CJK.search(t) for t in english):
        raise DraftError(lang.t("发给 RD-Agent 的字段须为英文，大模型返回了中文"))
    for d in raw.get("data_needs") or []:
        cat = d.get("category") if d.get("category") in DATA else "other"
        name, status, usable = DATA[cat]
        out["data_needs"].append({"category": cat, "name": name, "detail": str(d.get("detail") or "").strip(), "usable": usable,
                                  "status": {m: status[m] for m in inputs["markets"]}})
    # 信号来源：以第一条数据需求为主。text 只是附加条件 / 数据缺口时不能把整份研究判成文本信号研究
    # （例如 FX/量价假设里提到“公告语气条件腿”，RD-Agent 仍应跑 price_volume）。
    primary = out["data_needs"][0]["category"] if out["data_needs"] else "price_volume"
    out["signal_source"] = "text" if primary == "text" else "price_volume"
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
                  "香港、英国为模拟数据（仅用于流程验证），法国 AMF 真实公告从 2024 年 8 月开始，测试期内数据不足。"
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
        qs.append(f"输入中提到测试期（2022 年起）的内容{('：' + '、'.join(years)) if years else ''}；保留会提前透露考题。")
    return qs


def _seeds_section(sd: dict | None) -> str:
    """起点因子段（英文，发给 RD-Agent）：只含名称、说明与公式，不含任何统计数值。"""
    if not sd:
        return "- none (the researcher did not start from an existing factor)"
    lines = ["The researcher chose these existing factors from the internal factor library as the starting points; "
             "the research must explore variations or combinations of them:"]
    for x in sd.get("items") or []:
        seg = f"- {x['id']} ({x['name']}, {x.get('source_zh') or ''})"
        if x.get("description"):
            seg += f": {x['description']}"
        if x.get("formulation"):
            seg += f" | formula: {x['formulation']}"
        lines.append(seg)
    if not sd.get("items"):
        lines.append("- no specific factor was chosen; the researcher described the target only (see below)")
    lines.append(f"Requested mode: {sd['mode']} "
                 + {"variant": "(a variation of one factor: window, sign, normalisation, condition)",
                    "combo": "(a combination of several factors: weighting, trigger, layered sort)",
                    "open": "(open: choose the starting factor yourself from the library)"}.get(sd["mode"], ""))
    if sd.get("brief"):
        lines += ["The researcher described the intended variation or combination in their own words "
                  f"(may be in Chinese, English or French): \"{sd['brief']}\""]
    if sd.get("intent"):
        lines.append(f"Stated intent: {sd['intent']}")
    p = sd.get("proposal") or {}
    if p.get("title_zh"):
        lines += ["An idea proposed by the assistant and accepted by the researcher:",
                  f"- {p['title_zh']}" + (f" — change: {p['change']}" if p.get("change") else "")
                  + (f" — why: {p['logic_zh']}" if p.get("logic_zh") else "")]
    return "\n".join(lines)


def make_draft(inputs: dict, on_llm=None) -> dict:
    """inputs：hypothesis、direction、markets、horizon、mandate、groups、include_past_runs、seeds。

    on_llm 可选：每次起草 LLM 调用后回调 (prompt, response, usage, model)，用于 Agent trace。
    """
    mandate = mandates.parse(mandates.load(inputs["mandate"]))
    sd = seedlib.normalize(inputs.get("seeds")) if inputs.get("seeds") else None
    alpha = evidence.alpha158_findings(inputs["markets"], inputs["horizon"], inputs.get("groups") or None)
    past = evidence.past_runs(inputs["markets"], inputs["horizon"]) if inputs.get("include_past_runs", True) else []
    internal = internal_knowledge(inputs["hypothesis"] + " " + (inputs.get("direction") or ""))
    external = [x for x in (inputs.get("external_evidence") or []) if isinstance(x, dict)][:8]
    external_text = "\n".join(
        f"- [W{i}] {x.get('title')} ({x.get('published') or 'date unknown'}; {x.get('url')}) {str(x.get('snippet') or '')[:600]}"
        for i, x in enumerate(external, 1)
    ) or "- none"
    prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
        internal="\n".join(f"- [{x['kind']}] {x['title']}: {x['prompt']}" for x in internal) or "- none", language=llm_language(),
        markets=", ".join(inputs["markets"]), horizon=inputs["horizon"],
        direction=inputs.get("direction") or "(not given)", hypothesis=inputs["hypothesis"],
        mandate_name=mandate["name"],
        mandate_constraints="\n".join(f"- {c}" for c in mandate["constraints"]) or "- none",
        mandate_free_text="\n".join(f"- {t}" for t in mandate["free_text"]) or "- none",
        evidence=_evidence_text(alpha),
        seeds_section=_seeds_section(sd),
        past_runs="\n".join(f"- {line}" for line in evidence.past_runs_lines(past)) or "- none (runs affected by data leakage are excluded)",
        external=external_text,
    )
    cfg = llm_config()
    model_used = cfg["model"]
    t0 = time.time()
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    inputs = {**inputs, "_free_text": mandate["free_text"]}
    retry_note = ""
    for attempt in range(2):  # JSON 格式、必填字段或语言不合格时，带上原因重试一次
        text, usage = _chat(prompt + retry_note, cfg, "研究规格起草")
        model_used = usage.get("model") or model_used
        if on_llm:
            try:
                on_llm(prompt + retry_note, text, usage, model_used)
            except Exception:                       # 审计回调失败不应影响起草
                pass
        for k in tokens:
            tokens[k] += int(usage.get(k) or 0)
        try:
            draft = _validate(json.loads(text), inputs)
            extra = [str(x).strip() for x in inputs.get("exclusions") or [] if str(x).strip()]
            if extra:                                   # 研究员带入的排除方向必须保留，不能只靠 AI 草稿
                draft["exclusions"] = list(dict.fromkeys([*extra, *draft["exclusions"]]))
            break
        except (json.JSONDecodeError, DraftError) as e:
            if attempt == 1:
                raise DraftError(lang.t("大模型两次输出都不合格：{e}", e=e)) from e
            retry_note = f"\n\nYour previous answer was rejected: {e}. Fix it and answer again with the full JSON object."
    draft["region_diff"] = regions.differences(inputs["markets"], mandates.load(inputs["mandate"]), draft["data_needs"])
    draft["system_questions"] = _system_questions(draft, inputs)
    idea_ids = list(dict.fromkeys([*([inputs["idea_id"]] if inputs.get("idea_id") else []), *(inputs.get("idea_ids") or [])]))
    if idea_ids:                                               # 来自研究线索池（第 17 章）：每条来源线索的防泄漏与文档许可须确认
        from factopia import ideas
        for iid in idea_ids:
            draft["system_questions"] += ideas.confirm_questions(iid)
    draft["constraints"] = mandate["constraints"] + [t["constraint_en"] for t in draft["mandate_interpretations"] if t["constraint_en"]] + PLATFORM_CONSTRAINTS
    if sd:
        draft["seeds"] = sd                                  # 起点因子随规格锁定，进提示词（context.research_section）
        p = sd.get("proposal") or {}
        if p.get("from_ai"):
            draft["system_questions"].append(
                f"研究起点与候选方向来自 AI 构想（模型 {p.get('model') or '—'}，提示词 {p.get('prompt_version') or '—'}）："
                f"引用因子 {'、'.join(p.get('cites') or [])}；改动方式「{p.get('change') or '—'}」，主要风险「{p.get('risk_zh') or '—'}」。")
    return {
        "draft": draft,
        "evidence": {"alpha158": alpha, "past_runs": past, "groups": inputs.get("groups") or [], "internal": internal},
        "mandate": mandate,
        "meta": {"model": model_used, "prompt_version": PROMPT_VERSION, **tokens, "idea_id": inputs.get("idea_id"),
                 "idea_ids": idea_ids, "combo_id": inputs.get("combo_id"), "based_on": inputs.get("based_on"),
                 "external_evidence": [{k: x.get(k) for k in ("title", "url", "published", "snippet", "source")} for x in external],
                 "seconds": round(time.time() - t0, 1), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
