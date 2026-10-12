"""按锁定的可证伪测试判定运行结果（Spec RS-10、第 14.5 节结论 4）。

RD-Agent 的"是否采纳"由它的 AI 评审决定，主要看收益；研究规格的可证伪测试在运行前写死，二者可能不一致。
这里按可证伪测试对每一轮重新判定，与 RD-Agent 的采纳结果并列显示。

判定规则：可证伪测试目前是英文原文，平台使用固定的标准检查，只从原文提取主市场与预期的 rank IC 方向：
    主市场（原文最先提到的市场；没有提到则为规格中的第一个市场）：rank IC 方向与预期一致；扣成本年化超额高于起点；最大回撤不比起点差。
    其他市场（部分适用）：rank IC 方向与预期一致。
主市场的结论看 RD-Agent 最终采纳的轮次（这次研究"交出"的结果），不在各轮中挑最好的一轮，避免多次尝试挑结果。
原文中其他条件（如 t 值门槛、回撤容忍幅度）平台尚未检验，页面注明需人工确认。
"""
import re

SIGN = re.compile(r"\b(positive|negative)\s+rank\s+IC\b", re.I)
NAMES = {"hk": r"Hong Kong|\bHK\b|\bHSI\b|Hang Seng", "uk": r"\bUK\b|United Kingdom|\bFTSE\b|London", "fr": r"\bFrance\b|\bFrench\b|\bFR\b|\bCAC\b|Paris"}
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}


def _primary(text: str, markets: list[str]) -> str:
    found = [(m.start(), k) for k in markets if (m := re.search(NAMES.get(k, "$^"), text))]
    return min(found)[1] if found else markets[0]


def rules(spec: dict) -> dict:
    text = spec.get("falsification") or ""
    m = SIGN.search(text)
    markets = spec.get("markets") or [spec.get("market")]
    primary = _primary(text, markets)
    return {"primary": primary, "markets": [primary] + [m for m in markets if m != primary],
            "ric_sign": -1 if m and m.group(1).lower() == "negative" else 1,
            "sign_from_text": bool(m), "text": text}


def _sign_zh(s: int) -> str:
    return "为正" if s > 0 else "为负"


def check_round(metrics: dict | None, baseline: dict | None, rule: dict, primary: bool, kind: str = "rdagent") -> list[dict]:
    """一轮的各项检查；ok 为 None 表示缺少数据、无法判定。
    文本信号研究（kind = text）的起点是等权持有股票池，相对基准的超额回撤恒为 0，回撤检查改为比较绝对回撤（扣成本）。"""
    m, b = metrics or {}, baseline or {}
    ric = m.get("rank_ic")
    items = [{"key": "rank_ic", "label": f"rank IC {_sign_zh(rule['ric_sign'])}", "value": ric, "ref": None,
              "ok": None if ric is None else ric * rule["ric_sign"] > 0}]
    if primary:
        r, br = m.get("annual_return_with_cost"), b.get("annual_return_with_cost")
        dk = "max_drawdown_abs" if kind == "text" else "max_drawdown_with_cost"
        d, bd = m.get(dk), b.get(dk)
        items.append({"key": "return", "label": "扣成本年化超额高于起点", "value": r, "ref": br,
                      "ok": None if r is None or br is None else r > br})
        items.append({"key": "drawdown", "label": "最大回撤（绝对，扣成本）不比等权持有股票池差" if kind == "text" else "最大回撤不比起点差",
                      "value": d, "ref": bd,
                      "ok": None if d is None or bd is None else d >= bd})
    return items


def passed(checks: list[dict]) -> bool | None:
    if not checks or any(c["ok"] is None for c in checks):
        return None
    return all(c["ok"] for c in checks)


def judge_rounds(run: dict, rounds: list[dict], rule: dict) -> list[dict]:
    """每一轮的判定，顺序与 rounds 一致。"""
    primary = run["market"] == rule["primary"]
    base = (run.get("baseline") or {}).get("metrics")
    out = []
    for r in rounds:
        checks = check_round(r.get("metrics"), base, rule, primary, run.get("kind", "rdagent")) if r.get("metrics") else []
        out.append({"loop": r["loop"], "checks": checks, "passed": passed(checks)})
    return out


def focus_round(rounds: list[dict]) -> dict | None:
    """RD-Agent 最终采纳的轮次（最后一个被采纳的）；没有采纳则为空。"""
    adopted = [r for r in rounds if r.get("decision") and r.get("metrics")]
    return adopted[-1] if adopted else None


