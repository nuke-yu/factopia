"""起点因子：以因子库中已有因子为起点做变体或组合研究（Spec 第 16.7 节扩展）。

背景：RD-Agent 的起点回测固定使用 Alpha158 的 20 个基础因子，研究员无法指定"从哪个因子出发"。
本模块让研究员（或 AI）先挑出因子库中的已有因子，把"变体 / 组合"意图写进研究规格，
再由 context.research_section() 注入 RD-Agent 的提示词，研究过程因此从这些因子出发。

三种入口（同一套数据，见 propose 的 mode）：
- variant：单个因子做变体（窗口、符号、标准化方式、条件化、跨市场参数），由 AI 给出具体改法；
- combo：多个因子做组合（合成方式、权重、条件触发、分层），由 AI 给出组合逻辑；
- open：不给因子，AI 先 review 因子库（按测试期前的统计挑候选），再自主提出变体或组合。

两条硬约束：
1. 防泄漏（MINE-11）：提供给 AI 的只有因子名称、说明、公式 / 代码与测试期之前的统计；
   获批信号与以往研究的测试期数值一律不放入提示词。
2. 引用可追溯：AI 提出的构想必须引用因子库中真实存在的编号（cites），引用不存在编号的构想被丢弃并计入 dropped，
   与 explain.filter_sentences 的做法一致；研究员能据此追到具体因子。
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from factopia.lang import t

PROMPT_VERSION = "rs-seeds-v1"
PROMPT_FILE = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
MODES = ("variant", "combo", "open")
MODE_ZH = {"variant": "单因子变体", "combo": "多因子组合", "open": "AI 自主审查"}
MAX_SEEDS = 6
MAX_POOL = 40
CJK = re.compile(r"[\u3400-\u9fff]")


class SeedError(ValueError):
    """起点因子相关的输入错误（前端可直接展示）。"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 1. 候选因子

def _train_stats(item: dict) -> dict:
    """测试期之前的统计（Alpha158 为 2015–2021）；获批信号的统计来自测试期，不提供。"""
    if item["source"] == "alpha158":
        return {m: v for m, v in (item.get("markets") or {}).items() if v.get("rank_ic") is not None}
    return {}


def candidates(markets: list[str] | None = None, source: str | None = None) -> dict:
    """因子库中可作为起点的条目。

    按来源分为三组，每条带名称、说明、公式 / 代码与（测试期之前的）统计。
    alpha158 按首选市场的 |t| 排序，便于研究员在长清单里先看到有信息量的因子。
    """
    from factopia.research import library
    ms = [m for m in (markets or ["hk", "uk", "fr"]) if m in ("hk", "uk", "fr")]
    focus = (ms or ["hk"])[0]
    items = library.items([source] if source else None)

    out = []
    for i in items:
        if i["source"] == "approved" and not i.get("approved_markets"):
            continue                              # 所在市场未批准的信号不能作为起点
        stats = _train_stats(i)
        t_val = (stats.get(focus) or {}).get("t")
        out.append({
            "id": i["id"], "name": i["name"], "source": i["source"], "source_zh": library.SOURCE_ZH[i["source"]],
            "status": i["status"], "group": i.get("group"), "description": i.get("description") or "",
            "formulation": i.get("formulation"), "code": i.get("code"),
            "markets": {m: stats[m] for m in ms if m in stats}, "label": i.get("label"),
            "period_note": i.get("period_note"), "provenance": i.get("provenance"),
            "approved_markets": i.get("approved_markets") or [],
            "_t": t_val,
        })
    out.sort(key=lambda x: (x["source"] != "approved", -(abs(x["_t"]) if x["_t"] is not None else -1), x["name"]))
    for x in out:
        x.pop("_t", None)
    groups = {library.SOURCE_ZH[k]: [x["id"] for x in out if x["source"] == k] for k in library.SOURCES}
    return {"items": out, "counts": {k: len(v) for k, v in groups.items()},
            "note": "Alpha158 的统计为 2015–2021（测试期之前）；获批信号的统计来自测试期，不提供给 AI，也不在此显示。"}


def _by_id(ids: list[str]) -> list[dict]:
    """按编号取因子；不存在的编号报错（前端可能是旧页面或手改）。"""
    pool = {x["id"]: x for x in candidates()["items"]}
    bad = [i for i in ids if i not in pool]
    if bad:
        raise SeedError(t("因子库中不存在这些编号：{ids}", ids="、".join(bad[:5])))
    return [pool[i] for i in ids]


