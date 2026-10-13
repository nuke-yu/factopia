"""AI 建议的方向选择（Spec 第 17.11 节，v2）：按平台证据给研究方向排序，而不是随机抽一个角度。

每个方向（研究角度）按关键词粗略归类以往的研究、RD-Agent 各轮假设、线索和人工排除方向，再结合：
- 研究缺口：以往研究没有涉及的方向、还没有获批信号的市场优先；
- 测试期前的证据：Alpha158 在测试期开始前（2015 至测试期前）显著的因子属于该方向的分组；
- 以往结论：有被采纳的轮次 → 值得延伸；多轮全部未采纳 → 降低优先级；被研究列为排除方向 → 降低；
- 需要公告文本的方向降低：训练期没有真实公告；
- 最近刚建议过的方向降低，保证"换一个"每次不同。
最后在得分最高的几个方向中按得分加权抽取一个：随机只用于在同样有依据的方向之间轮换，不决定研究什么。

防泄漏（MINE-11）：以往运行只用假设方向与是否采纳（与证据包相同，第 16.6 节），不用测试期的数值；
因子统计只用测试期开始前的数据并注明计算区间。情报台的公告文本发现不提供：法国真实公告从 2024-08 起，
全部落在 AI 不可见的时间段内。
"""
import random
import re

ANGLES = [
    {"zh": "短期反转与流动性提供", "kw": r"反转|reversal|liquidity[- ]provi|流动性提供", "groups": ["动量 / 反转"]},
    {"zh": "波动率调整后的中期动量", "kw": r"动量|momentum|volatility[- ]adjusted|波动率调整", "groups": ["动量 / 反转", "趋势"]},
    {"zh": "成交量与价格背离", "kw": r"背离|diverg|量价|price[- ]volume corr", "groups": ["量价关系"]},
    {"zh": "换手率异常与关注度", "kw": r"换手|turnover|关注度|attention|abnormal volume|异常成交", "groups": ["成交量"]},
    {"zh": "成交量波动与交易活跃度变化", "kw": r"成交量波动|volume volatility|VSTD|volume std|交易活跃", "groups": ["成交量"]},
    {"zh": "收盘在当日区间中的位置（K 线形态）", "kw": r"收盘位置|close position|KSFT|candlestick|K 线|影线|shadow", "groups": ["K 线"]},
    {"zh": "低波动与特质波动异象", "kw": r"低波动|low[- ]vol|idiosyncratic vol|特质波动|IDIOVOL", "groups": ["波动"]},
    {"zh": "连续涨跌天数与过度反应", "kw": r"连续|streak|consecutive|过度反应|overreact|CNTP|CNTN|up days", "groups": ["涨跌统计"]},
    {"zh": "市场急跌后的恢复速度", "kw": r"急跌|crash|sell[- ]?off|恢复速度|rebound speed|recovery", "groups": ["涨跌统计", "价格位置"]},
    {"zh": "流动性冲击后的价格回归", "kw": r"流动性冲击|liquidity shock|illiquid|amihud|非流动性", "groups": ["成交量", "量价关系"]},
    {"zh": "开盘与收盘价格行为差异", "kw": r"开盘|overnight|隔夜|open[- ]to[- ]close|gap", "groups": ["K 线", "价格位置"]},
    {"zh": "相对基准指数的强弱", "kw": r"相对强弱|relative strength|benchmark[- ]relative|相对基准|beta", "groups": ["趋势"]},
    {"zh": "成交额规模与信号衰减", "kw": r"市值|size|成交额|dollar volume|large[- ]cap|small[- ]cap", "groups": []},
    {"zh": "交易成本与印花税对高换手信号的侵蚀", "kw": r"印花税|stamp duty|transaction (cost|tax)|交易成本|financial transaction tax", "groups": []},
    {"zh": "节假日与交易日历差异带来的错位", "kw": r"节假日|holiday|calendar|日历", "groups": []},
    {"zh": "汇率波动对本地股票收益的传导", "kw": r"汇率|\bFX\b|currency|exchange rate", "groups": []},
    {"zh": "跨市场领先滞后（同一发行人在不同市场挂牌）", "kw": r"领先滞后|lead[- ]lag|cross[- ]list|跨市场|双重上市|dual[- ]list", "groups": []},
    {"zh": "公告发布时点与信息消化", "kw": r"公告时点|announcement timing|公告发布|disclosure timing", "groups": [], "text": True},
    {"zh": "管理层风险措辞的变化", "kw": r"风险措辞|risk (tone|wording|language)|文本信号|text signal", "groups": [], "text": True},
]
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}
TOP_K = 4                  # 在得分最高的几个方向中抽取
RECENT_ANGLES = 6          # 最近几条建议用过的方向降低优先级