def overall(markets: dict, rule: dict) -> dict:
    """研究结论：主市场未完成 → 进行中；主市场没有采纳轮次或采纳轮次未通过 → 否定；否则通过。"""
    p = markets.get(rule["primary"])
    if p and p.get("insufficient"):
        return {"state": "insufficient", "label": "数据不足", "reason": p["insufficient"]}
    if not p or p["state"] != "done":
        return {"state": "pending", "label": "进行中", "reason": "主市场的运行尚未完成"}
    if p["focus"] is None:
        return {"state": "rejected", "label": "否定", "reason": "主市场没有被采纳的轮次：新因子没有超过起点"}
    failed = [c["label"] for c in p["focus"]["checks"] if c["ok"] is False]
    if failed:
        return {"state": "rejected", "label": "否定", "reason": "主市场采纳的轮次未满足：" + "、".join(failed)}
    if p["focus"]["passed"] is None:
        return {"state": "pending", "label": "无法判定", "reason": "主市场采纳的轮次缺少指标"}
    others = [(m, e) for m, e in markets.items() if m != rule["primary"]]
    same = [MARKET_ZH[m] for m, e in others if e["state"] == "done" and e["focus"] and e["focus"]["passed"]]
    pending = [MARKET_ZH[m] for m, e in others if e["state"] != "done"]
    parts = ["主市场采纳的轮次满足标准条件"]
    if others:
        parts.append(f"其他市场中 rank IC 方向一致的：{'、'.join(same) or '没有'}" + (f"（{'、'.join(pending)}尚未完成）" if pending else ""))
    parts.append("原文中的其他条件（如 t 值门槛）平台未检验，需人工确认")
    return {"state": "passed", "label": "主市场通过", "reason": "；".join(parts)}


# ── 可信度检查（factopia/validate，Spec 第 14.7 节）──────────────────────────

PLACEBO_MAX_SHARE = 0.10   # 随机因子达到同样收益的比例不超过 10%
REPRO_MIN_SHARE = 0.99     # 重新计算的因子值与 RD-Agent 当时的值一致的比例


def credibility(validation: dict | None, sig: dict | None, rule: dict, ties: dict | None = None) -> dict:
    """采纳轮次的可信度：显著性（测试期）、打分区分度、测试期与未见数据的多次回测成对比较、安慰剂、复现。

    validation 为 worker 的输出（可能为空或尚未完成）；sig 为测试期逐日 rank IC 的显著性（导出的 signal 文件）；
    ties 为测试期打分的并列情况（导出的 score 文件）。
    收益类判断不看单次回测：同一组并列排序种子下 AI 与起点各回测多次，要求 AI 的中位数高于起点且多数次数胜出
    （单次回测的收益在打分大量并列、信号较弱时主要取决于买到哪几只，见第 14.7 节"可复现与并列"）。
    """
    from factopia.validate import metrics as vm
    s = rule["ric_sign"]
    items = []
    t = (sig or {}).get("t")
    items.append({"key": "significance", "label": "测试期 rank IC 显著（|t| > 2 且方向一致）",
                  "value": t, "ok": None if t is None else (t * s > 2)})
    v = validation if validation and validation.get("status") in ("placebo", "done") else None
    tie = ((v or {}).get("ai", {}).get("test", {}) or {}).get("ties") or ties
    items.append({"key": "ties", "label": f"打分区分度：持股分数线上并列不超过 {vm.TIE_MAX} 只（中位数）",
                  "value": tie, "ok": None if not tie else bool(tie["ok"])})
    if v:
        tp = vm.paired(v["ai"]["test"].get("annual_return_with_cost_all"), v["baseline"]["test"].get("annual_return_with_cost_all"))
        items.append({"key": "robust_test", "label": "测试期多次回测：AI 扣成本收益中位数高于起点，且多数次数胜出",
                      "value": tp, "ok": None if not tp else tp["ok"]})
        ai, bl = v["ai"]["holdout"], v["baseline"]["holdout"]
        ric = ai.get("rank_ic")
        hp = vm.paired(ai.get("annual_return_with_cost_all"), bl.get("annual_return_with_cost_all"))
        r, br = ai.get("annual_return_with_cost"), bl.get("annual_return_with_cost")
        ok_ret = hp["ok"] if hp else (None if r is None or br is None else r > br)
        items.append({"key": "holdout", "label": "2024 年以后（AI 未见过）rank IC 方向一致，且多次回测中位数高于起点、多数次数胜出",
                      "value": {"rank_ic": ric, "rank_ic_t": ai.get("rank_ic_t"), "ai": r, "baseline": br, "paired": hp,
                                "ai_range": ai.get("annual_return_with_cost_range"), "baseline_range": bl.get("annual_return_with_cost_range")},
                      "ok": None if ric is None or ok_ret is None else (ric * s > 0 and ok_ret)})
        rep = [f["match_share"] for f in v.get("factors", [])]
        items.append({"key": "reproduce", "label": "因子可复现（重新计算的值与 RD-Agent 当时一致）",
                      "value": min(rep) if rep else None, "ok": None if not rep else min(rep) >= REPRO_MIN_SHARE})
    pl = (validation or {}).get("placebo") if validation and validation.get("status") == "done" else None
    if pl and pl.get("share_ge_ai") is not None:
        items.append({"key": "placebo", "label": f"随机因子达到同样收益的比例不超过 {PLACEBO_MAX_SHARE:.0%}",
                      "value": {"share": pl["share_ge_ai"], "n": pl["n"], "median": pl.get("median")},
                      "ok": pl["share_ge_ai"] <= PLACEBO_MAX_SHARE})
    noise = None
    if v:
        t = v["ai"]["test"]
        noise = {"range": t.get("annual_return_with_cost_range"), "median": t.get("annual_return_with_cost"),
                 "seeds": v.get("seeds"), "reported": ((v.get("original") or {}).get("ai") or {}).get("annual_return_with_cost"),
                 "deterministic": bool(v.get("deterministic"))}
    done = validation is not None and validation.get("status") == "done"
    oks = [i["ok"] for i in items]
    state = ("unchecked" if not v else "pending" if not done or None in oks else "credible" if all(oks) else "doubtful")
    label = {"unchecked": "未检查", "pending": "检查中", "credible": "可信", "doubtful": "存疑"}[state]
    return {"state": state, "label": label, "checks": items, "noise": noise}


