"""本地语言专家复核（BR-11）。

- 队列：理解结果中带复核标记的段落（低置信度、翻译置信度低、否定句、监管措辞、关键财务术语、仅词典法）。
- 复核操作：确认（confirm）/ 修正概念或译文（correct）/ 判定不涉及风险（reject）；只追加写入 review/decisions.jsonl，
  不修改原有记录，保留完整留痕（谁、何时、改了什么、理由）。
- 打分使用复核后的结果：同一段落以最新一次复核为准；未复核的段落仍按模型结果计入，并在页面上标明"待复核"。
- 分组（Spec 第 14.9 节 v1.30，configs/text/pipeline.yaml review）：
  必须（低置信度翻译，业务文档要求人工复核；未复核阻止提交审批）/ 抽样（固定种子随机抽取大模型判断过的段落，
  不论是否触发复核，用于估计准确率；另抽术语表未命中的段落估计漏检率）/ 高影响（判断一旦相反就会翻转该篇
  "风险变化"方向）/ 其他（触发复核但不在以上三组，有空再看，不影响使用）。
"""
import hashlib
import math
import json
import os
from datetime import datetime, timezone

import pandas as pd

from factopia.text.common import DATASETS, LANG_ZH, REVIEW, concept_names, ds_dir, glossary, is_table, pipeline, read_json, series_exclusion, write_shared
from factopia.text.understand import FLAG_ZH

