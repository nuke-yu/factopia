"""AI 默认假设（Spec 第 17.11 节）：线索池除论文 / 研报外的渠道都给出 AI 生成的默认假设。

- 人工渠道（投资假设 / 研究员经验 / 人工反馈）：打开表单即由 AI 生成一条建议预填，可"换一个"，研究员修改后提交；
  提交时记录 AI 原稿与是否经人修改（BR-46）。
- 自动回流渠道（监控预警 / 组合归因 / 审批退回）：AI 把平台记录改写为可检验的研究假设，转立项时使用。

人工渠道的方向（v2）：研究员给了想法种子就以它为主线；否则由平台按证据给研究方向排序（研究缺口、测试期前的显著因子、
以往轮次是否采纳、排除方向、获批信号，见 directions.py），在得分最高的几个方向中加权抽取，系统给出选择理由。
AI 拿到编号的证据条目（[E#]），引用后在页面显示。防泄漏：以往运行只有方向与是否采纳，因子统计只用测试期前的数据。
每次不同：最近用过的方向降低优先级、"换一个"排除当前方向、避开已有线索与研究题目；按词语重合度查重，过于相似则重试一次。
自动回流渠道与线索合并仍使用 _context（平台背景，只有名称与说明）。
"""
import json
import random
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from factopia import ideas as I

PROMPT_VERSION = "idea-suggest-v2"
PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
AUTO_PROMPT_VERSION = "idea-auto-v1"
AUTO_PROMPT_FILE = Path(__file__).parent / "prompts" / f"{AUTO_PROMPT_VERSION}.md"
MAX_OVERLAP = 0.45