def _hit(kw: str, text: str) -> bool:
    return bool(re.search(kw, text or "", re.I))


def _short(text: str, n: int = 220) -> str:
    s = re.split(r"(?<=[.。;；])\s", (text or "").strip(), maxsplit=1)[0]
    return s if len(s) <= n else s[:n] + "…"


def gather(markets: list[str], horizon: int = 20) -> dict:
    """平台证据：以往研究（最新锁定版本）、RD-Agent 各轮（只有方向与是否采纳）、排除方向、获批信号、测试期前的因子统计、线索。"""
    from factopia import ideas as I
    from factopia.research import evidence, library, store
    studies = []
    for rid in store.research_ids():
        vs = store.versions(rid)
        if not vs:
            continue
        sp = store.load_version(rid, vs[-1])
        text = " ".join([sp.get("title_zh") or "", sp.get("hypothesis") or "", sp.get("hypothesis_zh") or "", *(sp.get("directions") or [])])
        studies.append({"id": rid, "title": sp.get("title_zh") or rid, "markets": sp.get("markets") or [], "text": text,
                        "exclusions": [str(x) for x in sp.get("exclusions") or []]})
    try:
        rounds = evidence.past_runs(markets, horizon)
    except Exception:
        rounds = []
    try:
        findings = evidence.alpha158_findings(markets, horizon)
    except Exception:
        findings = []
    try:
        approved = [f for f in library.approved() if f.get("approved_markets")]
    except Exception:
        approved = []
    try:
        alpha = [f for f in library.alpha158() if f.get("description")]
    except Exception:
        alpha = []
    ideas = I.all_ideas()
    return {"studies": studies, "rounds": rounds, "findings": findings, "approved": approved, "alpha": alpha, "ideas": ideas}


def coverage(angle: dict, ev: dict) -> dict:
    kw = angle["kw"]
    studies = [s for s in ev["studies"] if _hit(kw, s["text"])]
    rounds = [r for r in ev["rounds"] if _hit(kw, r["hypothesis"])]
    excl = [(s["title"], x) for s in ev["studies"] for x in s["exclusions"] if _hit(kw, x)]
    ideas = [i for i in ev["ideas"] if _hit(kw, f"{i['title']} {i.get('body') or ''}")]
    return {"studies": studies, "rounds": rounds, "exclusions": excl, "ideas": ideas}


def _support(angle: dict, ev: dict, market: str) -> list[dict]:
    """测试期开始前该市场显著、且属于该方向分组的 Alpha158 因子。"""
    f = next((x for x in ev["findings"] if x["market"] == market), None)
    return [r for r in (f or {}).get("factors", []) if r["group"] in angle["groups"]] if f else []


# 打分维度：页面打分表的列；每个方向在每个市场都逐项计分（不适用记 0 分并写明原因）
DIMENSIONS = [
    {"key": "gap", "label": "研究缺口", "rule": "以往研究没涉及 +2；涉及但没覆盖该市场 +1；已覆盖 0"},
    {"key": "pretest", "label": "测试期前显著因子", "rule": "该市场测试期前 |t|>2 的 Alpha158 因子属于该方向分组，每个 +1，最多 +2"},
    {"key": "adopted", "label": "以往采纳", "rule": "该市场有相关轮次被 RD-Agent 采纳 +1"},
    {"key": "rejected", "label": "以往全部未采纳", "rule": "该市场 ≥3 轮相关轮次全部未采纳 −2"},
    {"key": "no_signal", "label": "市场无获批信号", "rule": "该市场还没有获批信号 +0.5"},
    {"key": "excluded", "label": "排除方向", "rule": "有研究把相关方向列为排除方向 −3"},
    {"key": "text", "label": "需要公告文本", "rule": "训练期没有真实公告 −2"},
    {"key": "crowded", "label": "同类线索", "rule": "线索池已有 ≥2 条相关线索 −1"},
    {"key": "recent", "label": "最近建议过", "rule": "最近 6 条建议用过该方向 −3"},
]