ACTIONS = {"confirm": "确认", "correct": "修正", "reject": "判定不涉及风险"}
TIERS_VERSION = "tiers-v2"          # 分组算法变化时递增，使缓存失效
TIERS = {"must": "必须复核", "sample": "抽样质检", "impact": "高影响", "other": "其他"}


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
    unscreened = key(dataset, doc_id, i) in set(tiers(dataset)["unscreened"])
    if not unscreened and (not labels or not any(x["i"] == i for x in labels["labels"])):
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
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **({"unscreened": True} if unscreened else {})}
    ensure_dir()
    with open(REVIEW / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _rank(seed: int, k: str) -> int:
    return int(hashlib.sha256(f"{seed}:{k}".encode()).hexdigest()[:12], 16)


def _signature(dataset: str) -> str:
    d = ds_dir(dataset)
    labels = d / "labels"
    parts = [TIERS_VERSION, glossary()["version"], json.dumps(pipeline(), sort_keys=True, default=str)]   # 含清理规则：高影响依赖打分口径
    for p in (labels, labels / "_cache.json", d / "filings.parquet"):   # 不含复核记录：避免每次复核后重算高影响
        parts.append(str(p.stat().st_mtime_ns) if p.exists() else "-")
    parts.append(str(sum(1 for _ in labels.glob("*.json"))) if labels.exists() else "0")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def tiers(dataset: str) -> dict:
    """各分组包含的段落键。结果缓存在 <数据集>/review_tiers.json，标注、复核记录或设置变化时重算。"""
    d = ds_dir(dataset)
    sig = _signature(dataset)
    cache_file = REVIEW / f"tiers_{dataset}.json"
    cached = read_json(cache_file)
    if cached and cached.get("signature") == sig:
        return cached
    cfg = pipeline()["review"]
    seed = int(cfg["sample_seed"])
    simulated = bool(DATASETS[dataset]["simulated"])
    must, pool, files, skip = [], [], [], []
    meta = pd.read_parquet(d / "filings.parquet") if (d / "filings.parquet").exists() else pd.DataFrame(columns=["id"])
    files = meta.to_dict("records")
    excluded = []
    labelled = {}
    for f in files:
        f_id = f["id"]
        lab = read_json(d / "labels" / f"{f_id}.json")
        if not lab:
            continue
        # 不计分的段落不进复核与抽样：通知类公告的全部段落、表格行（第 14.9 节 v1.31）
        if series_exclusion(dataset, int(f.get("n_paragraphs") or 0), f.get("title") or ""):
            excluded.append(f_id)
            continue
        doc = read_json(d / "docs" / f"{f_id}.json") or {"paragraphs": []}
        tables = {p["i"] for p in doc["paragraphs"] if is_table(p["text"])}
        labelled[f_id] = {x["i"] for x in lab["labels"]} | tables
        for x in lab["labels"]:
            k = key(dataset, f_id, x["i"])
            if x["i"] in tables:
                skip.append(k)
                continue
            if set(x.get("flags") or []) & set(cfg["must_flags"]):
                must.append(k)
            if x.get("method") == "llm":
                pool.append(k)
    sample = [] if simulated else sorted(pool, key=lambda k: _rank(seed, k))[:int(cfg["sample_size"])]
    unscreened = []
    if not simulated and pipeline()["screening"] == "glossary" and cfg["sample_unscreened"]:
        for f_id in sorted(labelled, key=lambda x: _rank(seed, f"doc:{x}")):   # 随机公告，每篇取一段未送大模型的段落
            doc = read_json(d / "docs" / f"{f_id}.json") or {"paragraphs": []}
            cand = [p for p in doc["paragraphs"] if p["i"] not in labelled[f_id] and len(p["text"].strip()) >= 40]
            if cand:
                p = min(cand, key=lambda p: _rank(seed, key(dataset, f_id, p["i"])))
                unscreened.append(key(dataset, f_id, p["i"]))
            if len(unscreened) >= int(cfg["sample_unscreened"]):
                break
    impact = [] if simulated else _impact(dataset, int(cfg["high_impact_top"]))   # 模拟数据复核豁免，不排高影响
    out = {"signature": sig, "dataset": dataset, "must": must, "sample": sample, "unscreened": unscreened, "impact": impact,
           "skip": skip, "excluded_docs": excluded,
           "pool": len(pool), "settings": cfg, "screening": pipeline()["screening"]}
    try:
        ensure_dir()
        write_shared(cache_file, out)
    except OSError:
        pass
    return out


def _impact(dataset: str, top: int) -> list[str]:
    """高影响：该段判断一旦相反（方向反转），所在公告的"风险变化"就会变号。按 |2 × 贡献| ÷ |风险变化| 排序取前 N。
    只看触发复核的段落；只计本篇的变化（对下一篇变化的反向影响未计入）。"""
    d = ds_dir(dataset)
    if not (d / "aligned.parquet").exists():
        return []
    from factopia.text import signal                       # signal 依赖本模块，延迟导入
    try:
        df = signal.scores(dataset, realign=False, save=False)
    except Exception:
        return []
    if df.empty:
        return []
    dec = decisions()
    cand = []
    for r in df.dropna(subset=["change"]).to_dict("records"):
        ch = r["change"]
        if not ch:
            continue
        lab = read_json(d / "labels" / f"{r['id']}.json") or {"labels": [], "paragraphs_total": 1}
        n = max(int(lab["paragraphs_total"]), 1)
        for x in lab["labels"]:
            if not x.get("flags"):
                continue
            eff = apply(x, dec.get(key(dataset, r["id"], x["i"])))
            v = sum(c["direction"] * c["confidence"] for c in eff["concepts"]) / n * 100
            if v and (v > 0) == (ch > 0) and 2 * abs(v) >= abs(ch):
                cand.append((2 * abs(v) / abs(ch), key(dataset, r["id"], x["i"])))
    return [k for _, k in sorted(cand, reverse=True)[:top]]


def _tier_of(k: str, t: dict) -> str | None:
    if k in t["_must"]:
        return "must"
    if k in t["_sample"]:
        return "sample"
    if k in t["_impact"]:
        return "impact"
    return None


def queue(dataset: str | None = None, status: str = "pending", limit: int = 100, tier: str = "all") -> dict:
    dec = decisions()
    names = concept_names()
    order = {t: n for n, t in enumerate(TIERS)}
    picked = []                                          # 先确定分组与顺序，只为显示的前 limit 条读取原文
    counts = {"pending": 0, "done": 0, "tiers": {t: {"pending": 0, "done": 0} for t in TIERS}}
    for ds in ([dataset] if dataset else list(DATASETS)):
        d = ds_dir(ds)
        if not (d / "filings.parquet").exists():
            continue
        t = tiers(ds)
        t = {**t, "_must": set(t["must"]), "_sample": set(t["sample"]) | set(t["unscreened"]), "_impact": set(t["impact"])}
        skip, excluded_docs = set(t.get("skip") or []), set(t.get("excluded_docs") or [])
        unscreened = set(t["unscreened"])
        meta = pd.read_parquet(d / "filings.parquet").set_index("id")
        by_doc: dict[str, set[int]] = {}
        for k in unscreened:
            _, f_id, i = k.rsplit(":", 2)
            by_doc.setdefault(f_id, set()).add(int(i))
        for f_id in reversed(meta.index.tolist()):
            if f_id in excluded_docs:
                continue
            lab = read_json(d / "labels" / f"{f_id}.json")
            if not lab:
                continue
            rows = list(lab["labels"]) + [{"i": i, "concepts": [], "flags": [], "method": "unscreened"} for i in sorted(by_doc.get(f_id, ()))]
            for x in rows:
                k = key(ds, f_id, x["i"])
                if k in skip:
                    continue
                tr = _tier_of(k, t) or ("other" if x.get("flags") else None)
                if tr is None:                               # 未触发复核、也不在抽样中的段落不进队列
                    continue
                done = k in dec
                counts["done" if done else "pending"] += 1
                counts["tiers"][tr]["done" if done else "pending"] += 1
                if (status == "pending" and done) or (status == "done" and not done) or (tier != "all" and tr != tier):
                    continue
                picked.append((order[tr], len(picked), ds, f_id, x, k, tr, done, lab, meta, unscreened))
    picked.sort(key=lambda r: r[:2])
    items, docs = [], {}
    for _, _, ds, f_id, x, k, tr, done, lab, meta, unscreened in picked[:limit]:
                doc = docs.setdefault((ds, f_id), read_json(ds_dir(ds) / "docs" / f"{f_id}.json"))
                m = meta.loc[f_id]
                eff = apply(x, dec.get(k)) if x["method"] != "unscreened" else {**x, "translation": {}}
                if x["method"] == "unscreened" and dec.get(k) and dec[k].get("concepts"):
                    eff["concepts"] = dec[k]["concepts"]
                items.append({
                    "key": k, "dataset": ds, "doc_id": f_id, "i": x["i"], "simulated": bool(DATASETS[ds]["simulated"]),
                    "tier": tr, "tier_zh": TIERS[tr], "sample_kind": ("unscreened" if k in unscreened else "screened") if tr == "sample" else None,
                    "company": m["company"], "instrument": m["instrument"], "title": m["title"],
                    "published_at": m["published_at"], "language": lab["language"],
                    "language_zh": LANG_ZH.get(lab["language"], lab["language"]),
                    "original": doc["paragraphs"][x["i"]]["text"], "translation": eff.get("translation") or {},
                    "translation_confidence": x.get("translation_confidence"), "model": x.get("model") or lab["model"],
                    "glossary_version": lab["glossary_version"], "method": x["method"],
                    "concepts": [{**c, "name": names.get(c["concept"], c["concept"])} for c in eff["concepts"]],
                    "model_concepts": [{**c, "name": names.get(c["concept"], c["concept"])} for c in x["concepts"]],
                    "flags": [{"code": f, "label": FLAG_ZH.get(f, f)} for f in x["flags"]],
                    "decision": dec.get(k), "history": history(k) if done else [],
                })
    return {"items": items, "counts": counts, "actions": ACTIONS, "tiers_zh": TIERS}


def pending_must(dataset: str) -> int:
    """必须复核组中尚未复核的段落数（阻止提交审批）。"""
    dec = decisions()
    return sum(1 for k in tiers(dataset)["must"] if k not in dec)


def _wilson(k: int, n: int) -> tuple[float, float] | None:
    if not n:
        return None
    z, p = 1.96, k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def _same(a: list[dict], b: list[dict]) -> bool:
    return {(c["concept"], 1 if c["direction"] > 0 else -1) for c in a or []} == {(c["concept"], 1 if c["direction"] > 0 else -1) for c in b or []}


def accuracy(dataset: str) -> dict:
    """抽样质检结果：大模型判断的准确率（按是否触发复核、按复核原因分组）与术语表漏检率，附 95% 区间（Wilson）。
    判断正确 = 复核人确认，或判定不涉及风险且模型本来也没有标注概念，或修正时只改译文、概念与方向不变。"""
    d = ds_dir(dataset)
    t = tiers(dataset)
    dec = decisions()
    groups: dict[str, list[int]] = {}
    def add(g, ok):
        groups.setdefault(g, [0, 0])
        groups[g][0] += ok
        groups[g][1] += 1
    labels_cache: dict[str, dict] = {}
    for k in t["sample"]:
        r = dec.get(k)
        if not r:
            continue
        _, f_id, i = k.rsplit(":", 2)
        lab = labels_cache.setdefault(f_id, read_json(d / "labels" / f"{f_id}.json") or {"labels": []})
        x = next((y for y in lab["labels"] if y["i"] == int(i)), None)
        if not x:
            continue
        model = x["concepts"]
        ok = r["action"] == "confirm" or (r["action"] == "reject" and not model) or (
            r["action"] == "correct" and (r.get("concepts") is None or _same(r["concepts"], model)))
        add("all", ok)
        add("flagged" if x.get("flags") else "unflagged", ok)
        for f in x.get("flags") or []:
            add(f"flag:{f}", ok)
    miss = [0, 0]
    for k in t["unscreened"]:
        r = dec.get(k)
        if r:
            miss[1] += 1
            miss[0] += bool(r.get("concepts")) and r["action"] == "correct"
    label_zh = {"all": "全部抽样", "flagged": "触发复核的段落", "unflagged": "未触发复核的段落", **{f"flag:{c}": v for c, v in FLAG_ZH.items()}}
    rows = [{"group": g, "group_zh": label_zh.get(g, g), "correct": k, "reviewed": n, "accuracy": round(k / n, 3), "ci95": _wilson(k, n)}
            for g, (k, n) in sorted(groups.items(), key=lambda kv: ({"all": 0, "flagged": 1, "unflagged": 2}.get(kv[0], 3), kv[0]))]
    return {"dataset": dataset, "sample_size": len(t["sample"]), "pool": t["pool"], "reviewed": groups.get("all", [0, 0])[1], "rows": rows,
            "unscreened": {"sample_size": len(t["unscreened"]), "reviewed": miss[1], "missed": miss[0],
                           "miss_rate": round(miss[0] / miss[1], 3) if miss[1] else None, "ci95": _wilson(miss[0], miss[1])},
            "screening": t["screening"], "settings": t["settings"],
            "note": "抽样按固定种子随机抽取，复核完全部样本后的准确率可代表全体大模型判断；样本少时区间较宽"}


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
