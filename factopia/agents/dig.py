"""公告深挖（WF-04）：情报台内公司公告的有界检索-提取-决策循环（docs/Agentic-workflow-情报台.md）。

PLAN → REWRITE → SEARCH → (ANNOTATE / SUMMARIZE) → EXTRACT 循环，最多 max_rounds 轮（3–5）：
- plan：Pro LLM 从白名单选下一步（query / finish / give_up），query 可带 annotate_first / summarize_first 开关；
- rewrite：Lite LLM 把查询扩写为多语言术语与概念（失败回退原文查询，不阻断）；
- search：确定性代码，复用 factopia/text/search.py 跨语言检索，命中只保留种子公司公告；
- annotate / summarize：可选原语，复用 factopia/text/understand.py 的 llm_label 与 search.summarize（标注进现有复核队列）；
- extract：Pro LLM 从命中段落提取结构化风险事实（引用强制、证据须为原文片段、日期取自命中公告），编入 [F#]；
- finish / give_up：深挖结论（Pro：风险时间线 + 概念分组 + 与文本信号对照）或数据不足结论（Lite，代码兜底）；
- web_search（可选，勾选「联网搜索」时）：首轮规划前有界检索公开资料（门户网站 / 彭博社 / 经济论坛等，
  复用 research/websearch.py，不限日期），结果仅作启发、来源需人工核验，不作为平台事实。

与 WF-03（agents/brief.py）同款基础设施：文件检查点、trace.jsonl、费用账本、runner 任务取消。
记录：runtime/digs/<dig_id>/（dig.json、rounds/<n>/、trace.jsonl；联网资料在 rounds/000/websearch.json）。
"""
import json
import math
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from factopia.agents import llm
from factopia.lang import llm_language
from factopia.text.common import DATASETS, concept_names, ds_dir, glossary, is_table, read_json

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
if not RUNTIME.exists():                       # 本地直接运行（不在容器内）时回退到仓库的 runtime/
    RUNTIME = Path(__file__).resolve().parents[2] / "runtime"
DIGS_DIR = RUNTIME / "digs"
DIG_ID = re.compile(r"^dig_\d{14}_[0-9a-f]{4}$")
API_UID = int(os.environ.get("FACTOPIA_API_UID", "10001"))   # runner 以 root 运行；新文件交给 API 用户读写

TERMINAL = ("done", "insufficient", "stopped", "failed")
RUNNING = ("queued", "web_searching", "planning", "rewriting", "searching", "annotating", "summarizing",
           "extracting", "concluding")
TOOL_DESC = {
    "query": "Search the seed company's announcements with a cross-lingual query; this round rewrites it, "
             "searches, and extracts risk facts from the hits. "
             "args: {\"query\": \"...\", \"annotate_first\": false, \"summarize_first\": false}",
    "finish": "The extracted facts are sufficient: write the risk profile (timeline + concept groups + comparison "
              "with the platform signal). args: {}",
    "give_up": "The company's announcements cannot support a risk profile. "
               "args: {\"reason\": \"...\", \"suggestion\": \"...\"}",
}
WEB_QUERIES = ("{company} {instrument}", "{company} 风险 诉讼 监管 负面", "{company} 业绩 展望 评级 新闻")
WEB_COUNT = 5          # 每个查询最多保留的结果条数
WEB_TOTAL = 15         # 全部查询合计最多保留的结果条数


class DigError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return f"dig_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{secrets.token_hex(2)}"


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _dumps(obj, indent: int | None = None) -> str:
    return json.dumps(_json_safe(obj), ensure_ascii=False, indent=indent, allow_nan=False)


def _own(path: Path) -> None:
    """runner 以 root 写文件时，把文件与目录交给 API 用户（10001）。"""
    if os.geteuid() != 0:
        return
    try:
        for p in (path, path.parent):
            os.chown(p, API_UID, API_UID)
            if p.is_dir():
                os.chmod(p, 0o775)
            else:
                os.chmod(p, 0o664)
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    _own(path)


# ---------------------------------------------------------------- 存储

def dir_path(dig_id: str) -> Path:
    if not DIG_ID.match(dig_id or ""):
        raise DigError(f"公告深挖编号无效：{dig_id}")
    return DIGS_DIR / dig_id


def create(dataset: str, company: str, instrument: str, filing: dict | None = None, policy: dict | None = None,
           web_search: bool = False) -> dict:
    """新建一条公告深挖记录；同一公司不能有第二个进行中的深挖（幂等）。"""
    if dataset not in DATASETS:
        raise DigError(f"数据集不存在：{dataset}")
    if not str(instrument or "").strip():
        raise DigError("缺少公司证券代码（instrument）")
    if not (ds_dir(dataset) / "aligned.parquet").exists():
        raise DigError("该数据集尚未生成理解与信号结果（先运行 python -m factopia.text all --yes）")
    for rec in list_digs():
        if rec.get("dataset") == dataset and rec.get("instrument") == instrument and rec.get("state") not in TERMINAL:
            raise DigError("该公司已有进行中的公告深挖")
    if web_search:
        from factopia.research import websearch
        if not websearch.configured():
            raise DigError("未配置联网搜索：在 .env 中设置 FACTOPIA_LLM_API_KEY（百炼内置搜索）"
                           "或 FACTOPIA_SEARCH_API_KEY（博查 / Tavily）")
    summary = read_json(ds_dir(dataset) / "summary.json") or {}
    filings = [f for f in summary.get("filings") or [] if f.get("instrument") == instrument]
    seed = {
        "dataset": dataset,
        "company": str(company or "").strip()[:200] or instrument,
        "instrument": instrument,
        "market": DATASETS[dataset]["market"],
        "simulated": bool(DATASETS[dataset]["simulated"]),
        "n_filings": len(filings) or None,
        "filing": _seed_filing(filing),
    }
    rec = {
        "dig_id": new_id(), "dataset": dataset, "instrument": instrument, "company": seed["company"],
        "state": "queued", "created_at": now(), "updated_at": now(),
        "seed": seed, "policy": policy, "round": 0,
        "max_rounds": int((policy or {}).get("max_rounds") or 4),
        "web_search": bool(web_search), "web": None,
        "facts": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "cost_usd": 0.0, "outcome": None, "conclusion": None, "stop_reason": None, "last_error": None,
    }
    save(rec)
    append_trace(rec["dig_id"], {"kind": "state", "from": None, "to": "queued", "note": "创建公告深挖"})
    return rec


