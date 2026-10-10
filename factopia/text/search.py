"""跨语言检索（BR-10、BR-12）：任意语言提问 → 统一概念标签 → 原文与译文中的段落，每条结果带引用。

匹配方式：① 提问映射到概念，返回标注了该概念的段落（使用复核后的标注）；② 关键词直接出现在原文或译文中。
结果给出来源、段落编号、公告标题、业务日期、正式发布时间、实际可获取时间、生效交易日，以及是否为模拟数据。
"""
import json
import re

import pandas as pd

from factopia.text import review
from factopia.text.common import DATASETS, LANG_ZH, concept_names, ds_dir, fold, query_concepts, read_json


def search(query: str, datasets: list[str] | None = None, concepts: list[str] | None = None,
           limit: int = 50) -> dict:
    query = (query or "").strip()
    wanted = query_concepts(query) if query else {}
    if concepts:
        wanted.update({c: [] for c in concepts if c in concept_names()})
    q = fold(query)
    names = concept_names()
    dec = review.decisions()
    per_ds: list[list[dict]] = []
    for ds in datasets or list(DATASETS):
        hits: list[dict] = []
        per_ds.append(hits)
        d = ds_dir(ds)
        if not (d / "aligned.parquet").exists():      # 由 signal 生成（含三个时间与生效交易日）
            continue
        meta = pd.read_parquet(d / "aligned.parquet")
        for f in meta.sort_values("published_at", ascending=False).to_dict("records"):
            lab = read_json(d / "labels" / f"{f['id']}.json")
            if not lab:
                continue
            doc = None
            for x in lab["labels"]:
                eff = review.apply(x, dec.get(review.key(ds, f["id"], x["i"])))
                matched = [c for c in eff["concepts"] if c["concept"] in wanted]
                doc = doc or read_json(d / "docs" / f"{f['id']}.json")
                text = doc["paragraphs"][x["i"]]["text"]
                tr = eff.get("translation") or {}
                kw = bool(q) and len(q) >= 2 and any(q in fold(t) for t in [text, *tr.values()])
                if not matched and not kw:
                    continue
                hits.append({
                    "dataset": ds, "simulated": bool(DATASETS[ds]["simulated"]), "source": f["source"],
                    "licence": f["licence"], "doc_id": f["id"], "paragraph": x["i"], "company": f["company"],
                    "instrument": f["instrument"], "title": f["title"], "url": f.get("url"),
                    "language": lab["language"], "language_zh": LANG_ZH.get(lab["language"], lab["language"]),
                    "business_date": f["business_date"], "published_at": f["published_at"],
                    "available_at": f["available_at"],
                    "effective_date": None if pd.isna(f["effective_date"]) else f["effective_date"].strftime("%Y-%m-%d"),
                    "original": text, "translation": tr, "translation_confidence": x.get("translation_confidence"),
                    "concepts": [{**c, "name": names[c["concept"]]} for c in eff["concepts"]],
                    "matched": [names[c["concept"]] for c in matched] + (["关键词"] if kw else []),
                    "review_status": eff["review_status"], "model": lab["model"],
                    "glossary_version": lab["glossary_version"],
                })
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
    # 各数据集的结果交替排列，避免一个市场占满结果
    hits = [h for group in zip(*[g + [None] * (limit - len(g)) for g in per_ds]) for h in group if h][:limit]
    return {"query": query, "concepts": [{"concept": c, "name": names[c], "terms": t} for c, t in wanted.items()],
            "results": hits, "truncated": len(hits) >= limit}


SUMMARY_PROMPT = """You are a research assistant on a cross-border equity research platform. Using ONLY the numbered excerpts
below (original language plus translations), answer the researcher's question in Simplified Chinese.
Rules: every sentence must end with one or more citations like [1] or [2][5] that point to the excerpts supporting it;
do not state anything the excerpts do not support; say which market/company each point is about; if excerpts are marked
SIMULATED, say the point comes from simulated demo data. Do not give investment advice.
Return JSON: {{"sentences": [{{"text": "...", "cites": [1, 2]}}]}}
Question: {query}
Excerpts:
{excerpts}"""


def summarize(query: str, hits: list[dict], max_items: int = 12) -> dict:
    """AI 摘要（BR-10）：每句结论必须引用检索结果编号；引用不存在或缺失的句子被丢弃，保证结论可回到原文段落。"""
    from factopia.research import draft

    items = hits[:max_items]
    if not items:
        return {"sentences": [], "dropped": 0, "citations": [], "model": None}
    lines = []
    for n, h in enumerate(items, 1):
        tr = h.get("translation") or {}
        sim = " SIMULATED" if h["simulated"] else ""
        lines.append(f"[{n}]{sim} {h['company']} ({h['instrument']}), {h['title']}, published {h['published_at']}, "
                     f"language {h['language']}: {h['original']}" + (f" | zh: {tr['zh']}" if tr.get("zh") else ""))
    cfg = draft.llm_config()
    text, usage = draft._chat(SUMMARY_PROMPT.format(query=query, excerpts="\n".join(lines)), cfg)
    raw = json.loads(text).get("sentences") or []
    kept, dropped = [], 0
    for s in raw:
        try:
            cites = sorted({int(c) for c in s.get("cites") or [] if 1 <= int(c) <= len(items)})
        except (TypeError, ValueError):
            cites = []
        body = re.sub(r"(\[\d+\])+\s*$", "", str(s.get("text") or "")).strip()
        if not cites or not body:
            dropped += 1
            continue
        kept.append({"text": body, "cites": cites})
    cited = sorted({c for s in kept for c in s["cites"]})
    return {"sentences": kept, "dropped": dropped, "model": cfg["model"], "usage": usage,
            "citations": [{"n": n, **{k: items[n - 1][k] for k in ("dataset", "doc_id", "paragraph", "company", "title",
                                                                    "published_at", "available_at", "simulated", "url")}}
                          for n in cited]}
