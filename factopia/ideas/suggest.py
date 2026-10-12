"""AI 默认假设（Spec 第 17.11 节）：线索池除论文 / 研报外的渠道都给出 AI 生成的默认假设，每次不同。

- 人工渠道（投资假设 / 研究员经验 / 人工反馈）：打开表单即由 AI 生成一条建议预填，可"换一个"，研究员修改后提交；
  提交时记录 AI 原稿与是否经人修改（BR-46）。
- 自动回流渠道（监控预警 / 组合归因 / 审批退回）：AI 把平台记录改写为可检验的研究假设，转立项时使用。

依据（体现 AI 挖掘，而不是凭空编写）：平台可用数据、三个市场的交易差异、因子库的名称与说明、以往研究题目、
近期的监控与组合线索。防泄漏：不提供任何测试期（2022 年起）以后的统计结果，只提供名称与说明。
每次不同：随机研究角度 + 随机市场侧重 + 避开最近的建议与已有线索；生成后按词语重合度查重，过于相似则重试一次。
"""
import json
import random
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from factopia import ideas as I

PROMPT_VERSION = "idea-suggest-v1"
PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
AUTO_PROMPT_VERSION = "idea-auto-v1"
AUTO_PROMPT_FILE = Path(__file__).parent / "prompts" / f"{AUTO_PROMPT_VERSION}.md"
MAX_OVERLAP = 0.45

ANGLES = [
    "短期反转与流动性提供", "波动率调整后的中期动量", "成交量与价格背离", "换手率异常与关注度", "低波动异象",
    "公告发布时点与盘后信息消化", "管理层风险措辞的变化", "跨市场领先滞后（同一发行人在不同市场挂牌）",
    "交易成本与印花税对高换手信号的侵蚀", "节假日与交易日历差异带来的错位", "行业内相对强弱",
    "市场急跌后的恢复速度", "指数成分调整前后的价格压力", "汇率波动对本地股票收益的传导", "开盘与收盘价格行为差异",
    "大市值与中小市值的信号衰减差异", "流动性冲击后的价格回归", "连续涨跌天数与过度反应",
]
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


def _chat(prompt: str) -> tuple[dict, dict, str]:
    from factopia.research import draft as drafting
    cfg = drafting.llm_config()
    text, usage = drafting._chat(prompt, cfg)
    try:
        return json.loads(text), usage, cfg["model"]
    except json.JSONDecodeError as e:
        raise I.IdeaError(f"大模型输出不是合法 JSON：{e}") from e


def suggest(channel: str, markets: list[str] | None = None, avoid: list[str] | None = None) -> dict:
    if channel not in CHANNEL_TASK:
        raise I.IdeaError("论文 / 研报渠道不生成默认假设；可选：投资假设、研究员经验、人工反馈")
    ms = [m for m in I.MARKETS if m in (markets or I.MARKETS)] or list(I.MARKETS)
    recent = [r["title"] for r in _recent()] + [i["title"] for i in I.all_ideas()[:40]] + list(avoid or [])
    focus = random.choice(ms + ["all"] * 2)
    angle = random.choice(ANGLES)
    ctx = _context(ms)
    out, usage_all, model, note = None, {"prompt_tokens": 0, "completion_tokens": 0}, None, ""
    for attempt in range(2):
        prompt = PROMPT_FILE.read_text(encoding="utf-8").format(
            task=CHANNEL_TASK[channel], angle=angle, focus=("all markets in scope" if focus == "all" else I.MARKET_ZH[focus] + f" ({focus})"),
            markets=ctx["markets"], data=ctx["data"], facts="\n".join(f"[K{i + 1}] {f}" for i, f in enumerate(ctx["facts"])) or "- none",
            avoid="\n".join(f"- {t}" for t in recent[-40:]) or "- none", roles=", ".join(ROLE_CHOICES), note=note)
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
        note = f"\n\nYour previous suggestion \"{title}\" was too similar to an existing one. Propose a clearly different idea."
        angle = random.choice([a for a in ANGLES if a != angle])
    if not out:
        raise I.IdeaError("AI 两次都没有给出完整的建议，请再试一次")
    nk = len(ctx["facts"])
    cites = sorted({int(x) for x in re.findall(r"\d+", " ".join(map(str, out.get("grounds") or []))) if 1 <= int(x) <= nk})
    sug = {"id": f"sug_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{random.randrange(16 ** 4):04x}",
           "channel": channel, "title": str(out["title"]).strip()[:200], "body": str(out["body"]).strip()[:4000],
           "direction": str(out.get("direction") or "")[:500], "period": str(out.get("period") or "")[:200],
           "feedback_role": out.get("feedback_role") if out.get("feedback_role") in ROLE_CHOICES else ROLE_CHOICES[0],
           "markets": [m for m in I.MARKETS if m in (out.get("markets") or ms)] or ms,
           "why_zh": str(out.get("why_zh") or "")[:600], "grounds": [ctx["facts"][k - 1] for k in cites][:5],
           "angle": angle, "model": model, "prompt_version": PROMPT_VERSION, "usage": usage_all, "at": _now()}
    I.DIR.mkdir(parents=True, exist_ok=True)
    with _log().open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: sug[k] for k in ("id", "channel", "title", "angle", "model", "prompt_version", "at")}, ensure_ascii=False) + "\n")
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
    raw, usage, model = _chat(prompt)
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
