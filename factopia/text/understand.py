"""② 理解：段落 → 统一概念标签、翻译、置信度、复核标记（BR-09、BR-11、BR-12）。

- 预筛：只把命中术语表（该语言）的段落交给大模型，控制调用成本；未命中的段落视为不涉及四类风险。
- 大模型（.env 中与研究规格起草相同的配置）：每批最多 BATCH 段，输出 JSON：各概念的方向（+1 风险加重 / -1 缓解或否定）、
  是否否定句、置信度、原文证据，以及中文 / 英文译文与翻译置信度。
- 未配置大模型或加 --lexicon：用术语表做词典法标注（置信度 0.5，无译文），全部进入复核。
- 原文优先：标注结果单独保存在 labels/<公告id>.json，记录模型、提示词版本、术语表版本；原文（docs/）不改动。
- 复核（BR-11）：低置信度、翻译置信度低、否定句、监管措辞、关键财务术语、仅词典法 → 进入语言专家复核队列（review 模块）。
- 缓存：同一段落（按内容哈希）、同一模型与提示词版本不重复调用。
- 费用控制：运行前先估算（estimate：待标注公告 × 预筛段落 × 以往每段实际 token × 单价）；命令行不加 --yes 只显示估算、
  不调用大模型；运行中累计实际费用（接口返回的用量），达到预算上限（--budget，默认 FACTOPIA_TEXT_BUDGET_USD = 2 美元）
  即保存已完成部分并停止，下次运行从未完成的公告继续。每次运行记录在 labels/_runs.jsonl。
"""
import http.client
import json
import math
import os
import time
from datetime import datetime, timezone

import pandas as pd

from factopia import llm_cost
from factopia.research import draft
from factopia.text.common import concept_hits, ds_dir, flag_hits, glossary, pipeline, read_json, write_json

PROMPT_VERSION = "text-label-v1"
BATCH = 10
RETRIES = 3
TRUNCATED_AT = 8000                    # 输出 token 达到约 8192 的上限视为被截断
LOW_CONFIDENCE = 0.6
LOW_TRANSLATION = 0.7
LEXICON_CONFIDENCE = 0.5
DEFAULT_BUDGET = float(os.environ.get("FACTOPIA_TEXT_BUDGET_USD", "2"))
EST_MARGIN = 1.2                       # 估算上浮 20%（重试、段落长短差异）
FALLBACK_TOKENS = (250, 350)           # 没有历史用量时，每个预筛段落的输入 / 输出 token
FLAG_ZH = {
    "low_confidence": "概念判断置信度低", "low_translation_confidence": "翻译置信度低", "negation": "含否定句",
    "regulatory_wording": "含监管措辞", "key_financial_terms": "含关键财务术语", "lexicon_only": "仅词典法标注，未经大模型",
}

PROMPT = """You label sentences from listed-company filings for a cross-border equity research platform.
Concepts (use these ids only):
{concepts}
Direction: +1 = the text says this risk is present / increasing / worsening; -1 = the text says it eased, decreased,
or explicitly did NOT happen (negation such as "demand did not weaken", « la demande ne s'est pas affaiblie », "需求并未转弱").
Only label a concept if the paragraph itself states it about the company or its markets; boilerplate legal disclaimers
("forward-looking statements involve risks") get no concept.
Glossary hints (not exhaustive):
{glossary}
For each paragraph return: concepts (list of {{"concept", "direction", "negated", "confidence" 0-1, "evidence": short quote
copied verbatim from the original}}), translations into {targets} that keep financial terms precise, and
"translation_confidence" 0-1. Never paraphrase the original into the evidence field.
Return JSON: {{"items": [{{"i": <paragraph index>, "concepts": [...], "translation_zh": "...", "translation_en": "...",
"translation_confidence": 0.0}}]}}
Paragraphs (language: {lang}):
{paragraphs}"""


def _targets(lang: str) -> list[str]:
    return [t for t in ("zh", "en") if t != lang]


