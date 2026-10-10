"""② 理解：段落 → 统一概念标签、翻译、置信度、复核标记（BR-09、BR-11、BR-12）。

- 预筛：只把命中术语表（该语言）的段落交给大模型，控制调用成本；未命中的段落视为不涉及四类风险。
- 大模型（.env 中与研究规格起草相同的配置）：每批最多 BATCH 段，输出 JSON：各概念的方向（+1 风险加重 / -1 缓解或否定）、
  是否否定句、置信度、原文证据，以及中文 / 英文译文与翻译置信度。
- 未配置大模型或加 --lexicon：用术语表做词典法标注（置信度 0.5，无译文），全部进入复核。
- 原文优先：标注结果单独保存在 labels/<公告id>.json，记录模型、提示词版本、术语表版本；原文（docs/）不改动。
- 复核（BR-11）：低置信度、翻译置信度低、否定句、监管措辞、关键财务术语、仅词典法 → 进入语言专家复核队列（review 模块）。
- 缓存：同一段落（按内容哈希）、同一模型与提示词版本不重复调用。
"""
import json
from datetime import datetime, timezone

import pandas as pd

from factopia.research import draft
from factopia.text.common import concept_hits, ds_dir, flag_hits, glossary, read_json, write_json

PROMPT_VERSION = "text-label-v1"
BATCH = 10
LOW_CONFIDENCE = 0.6
LOW_TRANSLATION = 0.7
LEXICON_CONFIDENCE = 0.5
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
    text, usage = draft._chat(prompt, cfg)
    items = json.loads(text).get("items") or []
    known = set(g["concepts"])
    out = {}
    for raw in items:
        try:
            out[int(raw["i"])] = _clean_llm(raw, known)
        except (KeyError, TypeError, ValueError):
            continue
    return out, usage


def label_doc(dataset: str, filing: dict, use_llm: bool, cfg: dict | None, cache: dict) -> dict:
    d = ds_dir(dataset)
    doc = read_json(d / "docs" / f"{filing['id']}.json")
    lang = filing["language"]
    method = "llm" if use_llm else "lexicon"
    model = cfg["model"] if use_llm else "lexicon"
    gver = glossary()["version"]
    picked = [p for p in doc["paragraphs"] if concept_hits(p["text"], lang)]
    results, usage_total = {}, {"prompt_tokens": 0, "completion_tokens": 0}
    todo = []
    for p in picked:
        key = f"{model}:{PROMPT_VERSION}:{gver}:{p['sha']}"
        if key in cache:
            results[p["i"]] = cache[key]
        else:
            todo.append(p)
    if use_llm:
        for k in range(0, len(todo), BATCH):
            batch = todo[k:k + BATCH]
            got, usage = llm_label(batch, lang, cfg)
            for u in usage_total:
                usage_total[u] += int(usage.get(u) or 0)
            for p in batch:   # 大模型漏掉的段落用词典法补，并标记
                results[p["i"]] = got.get(p["i"]) or {**lexicon_label(p["text"], lang), "fallback": True}
    else:
        for p in todo:
            results[p["i"]] = lexicon_label(p["text"], lang)
    texts = {p["i"]: p for p in doc["paragraphs"]}
    labels = []
    for i in sorted(results):
        r = results[i]
        m = "lexicon" if (method == "lexicon" or r.get("fallback")) else "llm"
        cache[f"{model}:{PROMPT_VERSION}:{gver}:{texts[i]['sha']}"] = {k: v for k, v in r.items() if k != "fallback"}
        labels.append({"i": i, "sha": texts[i]["sha"], "method": m, **{k: v for k, v in r.items() if k != "fallback"},
                       "flags": _flags(r, lang, texts[i]["text"], m)})
    out = {"id": filing["id"], "dataset": dataset, "language": lang, "model": model, "prompt_version": PROMPT_VERSION,
           "glossary_version": gver, "labelled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "paragraphs_total": len(doc["paragraphs"]), "paragraphs_screened": len(picked), "usage": usage_total,
           "labels": labels}
    write_json(d / "labels" / f"{filing['id']}.json", out)
    return out


def run(dataset: str, lexicon: bool = False, max_docs: int | None = None, redo: bool = False) -> dict:
    d = ds_dir(dataset)
    meta = pd.read_parquet(d / "filings.parquet")
    cfg = None
    if not lexicon:
        try:
            cfg = draft.llm_config()
        except draft.DraftError as e:
            print(f"{e}；改用词典法标注（全部进入复核）")
    cache_path = d / "labels" / "_cache.json"
    cache = read_json(cache_path, {})
    done = n = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    for f in meta.to_dict("records"):
        if max_docs is not None and n >= max_docs:
            break
        if not redo and (d / "labels" / f"{f['id']}.json").exists():
            continue
        n += 1
        try:
            out = label_doc(dataset, f, cfg is not None, cfg, cache)
        except draft.DraftError as e:      # 余额不足、密钥错误等：保存已完成部分后停止
            print(f"大模型调用失败，已停止：{e}")
            break
        for u in usage:
            usage[u] += out["usage"][u]
        done += 1
        write_json(cache_path, cache)
        print(f"{f['id']} {f['language']} 段落 {out['paragraphs_total']} 预筛 {out['paragraphs_screened']} "
              f"标注 {sum(bool(x['concepts']) for x in out['labels'])}", flush=True)
    return {"labelled": done, "usage": usage}
