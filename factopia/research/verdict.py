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
NAMES = {"hk": r"Hong Kong|\bHSI\b|Hang Seng", "uk": r"\bUK\b|United Kingdom|\bFTSE\b|London", "fr": r"\bFrance\b|\bFrench\b|\bCAC\b|Paris"}
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


def check_round(metrics: dict | None, baseline: dict | None, rule: dict, primary: bool) -> list[dict]:
    """一轮的各项检查；ok 为 None 表示缺少数据、无法判定。"""
    m, b = metrics or {}, baseline or {}
    ric = m.get("rank_ic")
    items = [{"key": "rank_ic", "label": f"rank IC {_sign_zh(rule['ric_sign'])}", "value": ric, "ref": None,
              "ok": None if ric is None else ric * rule["ric_sign"] > 0}]
    if primary:
        r, br = m.get("annual_return_with_cost"), b.get("annual_return_with_cost")
        d, bd = m.get("max_drawdown_with_cost"), b.get("max_drawdown_with_cost")
        items.append({"key": "return", "label": "扣成本年化超额高于起点", "value": r, "ref": br,
                      "ok": None if r is None or br is None else r > br})
        items.append({"key": "drawdown", "label": "最大回撤不比起点差", "value": d, "ref": bd,
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
        checks = check_round(r.get("metrics"), base, rule, primary) if r.get("metrics") else []
        out.append({"loop": r["loop"], "checks": checks, "passed": passed(checks)})
    return out


def focus_round(rounds: list[dict]) -> dict | None:
    """RD-Agent 最终采纳的轮次（最后一个被采纳的）；没有采纳则为空。"""
    adopted = [r for r in rounds if r.get("decision") and r.get("metrics")]
    return adopted[-1] if adopted else None


def overall(markets: dict, rule: dict) -> dict:
    """研究结论：主市场未完成 → 进行中；主市场没有采纳轮次或采纳轮次未通过 → 否定；否则通过。"""
    p = markets.get(rule["primary"])
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


def credibility(validation: dict | None, sig: dict | None, rule: dict) -> dict:
    """采纳轮次的可信度：显著性（测试期）、未见数据、安慰剂、复现；回测随机性作为说明，不计入通过与否。

    validation 为 worker 的输出（可能为空或尚未完成）；sig 为测试期逐日 rank IC 的显著性（导出的 signal 文件）。
    """
    s = rule["ric_sign"]
    items = []
    t = (sig or {}).get("t")
    items.append({"key": "significance", "label": "测试期 rank IC 显著（|t| > 2 且方向一致）",
                  "value": t, "ok": None if t is None else (t * s > 2)})
    v = validation if validation and validation.get("status") in ("placebo", "done") else None
    if v:
        ai, bl = v["ai"]["holdout"], v["baseline"]["holdout"]
        ric, r, br = ai.get("rank_ic"), ai.get("annual_return_with_cost"), bl.get("annual_return_with_cost")
        items.append({"key": "holdout", "label": "2024 年以后（AI 未见过）rank IC 方向一致且收益高于起点",
                      "value": {"rank_ic": ric, "rank_ic_t": ai.get("rank_ic_t"), "ai": r, "baseline": br,
                                "ai_range": ai.get("annual_return_with_cost_range"), "baseline_range": bl.get("annual_return_with_cost_range")},
                      "ok": None if ric is None or r is None or br is None else (ric * s > 0 and r > br)})
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
        rng = v["ai"]["test"].get("annual_return_with_cost_range")
        noise = {"range": rng, "seeds": v.get("seeds"), "reported": ((v.get("original") or {}).get("ai") or {}).get("annual_return_with_cost")}
    done = validation is not None and validation.get("status") == "done"
    oks = [i["ok"] for i in items]
    state = ("unchecked" if not v else "pending" if not done or None in oks else "credible" if all(oks) else "doubtful")
    label = {"unchecked": "未检查", "pending": "检查中", "credible": "可信", "doubtful": "存疑"}[state]
    return {"state": state, "label": label, "checks": items, "noise": noise}
