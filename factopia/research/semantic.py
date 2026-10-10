"""语义检索：内部研究、因子库、数据字典与经批准的外部内容（业务需求 BR-25、35；Spec 第 14.18 节）。

检索范围（每条结果标注类型与来源，可点回原处）：
- 因子库（factopia/research/library.py）：Alpha158 公开基准、获批信号、团队专有因子；
- 内部研究：已锁定的研究规格（标题、假设、经济逻辑、可证伪测试）与 AI 解释的市场差异；
- 数据字典：configs/refdata/fields.yaml；
- 经批准的外部内容：数据来源登记表中可用的真实公告（法国 AMF，Licence Ouverte 2.0）的标题；模拟数据不在检索范围内。

做法（不依赖向量模型）：① 大模型把提问改写为中 / 英 / 法三种语言的关键词与同义表述（不可用时退回术语表概念词）；
② BM25 关键词检索（中文按双字切分，西文去重音、小写）；③ 返回得分、命中词与改写结果，便于研究员判断。
"""
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

import pandas as pd
import yaml

from factopia.research import library, store

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
KIND_ZH = {"factor": "因子库", "research": "内部研究", "field": "数据字典", "filing": "外部公告（已批准来源）"}
K1, B = 1.5, 0.75
CJK = re.compile(r"[㐀-鿿]+")
WORD = re.compile(r"[a-z0-9_]+")
STOP = {"the", "of", "and", "a", "an", "in", "to", "for", "on", "with", "by", "is", "are", "de", "la", "le", "les", "des", "et",
        "du", "en", "un", "une", "that", "this", "as", "at", "or", "be", "from", "its", "mise", "sur", "au", "aux", "par", "pour", "dans", "est"}


def tokens(text: str) -> list[str]:
    from factopia.text.common import fold
    t = fold(text or "")
    out = [w for w in WORD.findall(t) if w not in STOP and len(w) > 1]
    for seg in CJK.findall(t):
        out += [seg[i:i + 2] for i in range(len(seg) - 1)] or [seg]
    return out


def corpus() -> list[dict]:
    docs = []
    for f in library.items():
        docs.append({"kind": "factor", "id": f["id"], "title": f"{f['name']}（{library.SOURCE_ZH[f['source']]}）",
                     "text": " ".join(str(x) for x in (f["name"], f.get("group"), f.get("description"), f.get("formulation")) if x),
                     "ref": f["id"], "meta": {"source": f["source"], "status": f["status"], "label": f.get("label")}})
    from factopia import access
    for rid in store.research_ids():
        for v in store.versions(rid):
            s = store.load_version(rid, v)
            if not any(access.can(m) for m in s.get("markets") or []):      # 地区视角
                continue
            docs.append({"kind": "research", "id": f"{rid}.v{v}", "title": f"{s.get('title_zh') or rid} v{v}",
                         "text": " ".join(str(x) for x in (s.get("title_zh"), s.get("hypothesis_original"), s.get("hypothesis"),
                                                           s.get("hypothesis_zh"), (s.get("rationale") or {}).get("note"),
                                                           " ".join(s.get("directions") or [])) if x),
                         "ref": f"{rid}/{v}", "meta": {"markets": s.get("markets"), "signal_source": s.get("signal_source", "price_volume"),
                                                       "locked_at": s.get("locked_at")}})
            exp = store.RESEARCH_DIR / rid / f"explain_v{v}.json"
            if exp.exists() and access.cross_region():                    # AI 解释涉及三个地区，只对总部检索
                e = json.loads(exp.read_text(encoding="utf-8"))
                sents = [x.get("text") or x.get("sentence") or "" for x in (e.get("sentences") or e.get("kept") or [])]
                if sents:
                    docs.append({"kind": "research", "id": f"{rid}.v{v}.explain", "title": f"{s.get('title_zh') or rid} v{v} · AI 解释市场差异",
                                 "text": " ".join(sents), "ref": f"{rid}/{v}", "meta": {"model": e.get("model")}})
    fp = CONFIGS / "refdata" / "fields.yaml"
    if fp.exists():
        fd = yaml.safe_load(fp.read_text(encoding="utf-8"))
        for f in fd["fields"]:
            docs.append({"kind": "field", "id": f"field:{f['name']}", "title": f"{f['name']}（{f.get('zh', '')}）",
                         "text": " ".join(str(v) for v in f.values()), "ref": f"fields.yaml {fd.get('version', '')}",
                         "meta": {k: f.get(k) for k in ("unit", "source", "available")}})
    from factopia.text.common import DATASETS, ds_dir
    for ds, cfg in DATASETS.items():
        p = ds_dir(ds) / "aligned.parquet"
        if cfg["simulated"] or not p.exists() or not access.can(cfg["market"]):
            continue
        meta = pd.read_parquet(p, columns=["id", "company", "title", "language", "published_at", "available_at"])
        for r in meta.to_dict("records"):
            docs.append({"kind": "filing", "id": f"{ds}:{r['id']}", "title": f"{r['company']} · {r['title']}",
                         "text": f"{r['company']} {r['title']}", "ref": r["id"],
                         "meta": {"dataset": ds, "source": cfg["source"], "licence": cfg["licence"], "language": r["language"],
                                  "published_at": str(r["published_at"]), "available_at": str(r["available_at"])}})
    return docs


