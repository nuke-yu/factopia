"""AI 解释三个市场的差异（BR-38）。

平台把研究结论中的数据整理成编号的事实清单（F1、F2…：各市场 Rank IC 与 t 值、扣成本收益、成本拖累、换手、容量、汇率贡献、
风格暴露、中性化、市场状态等），并附上团队审核过的市场介绍；大模型据此解释差异的经济原因，判断能否迁移、如何本地化。
每句话必须引用事实编号，引用不存在或没有引用的句子被丢弃；结果连同模型、提示词版本、事实清单一起存档，供研究员确认。
AI 只做解释，不改变平台的判定。
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from factopia.research import draft

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))

PROMPT_VERSION = "explain-v1"
MARKET = {"hk": "Hong Kong", "uk": "UK", "fr": "France"}
PROMPT = """You are a senior quantitative researcher reviewing one hypothesis tested independently in several equity markets.
Explain in Simplified Chinese WHY the results differ across markets, using economic reasoning (market structure, costs and
taxes, liquidity, sector composition, investor base, currency) and say whether the signal can transfer and what local
adjustment (definition, threshold, parameter, cost-aware rebalancing) would be needed.
Rules: use ONLY the numbered facts and market notes below; every sentence must cite at least one fact id like [F3];
never invent numbers; if facts are insufficient, say so. Do not give investment advice. 5-8 sentences.
Return JSON: {{"sentences": [{{"text": "...", "cites": ["F1", "F4"]}}], "transfer_view": "one sentence", "local_adjustments": ["..."]}}
Hypothesis: {hypothesis}
Platform classification: {classification}
Facts:
{facts}
Market notes (team-reviewed, abridged):
{notes}"""


def facts(study: dict, diags: dict) -> list[dict]:
    out = []

    def add(m, key, text):
        out.append({"id": f"F{len(out) + 1}", "market": m, "key": key, "text": text})

    for m, e in study["markets"].items():
        f = e.get("focus") or {}
        mt, sig = f.get("metrics") or {}, f.get("significance") or {}
        if mt:
            add(m, "rank_ic", f"{MARKET[m]}: adopted round rank IC {mt.get('rank_ic')}, Newey-West t {sig.get('t')}")
            add(m, "return", f"{MARKET[m]}: annualised excess return with cost {mt.get('annual_return_with_cost')}, "
                             f"baseline {(e.get('baseline') or {}).get('annual_return_with_cost')}")
        d = diags.get(m)
        if not d:
            continue
        c, lq, fx, ex, s = d["cost"], d["liquidity"], d["fx"], d["exposure"], d.get("signal") or {}
        add(m, "cost", f"{MARKET[m]}: gross excess {c['gross_excess']}, cost drag {c['cost_drag']} per year "
                       f"(taxes {c['tax_share']:.0%} of cost), breakeven cost multiple {c['breakeven_cost_multiple']}")
        add(m, "turnover", f"{MARKET[m]}: annual turnover {c['turnover_annual']}, average holding {c['holding_days']} days")
        add(m, "liquidity", f"{MARKET[m]}: capacity about {lq['capacity_base']} {d['base_currency']}, "
                            f"weight in least liquid 20% of stocks {lq['low_liquidity_weight']}")
        if not fx.get("same_currency"):
            add(m, "fx", f"{MARKET[m]}: FX contribution {fx['fx_contribution']} per year in {fx['base']}")
        sector = f", max sector weight {ex['max_sector_weight']}" if ex.get("max_sector_weight") is not None else ", sector data unavailable"
        add(m, "exposure", f"{MARKET[m]}: beta {ex['beta']}, style z-scores {ex['style']}{sector}")
        if s:
            add(m, "neutral", f"{MARKET[m]}: rank IC after sector/style neutralisation {s.get('neutral_rank_ic')} "
                              f"(t {s.get('neutral_rank_ic_t')}), raw {s.get('rank_ic')}")
            add(m, "regime", f"{MARKET[m]}: rank IC high-vol {s['regime']['high_vol']}, low-vol {s['regime']['low_vol']}, "
                             f"up-market {s['regime']['up_market']}, down-market {s['regime']['down_market']}")
            add(m, "stability", f"{MARKET[m]}: rank IC by year {s.get('ic_by_year')}, by horizon {s.get('ic_by_horizon')}")
        add(m, "economic", f"{MARKET[m]}: economic significance — {d['economic']['label']}")
        if d.get("backtest_note"):
            add(m, "caveat", f"{MARKET[m]}: returns come from the original backtest before the tie-order fix; "
                             "they are sensitive to random tie-breaking and are not reliable evidence")
    for p in (study.get("cross_market") or {}).get("pairs", []):
        add(None, "cross_market", f"Correlation of monthly rank IC between {MARKET[p['a']]} and {MARKET[p['b']]}: {p['corr']} "
                                  f"over {study['cross_market']['months']} months")
    return out


def _notes(markets: list[str]) -> str:
    """团队审核过的市场介绍（configs/rdagent/context/<市场>.en.md），每个市场截取前 1500 个字符。"""
    out = []
    for m in markets:
        p = CONFIGS / "rdagent" / "context" / f"{m}.en.md"
        if p.exists():
            out.append(f"## {MARKET[m]}\n{p.read_text(encoding='utf-8')[:1500]}")
    return "\n".join(out)


def explain(study: dict, diags: dict) -> dict:
    fs = facts(study, diags)
    if not fs:
        raise draft.DraftError("还没有可解释的结果")
    cfg = draft.llm_config()
    prompt = PROMPT.format(hypothesis=study.get("hypothesis"), classification=json.dumps(
        {k: study["transfer"].get(k) for k in ("label", "reason")}, ensure_ascii=False),
        facts="\n".join(f"[{f['id']}] {f['text']}" for f in fs), notes=_notes(list(study["markets"])))
    text, usage = draft._chat(prompt, cfg)
    raw = json.loads(text)
    ids = {f["id"] for f in fs}
    kept, dropped = [], 0
    for s in raw.get("sentences") or []:
        cites = [c for c in (s.get("cites") or []) if c in ids]
        body = re.sub(r"\s*\[F\d+[^\]]*\]", "", str(s.get("text") or "")).strip()     # 引用统一显示在句末
        if cites and body:
            kept.append({"text": body, "cites": cites})
        else:
            dropped += 1
    return {"sentences": kept, "dropped": dropped, "transfer_view": str(raw.get("transfer_view") or ""),
            "local_adjustments": [str(x) for x in raw.get("local_adjustments") or []][:6], "facts": fs,
            "model": cfg["model"], "prompt_version": PROMPT_VERSION, "usage": usage,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "AI 解释，供研究员参考与确认；不改变平台的判定"}