TRANSFER_T = 2.0


def transfer(markets: dict, rule: dict, diagnostics: dict) -> dict:
    """四类迁移标签（BR-05）：按各市场采纳轮次的 Rank IC t 值（Newey-West，方向按假设）分为三市场通用 / 地区特定 / 需调参 / 无效；
    信号有效、但在有效市场中扣除成本、汇率或容量后不再有正超额 → 第四类"扣除币种、成本或市场结构影响后不再具有经济意义"。"""
    sign = rule["ric_sign"]
    per, ts = {}, {}
    short = {m: e["insufficient"] for m, e in markets.items() if e.get("insufficient")}   # 数据不足的市场不参与分类
    markets = {m: e for m, e in markets.items() if m not in short}
    if not markets:
        return {"label": "数据不足", "statistical": "数据不足", "reason": "；".join(short.values()), "markets": {}, "threshold": TRANSFER_T}
    for m, e in markets.items():
        sig = ((e.get("focus") or {}).get("significance") or {})
        t = sig.get("t")
        ts[m] = None if t is None else t * sign
        if e.get("state") == "done" and not e.get("focus"):     # 已完成但没有采纳的轮次：该市场视为无效
            ts[m] = 0.0
        eco = (diagnostics.get(m) or {}).get("economic")
        per[m] = {"t_aligned": ts[m], "effective": ts[m] is not None and ts[m] > TRANSFER_T,
                  "economic": eco}
    known = {m: t for m, t in ts.items() if t is not None}
    if len(known) < len(markets):
        return {"label": "待定", "reason": "部分市场尚无结果", "markets": per, "threshold": TRANSFER_T}
    eff = [m for m in known if known[m] > TRANSFER_T]
    if len(eff) == len(known):
        label = "三市场通用"
    elif eff:
        label = "地区特定（" + "、".join(m.upper() for m in eff) + "）"
    elif all(t > TRANSFER_T / 2 for t in known.values()):
        label = "需调整定义 / 阈值 / 参数后才能迁移"
    else:
        label = "无效"
    cand = eff or [m for m in known if known[m] > TRANSFER_T / 2]
    econ_fail = [m for m in cand
                 if per[m]["economic"] and not per[m]["economic"]["meaningful"] and per[m]["economic"]["reasons"]]
    if label != "无效" and econ_fail and econ_fail == cand:       # 有效的市场全部在扣除后失去经济意义
        reasons = sorted({r for m in econ_fail for r in per[m]["economic"]["reasons"]})
        return {"label": "扣除币种、成本或市场结构影响后不再具有经济意义", "statistical": label, "reason": "；".join(reasons),
                "markets": per, "threshold": TRANSFER_T}
    for m, why in short.items():
        per[m] = {"t_aligned": None, "effective": False, "economic": None, "insufficient": why}
    return {"label": label, "statistical": label, "reason": "；".join(f"{m.upper()} 数据不足，未参与分类" for m in short),
            "markets": per, "threshold": TRANSFER_T}