def expand(query: str, use_llm: bool = True) -> dict:
    """大模型改写为三种语言的关键词；不用大模型或调用失败时用术语表概念词。"""
    from factopia.research import draft
    prompt = ("You help search an internal quant research knowledge base (factor library, research specs, data dictionary, "
              "company filings) written in Chinese, English and French. Rewrite the user's query into search keywords. "
              'Return JSON {"en": [...], "zh": [...], "fr": [...], "intent": "<one short English sentence>"}, '
              "3-8 short keywords or synonyms per language, including technical factor terms (e.g. momentum, volatility, "
              "turnover, risk disclosure).\n\nQuery: " + query)
    try:
        if not use_llm:
            raise RuntimeError("未使用大模型")
        text, usage = draft._chat(prompt, draft.llm_config())
        d = json.loads(text)
        return {"method": "llm", "terms": {k: [str(x) for x in (d.get(k) or [])][:8] for k in ("en", "zh", "fr")},
                "intent": str(d.get("intent") or ""), "tokens": int(usage.get("total_tokens") or 0)}
    except Exception as e:                                   # 未配置大模型或调用失败：退回术语表
        from factopia.text.common import glossary, query_concepts
        g = glossary()
        terms = {"en": [], "zh": [], "fr": []}
        for c in query_concepts(query):
            for lang in terms:
                terms[lang] += [t.rstrip("*") for t in ((g["concepts"].get(c) or {}).get("terms") or {}).get(lang) or []][:5]
        return {"method": "glossary", "terms": terms, "intent": "", "error": str(e)[:200]}


def search(query: str, kinds: list[str] | None = None, limit: int = 40, use_llm: bool = True) -> dict:
    query = (query or "").strip()
    docs = [d for d in corpus() if not kinds or d["kind"] in kinds]
    exp = expand(query, use_llm) if query else {"method": "none", "terms": {}, "intent": ""}
    q_terms = Counter(tokens(query))
    for words in exp["terms"].values():
        for w in words:
            for t in tokens(w):
                q_terms[t] += 0.5                           # 改写词权重减半，原词优先
    toks = [tokens(d["title"] + " " + d["text"]) for d in docs]
    n = len(docs) or 1
    avg = sum(len(t) for t in toks) / n
    df = Counter(t for ts in toks for t in set(ts))
    hits = []
    for d, ts in zip(docs, toks):
        tf = Counter(ts)
        score, matched = 0.0, []
        for t, w in q_terms.items():
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += w * idf * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * len(ts) / avg))
            matched.append(t)
        if score > 0:
            hits.append({**{k: d[k] for k in ("kind", "id", "title", "ref", "meta")}, "kind_zh": KIND_ZH[d["kind"]],
                         "score": round(score, 3), "matched": matched[:12], "snippet": d["text"][:240]})
    hits.sort(key=lambda h: -h["score"])
    per_kind = max(1, limit // 4)                         # 每类各取前若干条：长文本（研究规格）的得分偏高，避免挤掉因子与字段
    kept, seen = [], Counter()
    for h in hits:
        if seen[h["kind"]] < per_kind:
            kept.append(h)
            seen[h["kind"]] += 1
    return {"query": query, "expansion": exp, "corpus": dict(Counter(d["kind"] for d in docs)), "results": kept,
            "method": "大模型多语言改写 + BM25 关键词检索（中文双字切分）" if exp["method"] == "llm" else "术语表改写 + BM25 关键词检索"}
