"""术语表扩充（Spec 第 14.9 节 v1.30）：AI 从术语表未命中、没送大模型的段落里找漏掉的风险说法，人工批准后生效。

1. 抽样：固定种子随机抽取未命中段落（configs/text/pipeline.yaml glossary_suggest.sample，默认 300 段）。
2. AI 判断：每批 10 段，判断是否表达四类风险概念之一；若是，从原文原样摘出最能代表该说法的短语（2–6 个词）。
3. 汇总候选：按（概念、语言、短语）合并，记录出现次数与原文例句；已在术语表中的不列。
4. 人工审批（情报台 → 术语表）：批准的写入 runtime/text/glossary_additions.json，术语表版本变为 <基础>+a<条数>；
   拒绝的保留记录。批准后运行 `understand --refresh` 只判断新纳入的段落（已判断的段落按缓存不再付费）。
费用：运行前估算，命令行不加 --yes 只显示估算；记录在 runtime/llm_costs.jsonl（用途"术语表扩充"）。
"""
import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timezone

import pandas as pd

from factopia import llm_cost
from factopia.research import draft
from factopia.text import common as C
from factopia.text.common import ds_dir, glossary, pipeline, read_json

PROMPT_VERSION = "glossary-suggest-v1"
BATCH = 10
CANDIDATES = C.REVIEW / "glossary_candidates.json"
PROMPT = """You help maintain a multilingual glossary used to pre-screen paragraphs from listed-company filings.
These paragraphs did NOT match the current glossary. Find paragraphs that nevertheless state one of these risk concepts
about the company or its markets (not boilerplate disclaimers):
{concepts}
For each such paragraph, copy VERBATIM from the original the shortest phrase (2-6 words, same language as the paragraph)
that signals the concept, so it can be added to the glossary. Skip paragraphs that state no concept.
Return JSON: {{"items": [{{"i": <paragraph index>, "concept": "<id>", "direction": 1 or -1, "phrase": "..."}}]}}
Paragraphs (language: {lang}):
{paragraphs}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())


def _sample(dataset: str, n: int) -> list[dict]:
    d = ds_dir(dataset)
    seed = int(pipeline()["review"]["sample_seed"]) + 1
    rank = lambda k: int(hashlib.sha256(f"{seed}:{k}".encode()).hexdigest()[:12], 16)   # noqa: E731
    out = []
    meta = pd.read_parquet(d / "filings.parquet")
    for f in sorted(meta.to_dict("records"), key=lambda f: rank(f["id"])):
        lab = read_json(d / "labels" / f"{f['id']}.json")
        if not lab:
            continue
        have = {x["i"] for x in lab["labels"]}
        doc = read_json(d / "docs" / f"{f['id']}.json") or {"paragraphs": []}
        cand = [p for p in doc["paragraphs"] if p["i"] not in have and len(p["text"].strip()) >= 40 and not C.concept_hits(p["text"], f["language"])]
        cand.sort(key=lambda p: rank(f"{f['id']}:{p['i']}"))
        for p in cand[:2]:                                  # 每篇最多 2 段，覆盖更多公告
            out.append({"doc_id": f["id"], "i": p["i"], "lang": f["language"], "text": p["text"][:1200]})
        if len(out) >= n:
            break
    return out[:n]


def estimate(dataset: str, n: int | None = None) -> dict:
    n = n or int(pipeline()["glossary_suggest"]["sample"])
    paras = _sample(dataset, n)
    chars = sum(len(p["text"]) for p in paras)
    calls = math.ceil(len(paras) / BATCH)
    pt = int(chars / 3.2 + calls * 400)                     # 约 3.2 字符 / token，另加每次调用的说明文字
    ct = int(len(paras) * 30)
    return {"dataset": dataset, "paragraphs": len(paras), "calls": calls, "prompt_tokens": pt, "completion_tokens": ct,
            "usd": round(llm_cost.usd(pt, ct) * 1.2, 3)}


def run(dataset: str, n: int | None = None, budget: float = 0.5) -> dict:
    cfg = draft.llm_config()
    n = n or int(pipeline()["glossary_suggest"]["sample"])
    g = glossary()
    concepts = "\n".join(f"- {c}: {s['en_name']} / {s['fr_name']} / {s['zh_name']}" for c, s in g["concepts"].items())
    paras = _sample(dataset, n)
    found, spent, usage_all = {}, 0.0, {"prompt_tokens": 0, "completion_tokens": 0}
    stopped = None
    by_lang: dict[str, list[dict]] = {}
    for p in paras:
        by_lang.setdefault(p["lang"], []).append(p)
    for lang, ps in by_lang.items():
        for k in range(0, len(ps), BATCH):
            if spent >= budget:
                stopped = f"达到预算上限 ${budget:g}"
                break
            batch = ps[k:k + BATCH]
            text = "\n".join(f"[{j}] {p['text']}" for j, p in enumerate(batch))
            try:
                raw, usage = draft._chat(PROMPT.format(concepts=concepts, lang=lang, paragraphs=text), cfg, "术语表扩充")
                items = json.loads(raw).get("items") or []
            except (draft.DraftError, json.JSONDecodeError) as e:
                stopped = f"大模型调用失败：{str(e)[:200]}"
                break
            spent += float(usage.get("usd") or 0)
            for u in usage_all:
                usage_all[u] += int(usage.get(u) or 0)
            for it in items:
                try:
                    p = batch[int(it["i"])]
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
                phrase, concept = str(it.get("phrase") or "").strip(), it.get("concept")
                if concept not in g["concepts"] or not (3 <= len(phrase) <= 80) or _norm(phrase) not in _norm(p["text"]):
                    continue                                 # 只收原文中确实存在的短语
                if C.concept_hits(phrase, lang):
                    continue                                 # 已被现有术语覆盖
                kk = (concept, lang, _norm(phrase))
                c = found.setdefault(kk, {"concept": concept, "lang": lang, "term": phrase, "count": 0, "examples": []})
                c["count"] += 1
                if len(c["examples"]) < 3:
                    c["examples"].append({"doc_id": p["doc_id"], "i": p["i"], "text": p["text"][:400], "direction": 1 if int(it.get("direction", 1)) >= 0 else -1})
        if stopped:
            break
    old = read_json(CANDIDATES, {"runs": [], "candidates": []})
    known = {(c["dataset"], c["concept"], c["lang"], _norm(c["term"])): c for c in old["candidates"]}
    added = 0
    for (concept, lang, nt), c in found.items():
        k = (dataset, concept, lang, nt)
        if k in known:
            known[k]["count"] = max(known[k]["count"], c["count"])
            continue
        known[k] = {"id": f"gc_{secrets.token_hex(4)}", "dataset": dataset, **c, "status": "pending", "created_at": _now()}
        added += 1
    run_rec = {"at": _now(), "dataset": dataset, "model": cfg["model"], "prompt_version": PROMPT_VERSION, "glossary_version": g["version"],
               "sampled": len(paras), "with_risk": sum(c["count"] for c in found.values()), "candidates": len(found), "added": added,
               "usd": round(spent, 4), "usage": usage_all, "budget_usd": budget, "stopped": stopped}
    C.write_shared(CANDIDATES, {"runs": old["runs"] + [run_rec], "candidates": sorted(known.values(), key=lambda c: -c["count"])})
    return run_rec


def candidates() -> dict:
    data = read_json(CANDIDATES, {"runs": [], "candidates": []})
    names = C.concept_names()
    return {**data, "candidates": [{**c, "concept_zh": names.get(c["concept"], c["concept"])} for c in data["candidates"]],
            "additions": glossary().get("additions", []), "glossary_version": glossary()["version"]}


def decide(cid: str, action: str, by: str, note: str = "", term: str | None = None) -> dict:
    """批准（可修改短语后批准）或拒绝一个候选术语；批准即写入新增术语表。"""
    by = (by or "").strip()[:60]
    if not by:
        raise ValueError("请填写审批人")
    if action not in ("approve", "reject"):
        raise ValueError("操作只能是 approve 或 reject")
    data = read_json(CANDIDATES, {"runs": [], "candidates": []})
    c = next((x for x in data["candidates"] if x["id"] == cid), None)
    if not c:
        raise KeyError("候选术语不存在")
    if c["status"] != "pending":
        raise ValueError(f"该候选已处理：{c['status']}")
    final = (term or c["term"]).strip()
    if action == "approve":
        if not (3 <= len(final) <= 80):
            raise ValueError("术语长度应为 3–80 个字符")
        adds = read_json(C.ADDITIONS, [])
        adds.append({"concept": c["concept"], "lang": c["lang"], "term": final, "by": by, "at": _now(), "candidate_id": cid,
                     "note": note[:300], "edited": final != c["term"]})
        C.write_shared(C.ADDITIONS, adds)
        C.glossary.cache_clear()
    c.update({"status": "approved" if action == "approve" else "rejected", "decided_by": by, "decided_at": _now(),
              "note": note[:300], "final_term": final if action == "approve" else None})
    C.write_shared(CANDIDATES, data)
    return c
