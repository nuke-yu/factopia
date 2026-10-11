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


def snapshot(datasets: list[str]) -> tuple[dict, dict[str, bytes]]:
    """审批提交时冻结翻译（BR-23"锁定获批的翻译"；第 14.22 节）。

    每个数据集导出复核后生效的全部段落标注（原文段落指纹、译文、概念、置信度、复核状态与复核人），
    以及该数据集的全部复核记录和术语表全文；返回（摘要，{文件名: gzip 字节}）。摘要含各文件 sha256、
    翻译模型 / 提示词 / 术语表版本，计入提交指纹，获批后锁定。
    """
    import gzip
    import hashlib
    from factopia.text.common import CONFIGS
    dec = decisions()
    summary, files = {}, {}
    gl = CONFIGS / "text" / "glossary.yaml"
    if gl.exists():
        files["glossary.yaml.gz"] = gzip.compress(gl.read_bytes(), mtime=0)
    for ds in datasets:
        d = ds_dir(ds)
        rows, versions = [], {"model": set(), "prompt_version": set(), "glossary_version": set()}
        for p in sorted((d / "labels").glob("*.json")) if (d / "labels").exists() else []:
            lab = read_json(p) or {}
            for v in versions:
                if lab.get(v):
                    versions[v].add(lab[v])
            doc_id = lab.get("id") or p.stem
            for x in lab.get("labels") or []:
                eff = apply(x, dec.get(key(ds, doc_id, x["i"])))
                rows.append({"doc_id": doc_id, "i": x["i"], "sha": x.get("sha"), "language": lab.get("language"),
                             "translation": eff.get("translation"), "translation_confidence": x.get("translation_confidence"),
                             "concepts": eff.get("concepts"), "flags": x.get("flags") or [], "review_status": eff["review_status"],
                             "reviewer": eff.get("reviewer"), "reviewed_at": eff.get("reviewed_at")})
        reviews = [r for r in dec.values() if r["dataset"] == ds]
        body = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows).encode("utf-8")
        rev = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in reviews).encode("utf-8")
        files[f"{ds}.labels.jsonl.gz"] = gzip.compress(body, mtime=0)
        files[f"{ds}.reviews.jsonl.gz"] = gzip.compress(rev, mtime=0)
        summary[ds] = {"paragraphs": len(rows), "documents": len({r["doc_id"] for r in rows}),
                       "reviewed": sum(1 for r in rows if r["review_status"] not in ("pending", "not_required")),
                       "pending": sum(1 for r in rows if r["review_status"] == "pending"),
                       "simulated": bool(DATASETS[ds]["simulated"]),
                       **{k: sorted(v) for k, v in versions.items()},
                       "labels_sha256": hashlib.sha256(body).hexdigest(), "reviews_sha256": hashlib.sha256(rev).hexdigest()}
    if gl.exists():
        summary["glossary_sha256"] = hashlib.sha256(gl.read_bytes()).hexdigest()
    return summary, files