SAFE_FIELDS = ("id", "name", "source", "source_zh", "description", "formulation", "code", "approved_markets")


def normalize(raw: dict | None) -> dict | None:
    """把前端传来的起点因子整理成写入研究规格的形式。

    只保留 SAFE_FIELDS：任何统计数值都不进规格，因此也不会随提示词发给 AI（MINE-11）。
    编号一律以因子库为准重新取，前端传来的名称 / 说明不作数（防伪造引用）。
    """
    if not raw:
        return None
    raw_items = raw.get("items") or []
    ids = []
    for i in raw_items:
        v = str(i.get("id") if isinstance(i, dict) else i).strip()
        if v:
            ids.append(v)
    if not ids and not str(raw.get("brief") or "").strip():
        return None
    mode = raw.get("mode") if raw.get("mode") in MODES else ("combo" if len(ids) > 1 else "variant")
    its = _by_id(list(dict.fromkeys(ids))[:MAX_SEEDS]) if ids else []
    prop = raw.get("proposal") or {}
    out = {
        "mode": mode, "mode_zh": MODE_ZH[mode],
        "items": [{k: x.get(k) for k in SAFE_FIELDS} for x in its],
        "brief": str(raw.get("brief") or "").strip()[:2000],
        "intent": str(raw.get("intent") or "").strip()[:600],
        "at": now(),
    }
    if prop:
        out["proposal"] = {
            "title_zh": str(prop.get("title_zh") or "").strip()[:120],
            "change": str(prop.get("change") or "").strip()[:400],
            "logic_zh": str(prop.get("logic_zh") or "").strip()[:600],
            "risk_zh": str(prop.get("risk_zh") or "").strip()[:400],
            "cites": [str(c) for c in (prop.get("cites") or []) if str(c) in {x["id"] for x in its}][:MAX_SEEDS],
            "model": prop.get("model"), "prompt_version": prop.get("prompt_version"),
        }
        out["proposal"]["from_ai"] = bool(out["proposal"]["cites"]) or raw.get("from_ai", False)
    return out



# ---------------------------------------------------------------- 2. 提供给 AI 的清单

def _line(x: dict) -> str:
    """一个因子在提示词中的一行：只有名称、说明、公式 / 代码与（测试期前的）统计。"""
    bits = [f"- {x['id']} ({x['name']}, {x['source_zh']})"]
    if x.get("description"):
        bits.append(f": {x['description']}")
    if x.get("formulation"):
        bits.append(f" | formula: {x['formulation']}")
    elif x.get("code"):
        bits.append(f" | implementation: {x['code'].strip().splitlines()[-1][:160]}")
    st = [(m, v) for m, v in x["markets"].items() if v.get("t") is not None]
    if st:
        bits.append(" | pre-test statistics: " + "; ".join(f"{m.upper()} rank IC {v['rank_ic']:.3f}, t={v['t']:.2f}" for m, v in st))
    if x.get("label"):
        bits.append(f" | cross-market class: {x['label']}")
    return "".join(bits)


def review_pool(markets: list[str], horizon: int) -> list[dict]:
    """AI review 用的候选池：优先已获批信号与有显著统计的公开基准因子，最多 MAX_POOL 条。"""
    its = candidates(markets)["items"]
    approved = [x for x in its if x["source"] == "approved"]
    scored = [x for x in its if x["source"] != "approved" and x["markets"]
              and any(abs((v.get("t") or 0)) >= 2 for v in x["markets"].values())]
    rest = [x for x in its if x not in approved and x not in scored]
    pool = approved + scored + rest
    return pool[:MAX_POOL]


# ---------------------------------------------------------------- 3. AI 提构想