def score(angle: dict, ev: dict, market: str, recent: list[str]) -> tuple[float, list[str], dict, dict]:
    """返回 (总分, 加减分理由, 覆盖情况, 逐维度明细 {key: {"points", "note"}})。"""
    cov = coverage(angle, ev)
    mz = MARKET_ZH[market]
    d = {}
    n_st = len([x for x in cov["studies"] if market in x["markets"]])
    if not cov["studies"]:
        d["gap"] = (2, "以往研究还没有涉及这个方向")
    elif n_st == 0:
        d["gap"] = (1, f"以往有 {len(cov['studies'])} 个研究涉及，但都没有覆盖{mz}")
    else:
        d["gap"] = (0, f"以往已有 {n_st} 个研究在{mz}涉及这个方向")
    sup = _support(angle, ev, market)
    f = next((x for x in ev["findings"] if x["market"] == market), None)
    if sup:
        d["pretest"] = (min(len(sup), 2), f"测试期前（{f['period']['start'][:4]}–{f['period']['end'][:4]}）{mz}有 {len(sup)} 个显著因子属于该方向："
                        + "、".join(f"{r['name']}（t={r['t']:+.1f}）" for r in sup[:3]))
    elif not angle["groups"]:
        d["pretest"] = (0, "该方向没有对应的 Alpha158 分组，无法用测试期前的因子统计支持")
    else:
        d["pretest"] = (0, f"{mz}测试期前没有属于该方向分组（{'、'.join(angle['groups'])}）的显著因子")
    rm = [r for r in cov["rounds"] if r["market"] == market]
    a, r_ = sum(r["adopted"] for r in rm), sum(not r["adopted"] for r in rm)
    d["adopted"] = (1, f"{mz}以往 {a} 轮相关假设被采纳，可以延伸") if a else (0, f"{mz}以往没有相关轮次被采纳（相关 {len(rm)} 轮）")
    d["rejected"] = (-2, f"{mz}以往 {r_} 轮相关假设全部未被采纳") if not a and r_ >= 3 else (0, f"{mz}相关未采纳 {r_} 轮" + ("（有采纳轮次，不扣分）" if a else "（不足 3 轮，不扣分）" if r_ else ""))
    has_sig = [x["name"] for x in ev["approved"] if market in (x.get("approved_markets") or [])]
    d["no_signal"] = (0, f"{mz}已有 {len(has_sig)} 个获批信号") if has_sig else (0.5, f"{mz}还没有获批信号")
    d["excluded"] = (-3, f"有研究把相关方向列为排除方向（{len(cov['exclusions'])} 条）") if cov["exclusions"] else (0, "没有研究把它列为排除方向")
    d["text"] = ((-2, "需要公告文本：训练期没有真实公告（法国真实公告从 2024-08 起，香港 / 英国为模拟数据），只能走文本信号研究")
                 if angle.get("text") else (0, "只用量价 / 指数 / 汇率数据"))
    d["crowded"] = (-1, f"线索池已有 {len(cov['ideas'])} 条相关线索") if len(cov["ideas"]) >= 2 else (0, f"线索池相关线索 {len(cov['ideas'])} 条")
    d["recent"] = (-3, "最近 6 条建议里用过这个方向") if angle["zh"] in recent else (0, "最近 6 条建议没有用过")
    detail = {k: {"points": v[0], "note": v[1]} for k, v in d.items()}
    total = sum(v[0] for v in d.values())
    why = [v[1] for v in d.values() if v[0]]
    return total, why, cov, detail


def choose(markets: list[str], recent_angles: list[str], ev: dict, exclude: str | None = None, draw: bool = True) -> dict:
    """全部方向的打分表（每个方向取得分最高的市场作为侧重，并保留各市场的总分），以及选中的方向。
    draw=False（研究员给了想法）时只出打分表，不选方向。"""
    recent = recent_angles[-RECENT_ANGLES:]
    ranked = []
    for a in ANGLES:
        per = {m: score(a, ev, m, recent) for m in markets}
        m = max(per, key=lambda k: per[k][0])
        total, why, cov, detail = per[m]
        ranked.append({"angle": a, "score": total, "why": why, "coverage": cov, "market": m, "detail": detail,
                       "by_market": {k: v[0] for k, v in per.items()}, "skipped": a["zh"] == exclude,
                       "counts": {"studies": len(cov["studies"]), "rounds": len(cov["rounds"]),
                                  "adopted": sum(r["adopted"] for r in cov["rounds"]), "exclusions": len(cov["exclusions"]), "ideas": len(cov["ideas"])}})
    ranked.sort(key=lambda x: -x["score"])
    eligible = [x for x in ranked if not x["skipped"]]
    top = eligible[:TOP_K]
    lo = min(x["score"] for x in top)
    weights = [x["score"] - lo + 1 for x in top]
    pick = random.choices(top, weights=weights)[0] if draw else None
    tw = sum(weights)
    table = [{"angle": x["angle"]["zh"], "market": x["market"], "score": x["score"], "by_market": x["by_market"], "detail": x["detail"],
              "counts": x["counts"], "groups": x["angle"]["groups"], "keywords": x["angle"]["kw"], "skipped": x["skipped"],
              "top": x in top, "chance": round(weights[top.index(x)] / tw, 3) if x in top and draw else None,
              "selected": pick is x} for x in ranked]
    out = {"table": table, "candidates": [{"angle": x["angle"]["zh"], "market": x["market"], "score": round(x["score"], 1)} for x in top]}
    return {**pick, **out} if pick else out