def _flags(item: dict, lang: str, text: str, method: str) -> list[str]:
    if not item["concepts"]:          # 大模型判断不涉及四类风险（如免责声明）：不进入复核
        return []
    flags = [k for k in flag_hits(text, lang)]
    if any(c.get("negated") for c in item["concepts"]) and "negation" not in flags:
        flags.append("negation")
    if any(c["confidence"] < LOW_CONFIDENCE for c in item["concepts"]):
        flags.append("low_confidence")
    if method == "llm" and item.get("translation_confidence", 1) < LOW_TRANSLATION:
        flags.append("low_translation_confidence")
    if method == "lexicon":
        flags.append("lexicon_only")
    return flags


def lexicon_label(text: str, lang: str) -> dict:
    neg = flag_hits(text, lang).get("negation", [])
    concepts = [{"concept": c, "direction": -1 if neg else 1, "negated": bool(neg),
                 "confidence": LEXICON_CONFIDENCE, "evidence": ", ".join(h)} for c, h in concept_hits(text, lang).items()]
    return {"concepts": concepts, "translation": {}, "translation_confidence": None}


def _clean_llm(raw: dict, known: set[str]) -> dict:
    concepts = []
    for c in raw.get("concepts") or []:
        if not isinstance(c, dict) or c.get("concept") not in known:
            continue
        try:
            direction = 1 if int(c.get("direction", 1)) >= 0 else -1
            conf = min(max(float(c.get("confidence", 0)), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
        concepts.append({"concept": c["concept"], "direction": direction, "negated": bool(c.get("negated")),
                         "confidence": round(conf, 3), "evidence": str(c.get("evidence") or "")[:400]})
    tr = {k: str(raw[f"translation_{k}"]) for k in ("zh", "en") if raw.get(f"translation_{k}")}
    try:
        tconf = round(min(max(float(raw.get("translation_confidence")), 0.0), 1.0), 3)
    except (TypeError, ValueError):
        tconf = None
    return {"concepts": concepts, "translation": tr, "translation_confidence": tconf}


def llm_label(batch: list[dict], lang: str, cfg: dict) -> tuple[dict[int, dict], dict]:
    g = glossary()
    concepts = "\n".join(f"- {c}: {s['en_name']} / {s['zh_name']} / {s['fr_name']}" for c, s in g["concepts"].items())
    gl = "\n".join(f"- {c}: " + "; ".join(s["terms"].get(lang, [])) for c, s in g["concepts"].items())
    paras = "\n".join(f"[{p['i']}] {p['text']}" for p in batch)
    prompt = PROMPT.format(concepts=concepts, glossary=gl, targets=" and ".join(_targets(lang)), lang=lang, paragraphs=paras)
    spent = {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}      # 含失败调用的用量（预算按实际花费累计）
    for attempt in range(RETRIES):      # 网络中断、响应不完整：重试；密钥 / 余额等接口错误直接上报
        try:
            text, usage = draft._chat(prompt, cfg, "公告标注")
        except draft.DraftError as e:
            if "接口返回" in str(e) or attempt == RETRIES - 1:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        except (http.client.HTTPException, OSError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        for k in spent:
            spent[k] += float(usage.get(k) or 0)
        try:
            items = json.loads(text).get("items") or []
            break
        except json.JSONDecodeError:
            # 输出达到长度上限被截断（长段落 + 两种译文）：拆成两半分别判断，重试同一批没有用
            if int(usage.get("completion_tokens") or 0) >= TRUNCATED_AT or attempt == RETRIES - 1:
                if len(batch) == 1:
                    return {}, spent                   # 单段仍超长：交给词典法补（标记 fallback，进入复核）
                h = len(batch) // 2
                a, ua = llm_label(batch[:h], lang, cfg)
                b, ub = llm_label(batch[h:], lang, cfg)
                return {**a, **b}, {k: spent[k] + float(ua.get(k) or 0) + float(ub.get(k) or 0) for k in spent}
    usage = spent
    known = set(g["concepts"])
    out = {}
    for raw in items:
        try:
            out[int(raw["i"])] = _clean_llm(raw, known)
        except (KeyError, TypeError, ValueError):
            continue
    return out, usage


class BudgetReached(Exception):
    pass


def screen(doc: dict, lang: str) -> list[dict]:
    """哪些段落交给大模型：术语表预筛（命中任一概念）或全文（configs/text/pipeline.yaml screening）。"""
    pl = pipeline()
    if pl["screening"] == "full":
        return [p for p in doc["paragraphs"] if len(p["text"].strip()) >= pl["full_min_chars"] or concept_hits(p["text"], lang)]
    return [p for p in doc["paragraphs"] if concept_hits(p["text"], lang)]


def _ckey(model: str, sha: str) -> str:
    return f"{model}:{PROMPT_VERSION}:{sha}"


def load_cache(dataset: str) -> dict:
    """段落缓存。旧版键带术语表版本（模型:提示词:术语表:哈希），读入时去掉术语表版本：
    术语表只决定送哪些段落，同一段落的判断与术语表版本无关，扩充术语表后已判断的段落不再重复付费。"""
    raw = read_json(ds_dir(dataset) / "labels" / "_cache.json", {})
    out = {}
    for k, v in raw.items():
        parts = k.split(":")
        out[":".join(parts[:2] + parts[3:]) if len(parts) == 4 else k] = v
    return out


def label_doc(dataset: str, filing: dict, use_llm: bool, cfg: dict | None, cache: dict, spend: dict | None = None,
              existing: dict | None = None) -> dict:
    """existing：补做模式（--refresh）——保留已有标注，只判断按当前预筛方式 / 术语表新纳入的段落。"""
    d = ds_dir(dataset)
    doc = read_json(d / "docs" / f"{filing['id']}.json")
    lang = filing["language"]
    method = "llm" if use_llm else "lexicon"
    model = cfg["model"] if use_llm else "lexicon"
    gver = glossary()["version"]
    picked = screen(doc, lang)
    kept = {x["i"]: x for x in (existing or {}).get("labels") or []}
    results, usage_total = {}, {"prompt_tokens": 0, "completion_tokens": 0}
    todo = []
    for p in picked:
        if p["i"] in kept:
            continue
        if _ckey(model, p["sha"]) in cache:
            results[p["i"]] = cache[_ckey(model, p["sha"])]
        else:
            todo.append(p)
    if use_llm:
        for k in range(0, len(todo), BATCH):
            if spend is not None and spend["usd"] >= spend["budget"]:      # 达到预算：本篇不保存，下次运行重做
                raise BudgetReached()
            batch = todo[k:k + BATCH]
            got, usage = llm_label(batch, lang, cfg)
            if spend is not None:
                spend["usd"] += float(usage.get("usd") or 0)
            for u in usage_total:
                usage_total[u] += int(usage.get(u) or 0)
            for p in batch:   # 大模型漏掉的段落用词典法补，并标记
                results[p["i"]] = got.get(p["i"]) or {**lexicon_label(p["text"], lang), "fallback": True}
    else:
        for p in todo:
            results[p["i"]] = lexicon_label(p["text"], lang)
    texts = {p["i"]: p for p in doc["paragraphs"]}
    labels = list(kept.values())
    for i in sorted(results):
        r = results[i]
        m = "lexicon" if (method == "lexicon" or r.get("fallback")) else "llm"
        cache[_ckey(model, texts[i]["sha"])] = {k: v for k, v in r.items() if k != "fallback"}
        lab = {"i": i, "sha": texts[i]["sha"], "method": m, **{k: v for k, v in r.items() if k != "fallback"},
               "flags": _flags(r, lang, texts[i]["text"], m)}
        if existing and model != existing.get("model"):
            lab["model"] = model                       # 补做的段落与原文件的模型不同时逐段记录
        labels.append(lab)
    labels.sort(key=lambda x: x["i"])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if existing:
        u0 = existing.get("usage") or {}
        out = {**existing, "glossary_version": gver, "screening": pipeline()["screening"], "refreshed_at": now,
               "paragraphs_screened": len({p["i"] for p in picked} | set(kept)),
               "usage": {k: int(u0.get(k) or 0) + usage_total[k] for k in usage_total}, "labels": labels,
               "refresh_added": sorted(results)}
    else:
        out = {"id": filing["id"], "dataset": dataset, "language": lang, "model": model, "prompt_version": PROMPT_VERSION,
               "glossary_version": gver, "screening": pipeline()["screening"], "labelled_at": now,
               "paragraphs_total": len(doc["paragraphs"]), "paragraphs_screened": len(picked), "usage": usage_total,
               "labels": labels}
    write_json(d / "labels" / f"{filing['id']}.json", out)
    return out


def _pending(dataset: str, max_docs: int | None, redo: bool, refresh: bool = False) -> list[tuple[dict, dict | None]]:
    """待处理的公告及其已有标注：未标注的全部；--redo 全部重做；--refresh 已标注的也纳入，只补新纳入的段落。"""
    d = ds_dir(dataset)
    out = []
    for f in pd.read_parquet(d / "filings.parquet").to_dict("records"):
        if max_docs is not None and len(out) >= max_docs:
            break
        path = d / "labels" / f"{f['id']}.json"
        if redo or not path.exists():
            out.append((f, None))
        elif refresh:
            out.append((f, read_json(path)))
    return out


def _per_paragraph(dataset: str, lang: str | None = None) -> tuple[float, float, str]:
    """以往大模型标注的实际用量：每个送标段落平均输入 / 输出 token。优先本数据集同语言，其次本数据集，再其次其他数据集，
    最后用默认值（不同语言的段落长度与译文量差别大：法国的英文长公告每段输出约为平均的 2–3 倍）。"""
    from factopia.text.common import DATASETS
    for ds, lg in [(dataset, lang), (dataset, None)] + [(x, None) for x in DATASETS if x != dataset]:
        pt = ct = n = 0
        for p in (ds_dir(ds) / "labels").glob("*.json"):
            if p.name.startswith("_"):
                continue
            r = read_json(p, {})
            if lg and r.get("language") != lg:
                continue
            u = r.get("usage") or {}
            k = sum(1 for x in r.get("labels") or [] if x.get("method") == "llm")
            if k and u.get("prompt_tokens"):
                pt, ct, n = pt + u["prompt_tokens"], ct + u.get("completion_tokens", 0), n + k
        if n >= 50:
            return pt / n, ct / n, f"{ds}{' 的 ' + lg + ' 段落' if lg else ''}以往 {n} 段的实际用量"
    return *FALLBACK_TOKENS, "没有历史用量，按默认值"


def estimate(dataset: str, max_docs: int | None = None, redo: bool = False, refresh: bool = False) -> dict:
    """运行前估算：待处理公告数、需送大模型的段落数（按当前预筛方式纳入、尚无标注且不在缓存中）、token 与费用。"""
    d = ds_dir(dataset)
    cache = load_cache(dataset)
    try:
        model = draft.llm_config()["model"]
    except draft.DraftError:
        model = None
    files = _pending(dataset, max_docs, redo, refresh)
    paras = touched = 0
    by_lang: dict[str, int] = {}
    for f, ex in files:
        doc = read_json(d / "docs" / f"{f['id']}.json", {"paragraphs": []})
        have = {x["i"] for x in (ex or {}).get("labels") or []}
        n = sum(1 for p in screen(doc, f["language"]) if p["i"] not in have and _ckey(model, p["sha"]) not in cache)
        paras += n
        by_lang[f["language"]] = by_lang.get(f["language"], 0) + n
        touched += bool(n) or ex is None
    pt = ct = 0
    bases = []
    for lg, n in by_lang.items():                      # 按语言分别估算
        if not n:
            continue
        ppt, pct, b = _per_paragraph(dataset, lg)
        pt, ct = pt + int(n * ppt * EST_MARGIN), ct + int(n * pct * EST_MARGIN)
        bases.append(b)
    basis = "；".join(bases) or "无待处理段落"
    pin, pout = llm_cost.prices()
    return {"dataset": dataset, "model": model, "filings": touched, "paragraphs": paras, "screening": pipeline()["screening"],
            "refresh": refresh, "calls": math.ceil(paras / BATCH) if paras else 0,
            "prompt_tokens": pt, "completion_tokens": ct, "usd": round(llm_cost.usd(pt, ct), 2), "basis": basis,
            "price_per_million": {"in": pin, "out": pout}, "margin": EST_MARGIN}


def describe(e: dict, budget: float) -> str:
    return (f"{e['dataset']}（{'全文' if e.get('screening') == 'full' else '术语表预筛'}{'，补做' if e.get('refresh') else ''}）：待处理 {e['filings']} 篇公告，送大模型 {e['paragraphs']} 段（约 {e['calls']} 次调用，模型 {e['model']}）；"
            f"预计 输入 {e['prompt_tokens']:,} / 输出 {e['completion_tokens']:,} token，约 ${e['usd']:.2f}"
            f"（单价 ${e['price_per_million']['in']} / ${e['price_per_million']['out']} 每百万 token，{e['basis']}，已上浮 {round((e['margin'] - 1) * 100)}%）；"
            f"预算上限 ${budget:g}" + ("，预计会在预算处停止，可分次运行或提高 --budget" if e["usd"] > budget else ""))


def run(dataset: str, lexicon: bool = False, max_docs: int | None = None, redo: bool = False, budget: float | None = None,
        refresh: bool = False) -> dict:
    d = ds_dir(dataset)
    budget = DEFAULT_BUDGET if budget is None else float(budget)
    cfg = None
    if not lexicon:
        try:
            cfg = draft.llm_config()
        except draft.DraftError as e:
            print(f"{e}；改用词典法标注（全部进入复核）")
    cache_path = d / "labels" / "_cache.json"
    cache = load_cache(dataset)
    done = skipped = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    est = estimate(dataset, max_docs, redo, refresh) if cfg else None
    spend = {"usd": 0.0, "budget": budget}
    started, stopped = datetime.now(timezone.utc).isoformat(timespec="seconds"), None
    if est:
        print(describe(est, budget), flush=True)
    for f, ex in _pending(dataset, max_docs, redo, refresh):
        if ex is not None:                             # 补做：没有新纳入的段落就不改文件
            doc = read_json(d / "docs" / f"{f['id']}.json")
            have = {x["i"] for x in ex.get("labels") or []}
            if not any(p["i"] not in have for p in screen(doc, f["language"])):
                continue
        try:
            out = label_doc(dataset, f, cfg is not None, cfg, cache, spend if cfg else None, ex)
        except BudgetReached:
            stopped = f"达到预算上限 ${budget:g}"
            print(f"已花费 ${spend['usd']:.3f}，{stopped}，已停止；已完成的公告已保存，下次运行从未完成的继续", flush=True)
            break
        except draft.DraftError as e:      # 余额不足、密钥错误等：保存已完成部分后停止
            stopped = f"大模型调用失败：{str(e)[:200]}"
            print(f"大模型调用失败，已停止：{e}")
            break
        except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:   # 重试后仍失败：跳过本篇，下次运行补做
            skipped += 1
            print(f"{f['id']} 跳过（重试 {RETRIES} 次仍失败：{type(e).__name__}），下次运行补做", flush=True)
            continue
        for u in usage:
            usage[u] += out["usage"][u]
        done += 1
        write_json(cache_path, cache)
        print(f"{f['id']} {f['language']} 段落 {out['paragraphs_total']} 预筛 {out['paragraphs_screened']} "
              f"标注 {sum(bool(x['concepts']) for x in out['labels'])}", flush=True)
    write_json(cache_path, cache)
    rec = {"started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": cfg["model"] if cfg else "lexicon",
           "estimate": est, "budget_usd": budget, "usd": round(spend["usd"], 4), "usage": usage, "labelled": done, "skipped": skipped,
           "stopped": stopped, "max_docs": max_docs, "redo": redo, "refresh": refresh,
           "screening": pipeline()["screening"], "glossary_version": glossary()["version"]}
    (d / "labels").mkdir(parents=True, exist_ok=True)
    with (d / "labels" / "_runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"labelled": done, "skipped": skipped, "usage": usage, "usd": round(spend["usd"], 4), "budget_usd": budget, "stopped": stopped}