def _prompt(seeds: list[dict], mode: str, brief: str, markets: list[str], horizon: int, n: int, pool: list[dict]) -> str:
    from factopia.research.context import MARKETS
    ms = ", ".join(f"{MARKETS[m]['name']}" for m in markets)
    parts = [
        PROMPT_FILE.read_text(encoding="utf-8").strip(),
        "",
        f"## Scope",
        f"- Markets: {ms}",
        f"- Prediction horizon: {horizon} trading days",
        f"- Requested mode: {mode} ({MODE_ZH[mode]})",
        f"- Produce at most {n} ideas, ordered by how likely they are to be worth a full research run.",
        "",
        "## Starting factors selected by the researcher",
        *([_line(x) for x in seeds] or ["- (none selected: review the candidate pool below and choose the starting points yourself)"]),
    ]
    if brief.strip():
        parts += ["", "## The researcher's own description (may be in Chinese, English or French)", brief.strip(),
                  "Turn this description into testable research ideas. Keep the researcher's intent; "
                  "if it is ambiguous, state the assumption you made in the idea."]
    if mode == "open" or not seeds:
        parts += ["", "## Candidate pool from the internal factor library (names, descriptions and pre-test statistics only)",
                  *[_line(x) for x in pool]]
    parts += ["", "Return JSON only, in the shape described above."]
    return "\n".join(parts)


def _clean(raw: dict, allowed: dict[str, dict]) -> dict | None:
    """校验一条构想：必须引用池中真实存在的因子编号，英文结论字段不含中文。"""
    cites = [str(c) for c in (raw.get("cites") or []) if str(c) in allowed]
    seeds = [str(s) for s in (raw.get("seeds") or []) if str(s) in allowed]
    if not cites:
        return None
    hyp_en = str(raw.get("hypothesis_en") or "").strip()
    fal_en = str(raw.get("falsification_en") or "").strip()
    if not hyp_en or not fal_en or CJK.search(hyp_en + fal_en):
        return None
    kind = raw.get("kind") if raw.get("kind") in ("variant", "combo") else ("combo" if len(cites) > 1 else "variant")
    return {
        "kind": kind, "kind_zh": "变体" if kind == "variant" else "组合",
        "title_zh": str(raw.get("title_zh") or "").strip()[:120],
        "hypothesis_zh": str(raw.get("hypothesis_zh") or "").strip(),
        "hypothesis": hyp_en,
        "direction": str(raw.get("direction_zh") or "").strip()[:600],
        "change": str(raw.get("change_zh") or "").strip()[:400],
        "logic_zh": str(raw.get("logic_zh") or "").strip()[:600],
        "falsification": fal_en,
        "risk_zh": str(raw.get("risk_zh") or "").strip()[:400],
        "seeds": seeds or cites, "cites": cites,
    }


def propose(seed_ids: list[str], mode: str, brief: str, markets: list[str], horizon: int, n: int = 4) -> dict:
    """AI 基于因子库提出变体 / 组合研究构想。

    与 factor library 的 review 走同一个函数：seed_ids 为空时，把候选池交给 AI 自己挑。
    """
    from factopia.research import draft as drafting
    mode = mode if mode in MODES else "open"
    if not markets:
        raise SeedError(t("请先选择市场"))
    seed_ids = list(dict.fromkeys(seed_ids))[:MAX_SEEDS]
    if not seed_ids and not brief.strip() and mode != "open":
        raise SeedError(t("请先选择起点因子，或用一段话描述你想要的变体 / 组合"))
    seeds = _by_id(seed_ids) if seed_ids else []
    pool = review_pool(markets, horizon) if (mode == "open" or not seeds) else []
    if not seeds and not pool:
        raise SeedError(t("因子库中还没有可用作起点的因子（需要先有 Alpha158 评估结果或获批信号）"))

    prompt = _prompt(seeds, mode, brief, markets, horizon, n, pool)
    cfg = drafting.llm_config()
    text, usage = drafting._chat(prompt, cfg, "起点因子构想")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise SeedError(t("大模型输出不是合法 JSON：{e}", e=e)) from e

    allowed = {x["id"]: x for x in (seeds + pool)}
    rows = raw.get("ideas") or raw.get("items") or []
    items, dropped = [], 0
    for r in rows:
        c = _clean(r, allowed)
        if c is None:
            dropped += 1
            continue
        c["seed_detail"] = [{k: allowed[i].get(k) for k in ("id", "name", "source_zh", "description", "formulation")} for i in c["seeds"] if i in allowed]
        items.append(c)
    if not items:
        raise SeedError(t("AI 没有给出可用的构想（引用因子库编号或英文结论字段不合规）；请换个描述再试一次"))
    return {"items": items[:n], "dropped": dropped, "mode": mode, "brief": brief.strip(),
            "seeds": seed_ids, "markets": markets, "horizon": horizon,
            "model": cfg["model"], "prompt_version": PROMPT_VERSION, "at": now(),
            "prompt_chars": len(prompt), "usage": usage}