def evidence_items(angle: dict | None, cov: dict | None, market: str | None, markets: list[str], ev: dict, seed: str = "") -> list[dict]:
    """发给 AI 的证据条目（[E#]）：每条带类型与中文说明，AI 引用后在页面显示。"""
    items = []
    focus = [market] if market else markets
    for f in ev["findings"]:
        if f["market"] not in focus:
            continue
        rows = [r for r in f["factors"] if not angle or r["group"] in angle["groups"]] or f["factors"]
        if rows:
            items.append({"kind": "测试期前统计", "text": f"pre-test Alpha158 finding ({f['market']}, daily rank IC with the {20}-day forward return, "
                          f"{f['period']['start']}–{f['period']['end']}, before the test period): "
                          + "; ".join(f"{r['name']} ({r['desc']}) t={r['t']:+.2f}" for r in rows[:5]),
                          "zh": f"{MARKET_ZH[f['market']]}测试期前（{f['period']['start']} 至 {f['period']['end']}）显著因子："
                                + "、".join(f"{r['name']} t={r['t']:+.1f}" for r in rows[:5])})
        elif not f["factors"]:
            items.append({"kind": "测试期前统计", "text": f"pre-test Alpha158 finding ({f['market']}): no factor significant (|t|>2) before the test period",
                          "zh": f"{MARKET_ZH[f['market']]}测试期前没有显著（|t|>2）的 Alpha158 因子"})
    rounds = (cov or {}).get("rounds") or []
    if seed:
        rounds = [r for r in ev["rounds"] if any(w.lower() in r["hypothesis"].lower() for w in re.findall(r"[A-Za-z]{5,}", seed))][:6] or rounds
    seen = set()
    for r in sorted(rounds, key=lambda r: (r["market"] not in focus, not r["adopted"])):
        h = _short(r["hypothesis"])
        if h in seen:
            continue
        seen.add(h)
        items.append({"kind": "以往轮次", "text": f"past RD-Agent round ({r['market']}, {'adopted' if r['adopted'] else 'not adopted'}; results not shown): {h}",
                      "zh": f"{MARKET_ZH[r['market']]}以往一轮（{'被采纳' if r['adopted'] else '未采纳'}）：{h}"})
        if len(seen) >= 6:
            break
    for title, x in ((cov or {}).get("exclusions") or [])[:4]:
        items.append({"kind": "排除方向", "text": f"direction excluded by an earlier study ({title}): {x}", "zh": f"研究「{title}」的排除方向：{x}"})
    for f in ev["approved"][:5]:
        items.append({"kind": "获批信号", "text": f"approved internal signal ({', '.join(f.get('approved_markets') or [])}): {f['name']} — {f.get('description') or ''}",
                      "zh": f"获批信号 {f['name']}（{'、'.join(MARKET_ZH.get(m, m) for m in f.get('approved_markets') or [])}）"})
    pool = [f for f in ev["alpha"] if angle and _family_group(f["name"]) in angle["groups"]] or ev["alpha"]
    for f in random.sample(pool, min(6, len(pool))):
        items.append({"kind": "因子库", "text": f"factor library (public baseline): {f['name']} — {f['description']}", "zh": f"因子库 {f['name']}：{f['description']}"})
    for i in [i for i in ev["ideas"] if i["channel"] in ("monitor", "portfolio", "approval")][:6]:
        items.append({"kind": "平台回流", "text": f"recent platform signal ({i['channel_zh']}): {i['title']}", "zh": f"{i['channel_zh']}：{i['title']}"})
    for k, it in enumerate(items, 1):
        it["id"] = f"E{k}"
    return items