def _seed_filing(filing: dict | None) -> dict | None:
    if not filing:
        return None
    out = {"id": filing.get("id"), "title": str(filing.get("title") or "").strip()[:300]}
    for k in ("published_at", "tone_total", "change"):
        if filing.get(k) is not None:
            out[k] = filing[k]
    if filing.get("concepts"):
        out["concepts"] = [str(c)[:40] for c in filing["concepts"]][:4]
    return out or None


def save(rec: dict) -> None:
    rec["updated_at"] = now()
    _atomic_write(dir_path(rec["dig_id"]) / "dig.json", _dumps(rec, indent=1))


def load(dig_id: str) -> dict:
    path = dir_path(dig_id) / "dig.json"
    if not path.exists():
        raise DigError(f"公告深挖不存在：{dig_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_digs() -> list[dict]:
    if not DIGS_DIR.exists():
        return []
    out = []
    for path in sorted(DIGS_DIR.glob("dig_*/dig.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda d: d.get("created_at") or "", reverse=True)


def latest(dataset: str, instrument: str) -> dict | None:
    for rec in list_digs():
        if rec.get("dataset") == dataset and rec.get("instrument") == instrument:
            return rec
    return None


def round_dir(dig_id: str, n: int) -> Path:
    return dir_path(dig_id) / "rounds" / f"{int(n):03d}"


def write_round(dig_id: str, n: int, name: str, data) -> Path:
    path = round_dir(dig_id, n) / f"{name}.json"
    _atomic_write(path, _dumps(data, indent=1))
    return path


def read_round(dig_id: str, n: int, name: str) -> dict | None:
    path = round_dir(dig_id, n) / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def append_trace(dig_id: str, entry: dict) -> None:
    record = {"at": now(), **entry}
    path = dir_path(dig_id) / "trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(_dumps(record) + "\n")
    _own(path)


def add_usage(rec: dict, usage: dict | None) -> float:
    """累计 token 与费用；usage 含 usd 时直接累加，否则按单价换算。"""
    usage = usage or {}
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    usd = float(usage.get("usd") or 0.0)
    if not usd and (pt or ct):
        from factopia import llm_cost
        usd = llm_cost.usd(pt, ct)
    rec["usage"]["prompt_tokens"] += pt
    rec["usage"]["completion_tokens"] += ct
    rec["cost_usd"] = round(float(rec.get("cost_usd") or 0.0) + usd, 6)
    save(rec)
    return usd


def _advance(rec: dict, target: str, note: str | None = None) -> None:
    current = rec.get("state")
    rec["state"] = target
    if target in TERMINAL and note:
        rec["stop_reason"] = note
    save(rec)
    append_trace(rec["dig_id"], {"kind": "state", "from": current, "to": target,
                                 **({"note": note} if note else {})})


def _over_budget(rec: dict) -> bool:
    return float(rec.get("cost_usd") or 0.0) >= float(rec["policy"].get("budget_usd") or 0.0)


# ---------------------------------------------------------------- 提示词素材

def _seed_text(rec: dict) -> str:
    s = rec["seed"]
    src = "模拟数据（仅用于流程演示）" if s.get("simulated") else "真实公告"
    lines = [f"{s['company']} ({s['instrument']}), market {s['market']}, dataset {s['dataset']}（{src}）"]
    if s.get("n_filings"):
        lines.append(f"announcements in dataset: {s['n_filings']}")
    if s.get("filing"):
        f = s["filing"]
        lines.append(f"start filing: {f.get('title')}（published {f.get('published_at')}"
                     + (f"，风险语气 {f['tone_total']}" if f.get("tone_total") is not None else "")
                     + (f"，较上期变化 {f['change']}" if f.get("change") is not None else "")
                     + (f"，概念：{'、'.join(f['concepts'])}" if f.get("concepts") else "") + "）")
    return "\n".join(lines)


def _facts_text(facts: list[dict]) -> str:
    if not facts:
        return "- none"
    lines = []
    for f in facts:
        when = f" {f.get('date')}" if f.get("date") else ""
        direction = "加重" if f.get("direction") == 1 else "缓解"
        ev = f" — 证据 {f.get('evidence')!r}" if f.get("evidence") else ""
        lines.append(f"[{f['id']}] ({f.get('name') or f.get('concept')}·{direction}，{when}) {f.get('statement')}{ev}")
    return "\n".join(lines)


def _glossary_text() -> str:
    g = glossary()["concepts"]
    return "\n".join(f"- {c}: {s['zh_name']} / {s['en_name']} / {s['fr_name']}" for c, s in g.items())


def _signal_text(rec: dict) -> str:
    """该公司在平台文本信号里的语气与变化走势（确定性数据，来自 summary.json）。"""
    s = read_json(ds_dir(rec["dataset"]) / "summary.json") or {}
    rows = [f for f in s.get("filings") or [] if f.get("instrument") == rec["instrument"]]
    if not rows:
        return "（该数据集没有这家公司的信号记录）"
    names = concept_names()
    lines = ["平台文本信号（RISK_CHANGE：风险语气 = 每百段净风险表述；change = 与本公司同一系列的上一期比较）："]
    for f in rows[-8:]:
        cs = "、".join(f"{names.get(c, c)} {v}" for c, v in (f.get("changes") or {}).items() if v is not None)
        lines.append(f"- {f.get('published_at')} 语气 {f.get('tone_total')}，变化 {f.get('change')}"
                     + (f"（{cs}）" if cs else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- 联网搜索（可选，启动时）

def _web_search(rec: dict) -> None:
    """勾选「联网搜索」时：首轮规划前有界检索公开资料（门户网站 / 彭博社 / 经济论坛等）。

    复用 research/websearch.py；深挖是定性分析，不限日期（before="" 不过滤）。结果只取标题 /
    摘要 / URL / 日期，仅作启发、来源需人工核验，不作为平台事实；失败不阻断深挖。
    """
    from factopia.research import websearch
    out = {"configured": None, "provider": None, "model": None, "warning": None,
           "queries": [], "results": [], "error": None, "at": now()}
    if _over_budget(rec):
        out["error"] = "费用达到预算上限，跳过联网检索"
        _save_web(rec, out)
        return
    seen, rows = set(), []
    for q in WEB_QUERIES:
        query = q.format(company=rec["company"], instrument=rec["instrument"])
        r = {"configured": True, "provider": None, "model": None, "results": [], "error": None}
        try:
            r = websearch.search(query, before="", count=WEB_COUNT)
        except Exception as e:
            r = {**r, "error": str(e)[:300]}
        out["configured"] = r.get("configured")
        out["provider"] = r.get("provider")
        out["model"] = r.get("model")
        out["warning"] = r.get("warning")
        items = [{"title": str(x.get("title") or "")[:300], "url": str(x.get("url") or "")[:1000],
                  "snippet": str(x.get("snippet") or "")[:1200], "published": x.get("published"),
                  "source": x.get("source")} for x in (r.get("results") or [])]
        out["queries"].append({"query": query, "n": len(items), "error": r.get("error")})
        for it in items:
            key = (it["title"][:80], it["url"][:120])
            if key in seen:
                continue
            seen.add(key)
            rows.append(it)
        if r.get("error"):
            out["error"] = r["error"]
    out["results"] = rows[:WEB_TOTAL]
    _save_web(rec, out)


def _save_web(rec: dict, out: dict) -> None:
    write_round(rec["dig_id"], 0, "websearch", out)
    append_trace(rec["dig_id"], {"kind": "tool", "action": "websearch",
                                 "results": len(out.get("results") or []), "error": out.get("error")})
    rec["web"] = {k: out.get(k) for k in ("configured", "provider", "model", "warning",
                                          "queries", "results", "error", "at")}
    save(rec)


def _web_text(rec: dict) -> str:
    """给 Planner / 结论提示词的联网资料（不可信输入：仅启发，引用编号 [W#]，不作为平台事实）。"""
    if not rec.get("web_search"):
        return "- web search was not enabled"
    w = rec.get("web") or {}
    results = w.get("results") or []
    if not results:
        return f"- no web results{('（' + str(w.get('error')) + '）') if w.get('error') else ''}"
    lines = [f"[W{n}] {r.get('title')}（{r.get('source') or '来源未知'}，{r.get('published') or '日期未知'}）："
             f"{r.get('snippet')} — {r.get('url') or '无 URL'}" for n, r in enumerate(results, 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------- Planner

def _tools_text(policy: dict) -> tuple[str, list[str]]:
    available = [t for t in (policy.get("tools") or []) if t in TOOL_DESC]
    return "\n".join(f"- {t}: {TOOL_DESC[t]}" for t in available), available


def _clean_query(q) -> str:
    q = re.sub(r"\[[FS]\d+\]", " ", str(q or ""))      # 不带 [F#]/[S1] 标记进检索
    return re.sub(r"\s+", " ", q).strip()[:300]


def _validate_plan(data: dict, fact_ids: set[str], available: list[str]) -> dict:
    action = str(data.get("action") or "").strip()
    if action not in available:
        raise DigError(f"动作 {action!r} 不在白名单中；可用：{', '.join(available)}")
    rationale = data.get("rationale")
    if isinstance(rationale, str):
        rationale = {"text": rationale, "cites": data.get("cites") or []}
    if not isinstance(rationale, dict) or not str(rationale.get("text") or "").strip():
        raise DigError("rationale.text 不能为空")
    cites = [str(c) for c in (rationale.get("cites") or []) if str(c) in fact_ids or str(c) == "S1"]
    kept = {"text": str(rationale["text"]).strip()[:1000], "cites": cites}
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise DigError("args 必须是 JSON 对象")
    if action == "query":
        if not cites:
            raise DigError("rationale 必须引用 [S1] 或有效事实 [F#]")
        query = _clean_query(args.get("query"))
        if not query:
            raise DigError("query 需要 args.query")
        return {"action": "query", "rationale": kept,
                "args": {"query": query,
                         "annotate_first": bool(args.get("annotate_first")),
                         "summarize_first": bool(args.get("summarize_first"))}}
    if action == "finish":
        if not [c for c in cites if c in fact_ids]:
            raise DigError("finish 必须引用至少一条有效事实 [F#]（先提取一轮再收尾）")
        return {"action": "finish", "rationale": kept, "args": {}}
    if action == "give_up":
        reason = str(args.get("reason") or "").strip()
        if not reason:
            raise DigError("give_up 需要 args.reason")
        out_args = {"reason": reason[:1000]}
        suggestion = str(args.get("suggestion") or "").strip()
        if suggestion:
            out_args["suggestion"] = suggestion[:500]
        return {"action": "give_up", "rationale": kept, "args": out_args}
    raise DigError(f"未知动作 {action!r}")


def _plan(rec: dict, chat) -> dict:
    version = rec["policy"]["prompts"]["planner"]
    tools_text, available = _tools_text(rec["policy"])
    prompt = llm.prompt(
        version, language=llm_language(), seed=_seed_text(rec),
        tools=tools_text, facts=_facts_text(rec["facts"]), web=_web_text(rec),
        round=rec["round"], max_rounds=rec["max_rounds"],
        remaining=max(0, int(rec["max_rounds"]) - int(rec["round"])),
    )
    note = ""
    for attempt in range(2):
        raw, usage, model = chat(prompt + note, "公告深挖规划")
        append_trace(rec["dig_id"], {"kind": "llm", "role": "planner", "round": rec["round"],
                                     "model": model, "usage": usage, "prompt_version": version,
                                     "prompt": prompt + note, "response": raw})
        try:
            data = llm.parse_json(raw)
            out = _validate_plan(data, {f["id"] for f in rec["facts"]}, available)
            out.update({"model": model, "prompt_version": version, "usage": usage, "at": now()})
            return out
        except (llm.LLMError, DigError) as e:
            note = f"\n\nYour previous answer was rejected: {e}. Answer again with the required JSON object."
    raise DigError(f"Planner 两次都没有给出合法计划：{note.strip()}")


# ---------------------------------------------------------------- 改写（Lite）

def _validate_rewrite(data: dict, fallback: str) -> dict:
    q = _clean_query(data.get("query")) or fallback
    terms = [str(x).strip()[:40] for x in (data.get("terms") or []) if str(x).strip()][:8]
    known = set(concept_names())
    concepts = [str(c) for c in (data.get("concepts") or []) if str(c) in known][:4]
    return {"query": q, "terms": terms, "concepts": concepts}


def _rewrite(rec: dict, plan: dict, chat) -> dict:
    """Lite LLM 扩写查询；失败回退原文查询，不阻断本轮。"""
    version = rec["policy"]["prompts"]["rewrite"]
    query = plan["args"]["query"]
    prompt = llm.prompt(version, language=llm_language(), query=query, glossary=_glossary_text())
    out = _validate_rewrite({"query": query, "terms": [], "concepts": []}, query)
    out.update({"model": None, "usage": None, "at": now()})
    try:
        raw, usage, model = chat(prompt, "公告深挖查询改写", lite=True)
        append_trace(rec["dig_id"], {"kind": "llm", "role": "rewrite", "round": rec["round"],
                                     "model": model, "usage": usage, "prompt_version": version,
                                     "prompt": prompt, "response": raw})
        out.update(_validate_rewrite(llm.parse_json(raw), query))
        out.update({"model": model, "usage": usage, "at": now()})
    except Exception as e:
        out["error"] = str(e)[:500]
        append_trace(rec["dig_id"], {"kind": "rewrite_error", "round": rec["round"], "error": str(e)[:500]})
    return out


# ---------------------------------------------------------------- 检索（确定性）

def _ts(v) -> str | None:
    return str(v)[:19] if v is not None else None


def _hit_view(h: dict) -> dict:
    """把 search() 的命中规整成可 JSON 化的精简视图（日期统一为字符串）。"""
    concepts = [{"concept": c.get("concept"), "name": c.get("name"), "direction": c.get("direction"),
                 "confidence": c.get("confidence")} for c in (h.get("concepts") or [])][:8]
    return {"doc_id": h.get("doc_id"), "paragraph": h.get("paragraph"), "company": h.get("company"),
            "instrument": h.get("instrument"), "title": h.get("title"), "language": h.get("language"),
            "published_at": _ts(h.get("published_at")), "business_date": _ts(h.get("business_date")),
            "available_at": _ts(h.get("available_at")), "effective_date": _ts(h.get("effective_date")),
            "original": h.get("original"), "translation": h.get("translation") or {},
            "concepts": concepts, "matched": h.get("matched") or [],
            "review_status": h.get("review_status"), "simulated": bool(h.get("simulated")),
            "url": h.get("url")}


def _search(rec: dict, plan: dict, rewrite: dict) -> dict:
    from factopia.text import search as tsearch
    try:
        out = tsearch.search(rewrite["query"], [rec["dataset"]], rewrite["concepts"],
                             int(rec["policy"].get("max_hits") or 20))
    except Exception as e:
        return {"query": rewrite["query"], "concepts": rewrite["concepts"], "terms": rewrite.get("terms"),
                "hits": [], "total": 0, "scoped_out": 0, "error": str(e)[:500], "at": now()}
    raw = out.get("results") or []
    hits, scoped = [], 0
    for h in raw:
        if h.get("instrument") != rec["instrument"]:       # 范围只限本公司公告
            scoped += 1
            continue
        if is_table(h.get("original") or ""):              # 纯数字表格行不交给提取
            continue
        hits.append(_hit_view(h))
    return {"query": rewrite["query"], "concepts": rewrite["concepts"], "terms": rewrite.get("terms"),
            "hits": hits, "total": len(raw), "scoped_out": scoped, "at": now()}


# ---------------------------------------------------------------- 标注 / 摘要原语（可选，Pro）

def _annotate_hits(rec: dict, hits: list[dict], chat) -> dict:
    """对命中里未标注或仅词典法的段落补概念标注（复用 understand.llm_label；写入现有 labels 文件，进复核队列）。"""
    from factopia.research import draft as draft_mod
    from factopia.text import understand
    if not hits:
        return {"labelled": 0, "skipped": True, "reason": "no_hits", "at": now()}
    if _over_budget(rec):
        return {"labelled": 0, "skipped": True, "reason": "budget", "at": now()}
    try:
        cfg = draft_mod.llm_config()
    except draft_mod.DraftError as e:
        return {"labelled": 0, "skipped": True, "reason": f"llm_config: {e}", "at": now()}
    by_doc: dict[str, dict] = {}
    for h in hits:
        slot = by_doc.setdefault(h["doc_id"], {"lang": h.get("language"), "paragraphs": set()})
        slot["paragraphs"].add(int(h["paragraph"]))
    labelled, usage_total, errors = 0, {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}, []
    d = ds_dir(rec["dataset"])
    for doc_id, slot in by_doc.items():
        lang = slot["lang"]
        lab_path = d / "labels" / f"{doc_id}.json"
        existing = read_json(lab_path) or {"labels": []}
        have_llm = {x["i"] for x in existing.get("labels") or [] if x.get("method") == "llm"}
        need = sorted(p for p in slot["paragraphs"] if p not in have_llm)
        if not need:
            continue
        doc = read_json(d / "docs" / f"{doc_id}.json") or {"paragraphs": []}
        by_i = {p["i"]: p for p in doc["paragraphs"]}
        batch = [by_i[p] for p in need if p in by_i]
        if not batch:
            continue
        try:
            got, usage = understand.llm_label(batch, lang, cfg)
        except Exception as e:
            errors.append(f"{doc_id}: {str(e)[:200]}")
            continue
        for k in usage_total:
            usage_total[k] += float(usage.get(k) or 0)
        labels = [x for x in existing.get("labels") or []]
        by_label_i = {x["i"]: x for x in labels}
        for p in batch:
            r = got.get(p["i"]) or {**understand.lexicon_label(p["text"], lang), "fallback": True}
            m = "lexicon" if r.get("fallback") else "llm"
            entry = {"i": p["i"], "sha": p.get("sha"), "method": m,
                     **{k: v for k, v in r.items() if k != "fallback"},
                     "flags": understand._flags(r, lang, p["text"], m)}
            if p["i"] in by_label_i:
                by_label_i[p["i"]] = entry
            else:
                labels.append(entry)
        labels.sort(key=lambda x: x["i"])
        u0 = existing.get("usage") or {}
        out = {**existing,
               "glossary_version": understand.glossary()["version"],
               "refreshed_at": now(),
               "usage": {k: int(u0.get(k) or 0) + int(usage_total[k]) for k in ("prompt_tokens", "completion_tokens")},
               "labels": labels}
        write_json_own(lab_path, out)
        labelled += len(batch)
    return {"labelled": labelled, "docs": len(by_doc), "model": cfg["model"], "usage": usage_total,
            "errors": errors, "at": now()}


def write_json_own(path: Path, data) -> None:
    from factopia.text.common import write_json
    write_json(path, data)
    _own(path)


def _summarize(rec: dict, hits: list[dict], query: str, chat) -> dict:
    """复用现有检索摘要原语（逐句引用；编号与提取输入的段落编号一致）。"""
    from factopia.text import search as tsearch
    try:
        out = tsearch.summarize(query, hits)
    except Exception as e:
        return {"query": query, "sentences": [], "error": str(e)[:500], "at": now()}
    return {"query": query, "sentences": out.get("sentences") or [], "dropped": out.get("dropped"),
            "model": out.get("model"), "usage": out.get("usage"), "at": now()}


# ---------------------------------------------------------------- 提取风险事实（Pro）

def _paragraphs_text(hits: list[dict], sentences: list[dict] | None = None) -> str:
    lines = []
    for n, h in enumerate(hits, 1):
        sim = " SIMULATED" if h.get("simulated") else ""
        labels = "、".join(f"{c.get('name')}（{'加重' if c.get('direction') == 1 else '缓解'}，置信度 {c.get('confidence')}）"
                           for c in h.get("concepts") or []) or "无概念标注"
        tr = ""
        for lang in ("zh", "en"):
            if (h.get("translation") or {}).get(lang):
                tr += f" | {lang}: {(h['translation'][lang])}"
        lines.append(f"[{n}]{sim} {h['company']} ({h['instrument']}), {h['title']}, published {h['published_at']}, "
                     f"language {h['language']}: {h['original']}{tr} — 概念标注：{labels} — 复核：{h.get('review_status')}")
    if sentences:
        lines.append("以下为上述段落的 AI 摘要（编号引用上方段落）：")
        lines += [f"- {s.get('text')} [{'] ['.join(str(c) for c in s.get('cites') or [])}]" for s in sentences]
    return "\n".join(lines)


def _validate_facts(hits: list[dict], raw: list) -> tuple[list[dict], list[dict]]:
    """事实校验（无大模型）：引用编号有效、概念已知、方向 ±1、证据为原文/译文片段、日期取自命中公告。"""
    known = concept_names()
    by_n = {n: h for n, h in enumerate(hits, 1)}
    kept, dropped = [], []
    for f in raw or []:
        if not isinstance(f, dict):
            dropped.append({"reason": "not_object"})
            continue
        statement = " ".join(str(f.get("statement") or "").split())[:800]
        cites = sorted({int(c) for c in (f.get("cites") or [])
                        if isinstance(c, (int, float)) and 1 <= int(c) <= len(hits)})
        if not cites:
            dropped.append({"reason": "no_valid_cites", "statement": statement[:200]})
            continue
        concept = f.get("concept")
        if concept not in known:
            dropped.append({"reason": "unknown_concept", "concept": str(concept)[:40], "statement": statement[:200]})
            continue
        try:
            direction = 1 if int(f.get("direction", 1)) >= 0 else -1
        except (TypeError, ValueError):
            dropped.append({"reason": "bad_direction", "statement": statement[:200]})
            continue
        if not statement:
            dropped.append({"reason": "empty_statement"})
            continue
        evidence = " ".join(str(f.get("evidence") or "").split())[:600]
        verbatim = evidence and any(
            evidence in (by_n[c].get("original") or "")
            or any(evidence in tr for tr in (by_n[c].get("translation") or {}).values())
            for c in cites)
        if not verbatim:
            dropped.append({"reason": "evidence_not_verbatim", "statement": statement[:200]})
            continue
        dates = sorted({str(by_n[c].get("published_at") or "")[:10] for c in cites})
        date = dates[0] if dates else None
        if not date:
            dropped.append({"reason": "no_date", "statement": statement[:200]})
            continue
        refs = [{"doc_id": by_n[c]["doc_id"], "paragraph": by_n[c]["paragraph"], "title": by_n[c]["title"]}
                for c in cites][:3]
        review = "pending" if any(by_n[c].get("review_status") == "pending" for c in cites) else None
        kept.append({"concept": concept, "name": known[concept], "direction": direction,
                     "statement": statement, "date": date, "evidence": evidence,
                     "cites": cites, "refs": refs, "review_status": review})
    return kept, dropped


def _add_facts(rec: dict, items: list[dict]) -> list[str]:
    """新事实编入 [F#]；按（概念、日期、正文前缀）去重。"""
    added = []
    seen = {(f.get("concept"), f.get("date"), str(f.get("statement") or "")[:120]) for f in rec["facts"]}
    for it in items:
        key = (it["concept"], it["date"], it["statement"][:120])
        if key in seen:
            continue
        seen.add(key)
        rec["facts"].append({"id": f"F{len(rec['facts']) + 1}", "at": now(), **it})
        added.append(rec["facts"][-1]["id"])
    if added:
        save(rec)
    return added


def _extract(rec: dict, hits: list[dict], sentences: list[dict] | None, chat) -> dict:
    version = rec["policy"]["prompts"]["extract"]
    prompt = llm.prompt(version, language=llm_language(), paragraphs=_paragraphs_text(hits, sentences),
                        facts=_facts_text(rec["facts"]), glossary=_glossary_text())
    raw, usage, model = chat(prompt, "公告深挖提取")
    append_trace(rec["dig_id"], {"kind": "llm", "role": "extract", "round": rec["round"],
                                 "model": model, "usage": usage, "prompt_version": version,
                                 "prompt": prompt, "response": raw})
    try:
        data = llm.parse_json(raw)
        kept, dropped = _validate_facts(hits, data.get("facts") or [])
    except llm.LLMError as e:
        return {"added_facts": [], "dropped": [], "error": str(e), "model": model, "usage": usage, "at": now()}
    added = _add_facts(rec, kept)
    return {"added_facts": added, "dropped": dropped, "model": model, "usage": usage, "at": now()}


# ---------------------------------------------------------------- 结论（Pro / Lite）

def _validate_conclusion(data: dict, rec: dict) -> dict:
    valid = {f["id"] for f in rec["facts"]}
    names = concept_names()
    by_id = {f["id"]: f for f in rec["facts"]}

    def clean(cs):
        return [str(c) for c in (cs or []) if str(c) in valid]

    groups = []
    for g in data.get("groups") or []:
        if not isinstance(g, dict) or g.get("concept") not in names:
            continue
        cites = clean(g.get("cites"))
        if not cites:
            continue
        groups.append({"concept": g["concept"], "name": names[g["concept"]],
                       "summary": " ".join(str(g.get("summary") or "").split())[:1000], "cites": cites})
    timeline = []
    for t in data.get("timeline") or []:
        if not isinstance(t, dict):
            continue
        cites = clean(t.get("cites"))
        if not cites:
            continue
        date = min((by_id[c].get("date") or "") for c in cites)      # 日期取自事实，不接受模型给的日期
        if not date:
            continue
        timeline.append({"date": date, "statement": " ".join(str(t.get("statement") or "").split())[:800],
                         "cites": cites})
    timeline.sort(key=lambda x: x["date"])
    follow_ups = [" ".join(str(x).split())[:300] for x in (data.get("follow_ups") or []) if str(x).strip()][:4]
    return {"summary": " ".join(str(data.get("summary") or "").split())[:2000], "groups": groups,
            "timeline": timeline, "signal_note": " ".join(str(data.get("signal_note") or "").split())[:1000],
            "web_note": " ".join(str(data.get("web_note") or "").split())[:1000],
            "follow_ups": follow_ups}


def _fallback_conclusion(rec: dict) -> dict:
    """结论 LLM 失败时的确定性兜底：直接基于事实清单组装。"""
    facts = sorted(rec["facts"], key=lambda f: (f.get("date") or "", f["id"]))
    groups, timeline = [], []
    for f in facts:
        timeline.append({"date": f.get("date"), "statement": f"{f.get('name')}（{'加重' if f.get('direction') == 1 else '缓解'}）：{f['statement']}",
                         "cites": [f["id"]]})
        g = next((x for x in groups if x["concept"] == f["concept"]), None)
        if g is None:
            groups.append({"concept": f["concept"], "name": f["name"], "summary": f["statement"], "cites": [f["id"]]})
        else:
            g["cites"].append(f["id"])
    return {"summary": f"共提取 {len(facts)} 条风险事实（按公告时间与概念整理如下）。",
            "groups": groups, "timeline": timeline, "signal_note": "", "web_note": "",
            "follow_ups": [], "fallback": True}


def _conclude(rec: dict, chat) -> None:
    """finish / 满轮有事实：Pro LLM 生成风险画像；失败用确定性兜底。"""
    version = rec["policy"]["prompts"]["conclusion"]
    prompt = llm.prompt(version, language=llm_language(), seed=_seed_text(rec),
                        facts=_facts_text(rec["facts"]), signal=_signal_text(rec), web=_web_text(rec))
    out = {"summary": "", "groups": [], "timeline": [], "signal_note": "", "web_note": "", "follow_ups": [],
           "model": None, "usage": None, "at": now()}
    if not _over_budget(rec):
        try:
            raw, usage, model = chat(prompt, "公告深挖结论")
            append_trace(rec["dig_id"], {"kind": "llm", "role": "conclusion", "round": rec["round"],
                                         "model": model, "usage": usage, "prompt_version": version,
                                         "prompt": prompt, "response": raw})
            out.update(_validate_conclusion(llm.parse_json(raw), rec))
            out.update({"model": model, "usage": usage, "at": now()})
        except Exception as e:
            append_trace(rec["dig_id"], {"kind": "conclusion_error", "round": rec["round"], "error": str(e)[:500]})
    if not out["groups"] and not out["timeline"]:
        out.update(_fallback_conclusion(rec))
    write_round(rec["dig_id"], rec["round"], "conclusion", out)
    add_usage(rec, out.get("usage"))
    rec["conclusion"] = {k: out.get(k) for k in ("summary", "groups", "timeline", "signal_note", "web_note",
                                                 "follow_ups", "model", "at", "fallback")}
    rec["outcome"] = "done"
    _advance(rec, "done", f"深挖完成：{len(rec['facts'])} 条事实")


def _give_up(rec: dict, chat, reason: str) -> None:
    """Lite LLM 写数据不足结论；调用失败或预算已超限时用代码兜底，不阻断收尾。"""
    version = rec["policy"]["prompts"]["conclusion"]
    prompt = llm.prompt(version, language=llm_language(), seed=_seed_text(rec),
                        facts=_facts_text(rec["facts"]), signal=_signal_text(rec), web=_web_text(rec))
    prompt += (f"\n\nNote: this deep-dive is being closed without enough facts because: {reason}\n"
               'Return a single JSON object: {"give_up": {"reason": "...", "evidence_gaps": ["..."], "suggestion": "..."}}')
    conclusion = {"reason": reason, "evidence_gaps": [], "suggestion": "",
                  "model": None, "usage": None, "at": now()}
    if not _over_budget(rec):                       # 预算已超限：不再产生新的 LLM 费用
        try:
            raw, usage, model = chat(prompt, "公告深挖结论", lite=True)
            append_trace(rec["dig_id"], {"kind": "llm", "role": "conclusion", "round": rec["round"],
                                         "model": model, "usage": usage, "prompt_version": version,
                                         "prompt": prompt, "response": raw})
            data = llm.parse_json(raw).get("give_up") or {}
            conclusion = {"reason": str(data.get("reason") or reason)[:1000],
                          "evidence_gaps": [str(x)[:300] for x in (data.get("evidence_gaps") or [])][:6],
                          "suggestion": str(data.get("suggestion") or "")[:500],
                          "model": model, "usage": usage, "at": now()}
        except Exception as e:
            append_trace(rec["dig_id"], {"kind": "conclusion_error", "round": rec["round"], "error": str(e)[:500]})
    write_round(rec["dig_id"], rec["round"], "conclusion", {"give_up": conclusion, "at": now()})
    add_usage(rec, conclusion.get("usage"))
    rec["conclusion"] = {"give_up": {k: conclusion.get(k) for k in ("reason", "evidence_gaps", "suggestion")},
                         "model": conclusion.get("model"), "at": conclusion.get("at")}
    rec["outcome"] = "insufficient"
    _advance(rec, "insufficient", "深挖结论：数据不足")


# ---------------------------------------------------------------- 主循环

def advance(rec: dict, chat=None, should_stop=None) -> dict:
    """推进深挖循环直到终态；每个阶段已写检查点，中断后重新排队可续跑。"""
    chat = chat or llm.chat
    try:
        while rec.get("state") not in TERMINAL:
            if should_stop and should_stop():
                _advance(rec, "stopped", "已取消")
                break
            state = rec.get("state")
            if state == "queued":
                _advance(rec, "web_searching" if rec.get("web_search") else "planning")
                continue
            if state == "web_searching":
                _web_search(rec)
                _advance(rec, "planning")
                continue
            if state == "planning":
                if _over_budget(rec):
                    _advance(rec, "closing", "费用达到预算上限，收尾")
                    continue
                rec["round"] = int(rec.get("round") or 0) + 1
                save(rec)
                try:
                    plan = _plan(rec, chat)
                except DigError as e:
                    write_round(rec["dig_id"], rec["round"], "plan", {"error": str(e)})
                    _advance(rec, "closing", f"规划失败：{e}")
                    continue
                write_round(rec["dig_id"], rec["round"], "plan", plan)
                add_usage(rec, plan.get("usage"))
                _advance(rec, "rewriting" if plan["action"] == "query" else "concluding")
                continue
            if state == "rewriting":
                plan = read_round(rec["dig_id"], rec["round"], "plan") or {}
                out = _rewrite(rec, plan, chat)
                write_round(rec["dig_id"], rec["round"], "rewrite", out)
                add_usage(rec, out.get("usage"))
                _advance(rec, "searching")
                continue
            if state == "searching":
                plan = read_round(rec["dig_id"], rec["round"], "plan") or {}
                rewrite = read_round(rec["dig_id"], rec["round"], "rewrite") or {}
                out = _search(rec, plan, rewrite)
                write_round(rec["dig_id"], rec["round"], "search", out)
                append_trace(rec["dig_id"], {"kind": "tool", "round": rec["round"], "action": "search",
                                             "query": out.get("query"), "hits": len(out.get("hits") or []),
                                             "scoped_out": out.get("scoped_out"), "error": out.get("error")})
                args = plan.get("args") or {}
                if out.get("hits"):
                    if args.get("annotate_first"):
                        _advance(rec, "annotating")
                        continue
                    if args.get("summarize_first"):
                        _advance(rec, "summarizing")
                        continue
                _advance(rec, "extracting")
                continue
            if state == "annotating":
                plan = read_round(rec["dig_id"], rec["round"], "plan") or {}
                search = read_round(rec["dig_id"], rec["round"], "search") or {}
                out = _annotate_hits(rec, search.get("hits") or [], chat)
                write_round(rec["dig_id"], rec["round"], "annotate", out)
                add_usage(rec, out.get("usage"))
                append_trace(rec["dig_id"], {"kind": "tool", "round": rec["round"], "action": "annotate",
                                             "labelled": out.get("labelled"), "skipped": out.get("skipped")})
                _advance(rec, "summarizing" if (plan.get("args") or {}).get("summarize_first") else "extracting")
                continue
            if state == "summarizing":
                plan = read_round(rec["dig_id"], rec["round"], "plan") or {}
                search = read_round(rec["dig_id"], rec["round"], "search") or {}
                out = _summarize(rec, search.get("hits") or [], search.get("query") or "", chat)
                write_round(rec["dig_id"], rec["round"], "summarize", out)
                add_usage(rec, out.get("usage"))
                append_trace(rec["dig_id"], {"kind": "tool", "round": rec["round"], "action": "summarize",
                                             "sentences": len(out.get("sentences") or []), "error": out.get("error")})
                _advance(rec, "extracting")
                continue
            if state == "extracting":
                search = read_round(rec["dig_id"], rec["round"], "search") or {}
                hits = search.get("hits") or []
                if hits:
                    summary = read_round(rec["dig_id"], rec["round"], "summarize") or {}
                    out = _extract(rec, hits, summary.get("sentences"), chat)
                else:
                    out = {"skipped": True, "reason": "no_hits", "added_facts": [], "dropped": [], "at": now()}
                write_round(rec["dig_id"], rec["round"], "extract", out)
                add_usage(rec, out.get("usage"))
                append_trace(rec["dig_id"], {"kind": "tool", "round": rec["round"], "action": "extract",
                                             "added_facts": out.get("added_facts"),
                                             "dropped": len(out.get("dropped") or []),
                                             "skipped": out.get("skipped"), "error": out.get("error")})
                if int(rec["round"]) >= int(rec["max_rounds"]):
                    _advance(rec, "closing", "达到最大轮数，收尾")
                    continue
                _advance(rec, "planning")
                continue
            if state == "concluding":
                plan = read_round(rec["dig_id"], rec["round"], "plan") or {}
                if plan.get("action") == "give_up":
                    _give_up(rec, chat, (plan.get("args") or {}).get("reason") or "数据不足")
                else:
                    _conclude(rec, chat)
                continue
            if state == "closing":
                if _over_budget(rec):
                    _give_up(rec, chat, "费用达到预算上限，收尾")
                elif rec["facts"]:
                    _advance(rec, "concluding", "满轮收尾：生成深挖结论")
                else:
                    _give_up(rec, chat, "没有提取到风险事实")
                continue
    except Exception as e:
        rec["last_error"] = str(e)[:2000]
        if rec.get("state") not in TERMINAL:
            try:
                _advance(rec, "failed")
            except Exception:
                rec["state"] = "failed"
                save(rec)
        raise
    save(rec)
    return rec


def advance_by_id(dig_id: str, chat=None, should_stop=None) -> dict:
    """runner 入口：从磁盘加载深挖记录后推进到终态。"""
    return advance(load(dig_id), chat=chat, should_stop=should_stop)