CHANNEL_TASK = {
    "hypothesis": "an investment hypothesis a quant researcher could test",
    "experience": "a market observation phrased the way an experienced local researcher would describe it from practice "
                  "(it is an AI-drafted example the researcher will replace or edit with their own experience)",
    "feedback": "a piece of feedback a portfolio manager, client team or local research team could raise, which leads to a testable research question "
                "(it is an AI-drafted example; the submitter will replace it with the real feedback)",
}
ROLE_CHOICES = ["投资组合经理", "客户团队", "本地研究员（香港）", "本地研究员（英国）", "本地研究员（法国）"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log() -> Path:
    return I.DIR / "suggestions.jsonl"                 # 建议记录：用于避开重复，也是 AI 建议的留痕


def _recent(n: int = 40) -> list[dict]:
    if not _log().exists():
        return []
    return [json.loads(x) for x in _log().read_text(encoding="utf-8").splitlines()[-n:] if x.strip()]


def _overlap(a: str, b: str) -> float:
    from factopia.research import semantic
    x, y = Counter(semantic.tokens(a)), Counter(semantic.tokens(b))
    return sum((x & y).values()) / (sum((x | y).values()) or 1)


def _context(markets: list[str]) -> dict:
    """提供给 AI 的平台背景：只有名称与说明，不含测试期以后的统计。"""
    facts = []
    try:
        from factopia.research import library
        al = [f for f in library.alpha158() if f.get("description")]
        for f in random.sample(al, min(10, len(al))):
            facts.append(f"factor library (public baseline): {f['name']} — {f['description']}")
        for f in [x for x in library.approved() if x.get("approved_markets")][:5]:   # 只提供实际获批市场的信号
            facts.append(f"approved internal signal: {f['name']} — {f.get('description') or ''}")
    except Exception:
        pass
    try:
        from factopia.research import store
        for rid in store.research_ids()[:20]:
            vs = store.versions(rid)
            if vs:
                sp = store.load_version(rid, vs[-1])
                facts.append(f"past internal research: {sp.get('title_zh') or rid} (hypothesis direction only, results not shown)")
    except Exception:
        pass
    for i in I.all_ideas()[:15]:
        if i["channel"] in ("monitor", "portfolio", "approval"):
            facts.append(f"recent platform signal ({i['channel_zh']}): {i['title']}")
    random.shuffle(facts)
    return {"facts": facts[:24],
            "data": "available ONLY for Hong Kong, UK and French stocks in scope: daily open/high/low/close and volume, the three local benchmark indices (Hang Seng, FTSE 100, CAC 40), FX rates HKD/GBP/EUR, company announcement text (French real filings from 2024-08; "
                    "Hong Kong / UK announcement text is simulated demo data); NOT available: futures, options, US or other markets, intraday data, index membership change records, "
                    "fundamentals / accounting data, analyst data, alternative data",
            "markets": "Hong Kong (Hang Seng constituents, HKD, stamp duty on both sides, board lots), UK (FTSE 100, GBP, 0.5% stamp duty on buys), "
                       "France (CAC 40, EUR, 0.4% financial transaction tax on buys); markets in scope for this idea: " + ", ".join(markets)}


def _chat(prompt: str, task: str = "线索 AI 建议") -> tuple[dict, dict, str]:
    from factopia.research import draft as drafting
    cfg = drafting.llm_config()
    text, usage = drafting._chat(prompt, cfg, task)
    try:
        return json.loads(text), usage, cfg["model"]
    except json.JSONDecodeError as e:
        raise I.IdeaError(f"大模型输出不是合法 JSON：{e}") from e


def suggest(channel: str, markets: list[str] | None = None, avoid: list[str] | None = None, seed: str = "",
            skip_angle: str | None = None) -> dict:
    """seed：研究员给的想法种子（可选），填了就以它为主线；不填时按平台证据选方向（factopia.ideas.directions）。
    skip_angle："换一个"时排除当前方向。"""
    from factopia.ideas import directions as D
    if channel not in CHANNEL_TASK:
        raise I.IdeaError("论文 / 研报渠道不生成默认假设；可选：投资假设、研究员经验、人工反馈")
    seed = (seed or "").strip()[:500]
    ms = [m for m in I.MARKETS if m in (markets or I.MARKETS)] or list(I.MARKETS)
    log = _recent()
    ev = D.gather(ms)
    recent = [r["title"] for r in log] + [i["title"] for i in ev["ideas"][:40]] + [s["title"] for s in ev["studies"]] + list(avoid or [])
    board = D.choose(ms, [r.get("angle") or "" for r in log], ev, skip_angle, draw=not seed)
    pick = board if not seed else None
    if pick:
        angle, market = pick["angle"]["zh"], pick["market"]
        selection = pick["why"]
        direction = (f"Research direction selected by the platform from its evidence: {angle} — market focus: {I.MARKET_ZH[market]} ({market}).\n"
                     "Why the platform selected it: " + " / ".join(selection))
    else:
        angle, market, selection = "研究员的想法", None, ["以研究员输入的想法为主线，平台证据作为补充；下方打分表仅供参考，未用于选方向"]
        direction = (f"The researcher gave this idea seed; develop it into a testable idea and keep its core intent:\n\"{seed}\"\n"
                     "If part of the seed cannot be tested with the available data, keep the testable part and say what was dropped in why_zh.")
    items = D.evidence_items(pick["angle"] if pick else None, pick["coverage"] if pick else None, market, ms, ev, seed)
    ctx = _context(ms)
    out, usage_all, model, note, prompt = None, {"prompt_tokens": 0, "completion_tokens": 0}, None, "", ""
    for attempt in range(2):
        prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
            task=CHANNEL_TASK[channel], direction=direction, markets=ctx["markets"], data=ctx["data"],
            evidence="\n".join(f"[{it['id']}] {it['text']}" for it in items) or "- none",
            avoid="\n".join(f"- {t}" for t in recent[-60:]) or "- none", roles=", ".join(ROLE_CHOICES), note=note)
        raw, usage, model = _chat(prompt)
        for k in usage_all:
            usage_all[k] += int(usage.get(k) or 0)
        title = str(raw.get("title") or "").strip()[:200]
        body = str(raw.get("body") or "").strip()[:4000]
        if not title or len(body) < 20:
            note = "\n\nYour previous answer was missing a title or body. Answer again."
            continue
        sim = max([_overlap(f"{title}", t) for t in recent] or [0])
        out = raw
        if sim <= MAX_OVERLAP:
            break
        note = f"\n\nYour previous suggestion \"{title}\" was too similar to an existing one. Propose a clearly different idea within the same direction."
    if not out:
        raise I.IdeaError("AI 两次都没有给出完整的建议，请再试一次")
    by_id = {it["id"]: it for it in items}
    cited = [f"E{n}" for n in dict.fromkeys(re.findall(r"E?(\d+)", " ".join(map(str, out.get("grounds") or []))))]
    grounds = [{"kind": by_id[c]["kind"], "text": by_id[c]["zh"]} for c in cited if c in by_id][:8]
    evidence = [{"id": it["id"], "kind": it["kind"], "zh": it["zh"], "en": it["text"], "cited": it["id"] in cited} for it in items]
    sug = {"id": f"sug_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{random.randrange(16 ** 4):04x}",
           "channel": channel, "title": str(out["title"]).strip()[:200], "body": str(out["body"]).strip()[:4000],
           "direction": str(out.get("direction") or "")[:500], "period": str(out.get("period") or "")[:200],
           "feedback_role": out.get("feedback_role") if out.get("feedback_role") in ROLE_CHOICES else ROLE_CHOICES[0],
           "markets": [m for m in I.MARKETS if m in (out.get("markets") or ms)] or ms,
           "why_zh": str(out.get("why_zh") or "")[:600], "grounds": grounds,
           "angle": angle, "focus_market": market, "selection": selection, "candidates": (pick or {}).get("candidates") or [],
           "seed": seed, "evidence_n": len(items), "evidence": evidence, "table": board["table"], "dimensions": D.DIMENSIONS,
           "inputs": D.inputs_summary(ev, ms, log, recent, avoid or []), "prompt": prompt,
           "model": model, "prompt_version": PROMPT_VERSION, "usage": usage_all, "at": _now()}
    I.DIR.mkdir(parents=True, exist_ok=True)
    with _log().open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: sug[k] for k in ("id", "channel", "title", "angle", "focus_market", "seed", "model", "prompt_version", "at")}, ensure_ascii=False) + "\n")
    return sug