def _family_group(name: str) -> str:
    from factopia.factors.families import family
    return family(name)[0]


def inputs_summary(ev: dict, markets: list[str], log: list[dict], avoid_titles: list[str], session_avoid: list[str]) -> dict:
    """页面「用了哪些数据」：每个数据维度的来源、数量、用途；以及明确不用的数据和原因。"""
    rounds = ev["rounds"]
    used = [
        {"name": "以往研究", "source": "runtime/research（各研究最新锁定版本）", "count": len(ev["studies"]),
         "use": "按关键词归类到方向，计「研究缺口」；研究题目放进避开清单", "items": [s["title"] for s in ev["studies"]]},
        {"name": "RD-Agent 以往各轮", "source": "runtime/results/runs（受泄漏影响的运行和文本研究除外）", "count": len(rounds),
         "use": f"只用假设方向与是否采纳（采纳 {sum(r['adopted'] for r in rounds)} 轮）；计「以往采纳 / 全部未采纳」，相关轮次作为证据发给 AI", "items": []},
        {"name": "排除方向", "source": "各研究规格的 exclusions 字段（研究员确认）", "count": sum(len(s["exclusions"]) for s in ev["studies"]),
         "use": "命中的方向 −3，并作为证据发给 AI", "items": [f"{s['title']}：{x}" for s in ev["studies"] for x in s["exclusions"]]},
        {"name": "测试期前因子统计", "source": "Alpha158 逐日 rank IC，按各市场测试期开始日截断重新计算", "count": sum(f["significant"] for f in ev["findings"]),
         "use": "该市场显著因子属于方向的分组时加分，并作为证据发给 AI",
         "items": [f"{MARKET_ZH[f['market']]}（{f['period']['start']} 至 {f['period']['end']}）：{f['significant']} 个显著"
                   + (("：" + "、".join(f"{r['name']} t={r['t']:+.1f}" for r in f["factors"])) if f["factors"] else "") for f in ev["findings"]]},
        {"name": "获批信号", "source": "因子库中已获批的市场版本", "count": len(ev["approved"]),
         "use": "该市场没有获批信号时 +0.5；名称与说明作为证据发给 AI",
         "items": [f"{x['name']}（{'、'.join(MARKET_ZH.get(m, m) for m in x.get('approved_markets') or [])}）" for x in ev["approved"]]},
        {"name": "因子库（Alpha158）", "source": "公开基准因子库的名称与说明", "count": len(ev["alpha"]),
         "use": "从选中方向的分组里抽 6 个作为证据（只有名称与说明）", "items": []},
        {"name": "线索池", "source": "runtime/ideas", "count": len(ev["ideas"]),
         "use": "按关键词计「同类线索」；回流线索（监控 / 组合 / 审批）标题作为证据；全部标题放进避开清单",
         "items": [f"{i['channel_zh']}：{i['title']}" for i in ev["ideas"]]},
        {"name": "最近的 AI 建议", "source": "runtime/ideas/suggestions.jsonl", "count": len(log),
         "use": f"最近 {RECENT_ANGLES} 条用过的方向 −3；标题放进避开清单", "items": [f"{r.get('angle') or ''}：{r['title']}" for r in log[-RECENT_ANGLES:]]},
        {"name": "数据与市场规则", "source": "平台固定说明", "count": len(markets),
         "use": "告诉 AI 能用和不能用的数据、三个市场的成本与交易规则", "items": []},
    ]
    not_used = [
        {"name": "测试期（2022 年起）的回测数值", "why": "防泄漏（MINE-11）：AI 只能看到以往轮次的方向与是否采纳"},
        {"name": "2024 年以后的数据与可信度检查结果", "why": "这是 AI 未见过的检验区间，用于判断结论是否成立"},
        {"name": "情报台的公告文本发现", "why": "法国真实公告从 2024-08 起，全部落在 AI 不可见的时间段内；香港 / 英国为模拟数据"},
        {"name": "监控预警与组合归因的具体数值", "why": "来自上线后的表现（2024 年以后），只提供线索标题"},
        {"name": "期货、期权、美股、日内、基本面、市值、分析师、指数成分变更", "why": "平台没有这些数据"},
    ]
    return {"used": used, "not_used": not_used, "avoid_n": len(avoid_titles), "session_avoid_n": len(session_avoid)}
