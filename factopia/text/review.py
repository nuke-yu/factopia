"""本地语言专家复核（BR-11）。

- 队列：理解结果中带复核标记的段落（低置信度、翻译置信度低、否定句、监管措辞、关键财务术语、仅词典法）。
- 复核操作：确认（confirm）/ 修正概念或译文（correct）/ 判定不涉及风险（reject）；只追加写入 review/decisions.jsonl，
  不修改原有记录，保留完整留痕（谁、何时、改了什么、理由）。
- 打分使用复核后的结果：同一段落以最新一次复核为准；未复核的段落仍按模型结果计入，并在页面上标明"待复核"。
"""
import json
import os
from datetime import datetime, timezone

import pandas as pd

from factopia.text.common import DATASETS, LANG_ZH, REVIEW, concept_names, ds_dir, read_json
from factopia.text.understand import FLAG_ZH

ACTIONS = {"confirm": "确认", "correct": "修正", "reject": "判定不涉及风险"}


def ensure_dir() -> None:
    REVIEW.mkdir(parents=True, exist_ok=True)
    try:   # api 服务以非 root 用户运行，也要能写入复核记录
        os.chmod(REVIEW, 0o777)
    except PermissionError:
        pass


def key(dataset: str, doc_id: str, i: int) -> str:
    return f"{dataset}:{doc_id}:{i}"


def decisions() -> dict[str, dict]:
    path = REVIEW / "decisions.jsonl"
    latest: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                latest[rec["key"]] = rec
    return latest


def history(k: str) -> list[dict]:
    path = REVIEW / "decisions.jsonl"
    if not path.exists():
        return []
    return [r for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            and (r := json.loads(line))["key"] == k]


def apply(label: dict, decision: dict | None) -> dict:
    """返回复核后生效的段落标注。"""
    if not decision:
        return {**label, "review_status": "pending" if label.get("flags") else "not_required"}
    out = {**label, "review_status": decision["action"], "reviewer": decision["reviewer"],
           "reviewed_at": decision["at"]}
    if decision["action"] == "reject":
        out["concepts"] = []
    elif decision["action"] == "correct":
        if decision.get("concepts") is not None:
            out["concepts"] = decision["concepts"]
        if decision.get("translation"):
            out["translation"] = {**(label.get("translation") or {}), **decision["translation"]}
    return out


def decide(dataset: str, doc_id: str, i: int, action: str, reviewer: str, note: str = "",
           concepts: list[dict] | None = None, translation: dict | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"未知操作：{action}")
    if not reviewer.strip():
        raise ValueError("请填写复核人")
    labels = read_json(ds_dir(dataset) / "labels" / f"{doc_id}.json")
    if not labels or not any(x["i"] == i for x in labels["labels"]):
        raise KeyError("段落不存在")
    known = set(concept_names())
    clean = None
    if action == "correct":
        if concepts is None and not translation:
            raise ValueError("修正时请给出概念或译文")
        if concepts is not None:
            clean = []
            for c in concepts:
                if c.get("concept") not in known:
                    raise ValueError(f"未知概念：{c.get('concept')}")
                clean.append({"concept": c["concept"], "direction": 1 if int(c.get("direction", 1)) >= 0 else -1,
                              "negated": bool(c.get("negated")), "confidence": 1.0, "evidence": str(c.get("evidence") or "")})
    rec = {"key": key(dataset, doc_id, i), "dataset": dataset, "doc_id": doc_id, "i": i, "action": action,
           "reviewer": reviewer.strip()[:60], "note": note[:500], "concepts": clean,
           "translation": {k: str(v) for k, v in (translation or {}).items() if k in ("zh", "en") and v} or None,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ensure_dir()
    with open(REVIEW / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def queue(dataset: str | None = None, status: str = "pending", limit: int = 100) -> dict:
    dec = decisions()
    names = concept_names()
    items, counts = [], {"pending": 0, "done": 0}
    for ds in ([dataset] if dataset else list(DATASETS)):
        d = ds_dir(ds)
        if not (d / "filings.parquet").exists():
            continue
        meta = pd.read_parquet(d / "filings.parquet").set_index("id")
        for f_id in reversed(meta.index.tolist()):
            lab = read_json(d / "labels" / f"{f_id}.json")
            if not lab:
                continue
            doc = None
            for x in lab["labels"]:
                if not x.get("flags"):
                    continue
                k = key(ds, f_id, x["i"])
                done = k in dec
                counts["done" if done else "pending"] += 1
                if (status == "pending" and done) or (status == "done" and not done) or len(items) >= limit:
                    continue
                doc = doc or read_json(d / "docs" / f"{f_id}.json")
                m = meta.loc[f_id]
                eff = apply(x, dec.get(k))
                items.append({
                    "key": k, "dataset": ds, "doc_id": f_id, "i": x["i"], "simulated": bool(DATASETS[ds]["simulated"]),
                    "company": m["company"], "instrument": m["instrument"], "title": m["title"],
                    "published_at": m["published_at"], "language": lab["language"],
                    "language_zh": LANG_ZH.get(lab["language"], lab["language"]),
                    "original": doc["paragraphs"][x["i"]]["text"], "translation": eff.get("translation") or {},
                    "translation_confidence": x.get("translation_confidence"), "model": lab["model"],
                    "glossary_version": lab["glossary_version"], "method": x["method"],
                    "concepts": [{**c, "name": names.get(c["concept"], c["concept"])} for c in eff["concepts"]],
                    "model_concepts": [{**c, "name": names.get(c["concept"], c["concept"])} for c in x["concepts"]],
                    "flags": [{"code": f, "label": FLAG_ZH.get(f, f)} for f in x["flags"]],
                    "decision": dec.get(k), "history": history(k) if done else [],
                })
    return {"items": items, "counts": counts, "actions": ACTIONS}