def auto_hypothesis(iid: str) -> dict:
    """自动回流线索：AI 把平台记录（监控预警、组合归因、审批退回）改写为可检验的研究假设，存入线索并留痕。"""
    idea = I.load(iid)
    if idea["channel"] not in ("monitor", "portfolio", "approval"):
        raise I.IdeaError("只有自动回流的线索需要 AI 改写为研究假设")
    if idea.get("ai_hypothesis"):
        return idea
    ctx = _context(idea["markets"] or list(I.MARKETS))
    prompt = AUTO_PROMPT_FILE.read_text(encoding="utf-8").format(
        channel=idea["channel_zh"], title=idea["title"], body=idea["body"], markets=ctx["markets"], data=ctx["data"])
    raw, usage, model = _chat(prompt, "线索 AI 研究假设")
    hyp = str(raw.get("hypothesis") or "").strip()
    if len(hyp) < 20:
        raise I.IdeaError("AI 没有给出完整的研究假设，请再试一次")
    idea["ai_hypothesis"] = {"title": str(raw.get("title") or idea["title"])[:200], "hypothesis": hyp[:3000],
                             "direction": str(raw.get("direction") or "")[:500], "why_zh": str(raw.get("why_zh") or "")[:600],
                             "model": model, "prompt_version": AUTO_PROMPT_VERSION,
                             "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")}, "at": _now()}
    I._save(idea)
    I._append(iid, {"action": "ai_hypothesis", "by": "AI", "model": model, "prompt_version": AUTO_PROMPT_VERSION})
    return idea
