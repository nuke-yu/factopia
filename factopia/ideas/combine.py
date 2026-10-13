"""合并转立项（Spec 第 17.12 节）：多条线索由 AI 合成一个可检验的研究假设，或把新线索追加到已有研究（生成新版本）。

- 来源材料与单条转立项相同（prefill）：论文取可做的候选因子，自动回流取 AI 研究假设，其余取原文。
- AI 合成：假设逐句标明来源线索（[I1]…，追加时已有研究为 [B]）；指出线索之间的矛盾与平台数据做不到的部分，
  不硬凑；转立项时去掉引用标记，引用版本留在合成记录里。
- 留痕：runtime/ideas/combos/<id>.json（来源、材料指纹、模型与提示词版本、输出）；锁定研究版本时所有来源线索
  标记为"已转立项"并指向同一研究版本。
- 追加：已有研究的最新锁定版本作为基础，锁定时沿用原研究编号 → 新版本；已检验的旧版本不变。
"""
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from factopia import ideas as I

PROMPT_VERSION = "idea-combine-v1"
PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
MAX_IDEAS = 8
CITE = re.compile(r"\s*\[(?:I\d+|B)\]")


def _dir() -> Path:
    return I.DIR / "combos"


def _material(idea: dict) -> dict:
    keys = None
    if idea["channel"] == "paper":
        keys = [f["key"] for f in (idea.get("extraction") or {}).get("factors") or [] if f["availability"]["ok"]]
        if not keys:
            raise I.IdeaError(f"线索 {idea['id']} 是论文 / 研报，还没有可做的候选因子：请先打开该线索抽取")
    return I.prefill(idea["id"], keys)


def _base(research_id: str) -> dict:
    from factopia.research import store
    vs = store.versions(research_id)
    if not vs:
        raise I.IdeaError(f"研究不存在或还没有锁定的版本：{research_id}")
    sp = store.load_version(research_id, vs[-1])
    ad = sp.get("ai_draft") or {}
    inherited = list(dict.fromkeys([*(ad.get("idea_ids") or []), *([ad["idea_id"]] if ad.get("idea_id") else [])]))
    return {"id": research_id, "version": vs[-1], "title": sp.get("title_zh") or research_id, "hypothesis": sp.get("hypothesis") or "",
            "directions": sp.get("directions") or [], "markets": sp.get("markets") or [], "inherited": inherited}


def combine(ids: list[str], by: str, base: str | None = None) -> dict:
    ids = list(dict.fromkeys(i for i in ids if i))
    by = (by or "").strip()[:60]
    if not by:
        raise I.IdeaError("请填写处理人")
    if len(ids) > MAX_IDEAS:
        raise I.IdeaError(f"一次最多合并 {MAX_IDEAS} 条线索")
    if len(ids) < (1 if base else 2):
        raise I.IdeaError("合并至少选 2 条线索；追加到已有研究至少选 1 条")
    b = _base(base) if base else None
    if b and set(ids) & set(b["inherited"]):
        raise I.IdeaError("所选线索中有已经属于该研究的：" + "、".join(sorted(set(ids) & set(b["inherited"]))))
    srcs = []
    for n, iid in enumerate(ids, 1):
        idea = I.load(iid)
        m = _material(idea)
        srcs.append({"tag": f"I{n}", "id": iid, "channel_zh": idea["channel_zh"], "title": idea["title"], "markets": m["markets"],
                     "hypothesis": m["hypothesis"], "direction": m["direction"], "questions": m["questions"]})
    from factopia.ideas import suggest
    ctx = suggest._context(sorted({mk for s in srcs for mk in s["markets"]} | set(b["markets"] if b else [])))
    blocks = [f"[{s['tag']}] ({s['channel_zh']}; markets: {', '.join(s['markets'])}) {s['title']}\n{s['hypothesis']}"
              + (f"\nDirection: {s['direction']}" if s["direction"] else "") for s in srcs]
    base_text = (f"[B] Existing locked study {b['id']} v{b['version']} — {b['title']}\nHypothesis: {b['hypothesis']}\n"
                 f"Directions: {'; '.join(b['directions']) or '-'}\nMarkets: {', '.join(b['markets'])}") if b else "none (this is a new study)"
    prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
        sources="\n\n".join(blocks), base=base_text, markets=ctx["markets"], data=ctx["data"],
        tags=", ".join(s["tag"] for s in srcs) + (", B" if b else ""))
    raw, usage, model = suggest._chat(prompt, "线索合并")
    cited = str(raw.get("hypothesis") or "").strip()
    if len(cited) < 20:
        raise I.IdeaError("AI 没有给出完整的合成假设，请再试一次")
    tags = {s["tag"] for s in srcs}
    used = set(re.findall(r"\[(I\d+)\]", cited)) & tags
    uses = [{"tag": str(u.get("tag")), "how_zh": str(u.get("how_zh") or "")[:300]} for u in raw.get("uses") or [] if str(u.get("tag")) in tags]
    unused = [s["tag"] for s in srcs if s["tag"] not in used]
    markets = [mk for mk in I.MARKETS if mk in ({mk for s in srcs for mk in s["markets"]} | set(b["markets"] if b else []))] or list(I.MARKETS)
    rec = {"id": f"cmb_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(2)}", "by": by, "at": I.now(),
           "idea_ids": ids, "base": {k: b[k] for k in ("id", "version", "title", "inherited")} if b else None,
           "sources": [{k: s[k] for k in ("tag", "id", "channel_zh", "title", "markets")} | {"material_sha256": hashlib.sha256(s["hypothesis"].encode()).hexdigest()} for s in srcs],
           "title": str(raw.get("title") or "")[:200], "hypothesis_cited": cited[:4000], "hypothesis": CITE.sub("", cited)[:3900],
           "direction": CITE.sub("", str(raw.get("direction") or ""))[:900], "why_zh": str(raw.get("why_zh") or "")[:800], "uses": uses, "unused": unused,
           "conflicts": [str(x)[:400] for x in raw.get("conflicts") or []][:6], "not_testable": [str(x)[:400] for x in raw.get("not_testable") or []][:6],
           "markets": markets, "questions": [q for s in srcs for q in s["questions"]],
           "model": model, "prompt_version": PROMPT_VERSION, "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")}}
    _dir().mkdir(parents=True, exist_ok=True)
    (_dir() / f"{rec['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def load(cid: str) -> dict | None:
    if not re.fullmatch(r"cmb_\d{14}_[0-9a-f]{4}", cid or ""):
        return None
    p = _dir() / f"{cid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
