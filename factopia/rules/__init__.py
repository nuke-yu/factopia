"""地区规则包与预检（业务需求 BR-19、20、21、39、45、47；Spec 第 12.1 节、第 14.12 节）。

- 规则包：configs/rules/<包>.yaml（common、hk、uk、fr），每个包有版本、负责人、生效日期、最后复核日期、状态；
  每条规则有编号、严重程度（block / warn / info）、检查时点（setup / portfolio / release）、检查方式、来源、所需审批人、影响。
  加载时校验格式（RULE-01）。成本与税费参数在市场包的 costs 中（RULE-04），回测诊断从这里读取。
- 选择：通用包 + 研究涉及的市场包（BR-21：按市场、客户授权、数据范围选择）。
- 预检：立项锁定前（setup：授权、市场、数据来源与许可）与组合生成后（portfolio：卖空、受限证券、集中度、成本、
  经济意义、时间点一致、可持续偏好）；release 为发布前复查（第 7 步审批时使用）。
- 输出：每条命中的规则编号与版本、市场、严重程度、原因、证据、影响、所需审批人；建议审批路径（按命中的规则加入
  数据负责人、各地区合规、模型风险）；存在阻断时流程停止（RULE-03）。
- 平台只负责发现、解释和路由，不作法律或合规结论；规则内容须由各地区合规确认。
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RULES_DIR = CONFIGS / "rules"
SEVERITIES = ("block", "warn", "info")
SEVERITY_ZH = {"block": "阻断", "warn": "警告", "info": "提示"}
STAGES = ("setup", "portfolio", "release")
STAGE_ZH = {"setup": "立项锁定前", "portfolio": "组合生成后", "release": "发布前"}
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国", "all": "通用"}
PACK_FIELDS = ("pack", "market", "version", "owner", "effective_date", "last_review", "status", "rules")
RULE_FIELDS = ("id", "title", "severity", "stages", "check", "source", "approvers", "impact")
CATEGORY_ZH = {"price_volume": "行情", "benchmark": "基准指数", "fx": "汇率", "text": "文本", "fundamentals": "财务",
               "alternative": "另类数据", "other": "其他", "always": "全部研究", "extra": "追加来源"}
STATUS_ZH = {"approved": "已批准", "limited": "有限制", "forbidden": "禁止", "unregistered": "未登记"}
DISCLAIMER = "平台只负责发现、解释和路由，不作法律或合规结论；规则内容待各地区合规确认。"
ROLE_ORDER = ["发起研究员", "本地研究员（香港）", "本地研究员（英国）", "本地研究员（法国）", "数据负责人",
              "香港合规", "英国合规", "法国合规", "模型风险", "投资组合经理", "运营 / 技术负责人"]


class RuleError(ValueError):
    pass


# ---------------------------------------------------------------- 规则包

def load_packs() -> dict[str, dict]:
    packs = {}
    for p in sorted(RULES_DIR.glob("*.yaml")):
        if p.stem == "data_sources":
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        missing = [f for f in PACK_FIELDS if f not in d]
        if missing:
            raise RuleError(f"{p.name} 缺少字段：{missing}")
        seen = set()
        for r in d["rules"]:
            miss = [f for f in RULE_FIELDS if f not in r]
            if miss:
                raise RuleError(f"{p.name} 规则 {r.get('id')} 缺少字段：{miss}")
            if r["severity"] not in SEVERITIES or any(s not in STAGES for s in r["stages"]):
                raise RuleError(f"{p.name} 规则 {r['id']} 的严重程度或检查时点无效")
            if r["id"] in seen:
                raise RuleError(f"{p.name} 规则编号重复：{r['id']}")
            seen.add(r["id"])
        d["file"] = p.name
        packs[d["pack"]] = d
    return packs


def load_sources() -> dict:
    return yaml.safe_load((RULES_DIR / "data_sources.yaml").read_text(encoding="utf-8"))


def costs(market: str) -> dict:
    """单边成本（RULE-04）：{"buy": {"tax", "commission"}, "sell": {...}}。"""
    c = load_packs()[market]["costs"]
    return {side: {"tax": float(c[side]["tax"]), "commission": float(c[side]["commission"]),
                   "total": float(c[side]["tax"]) + float(c[side]["commission"])} for side in ("buy", "sell")}


def select(markets: list[str]) -> list[dict]:
    packs = load_packs()
    return [packs["common"]] + [packs[m] for m in markets if m in packs]


# ---------------------------------------------------------------- 检查方式

def _sources_used(ctx: dict) -> list[tuple[str, str, str]]:
    """(市场, 来源, 数据类别)：研究规格的数据需求 + 每个研究都会用到的来源。"""
    reg = ctx["sources"]
    out = []
    for cat in sorted(ctx["data_categories"]):
        for m in ctx["markets"]:
            sid = (reg["category_sources"].get(cat) or {}).get(m)
            if sid:
                out.append((m, sid, cat))
    for sid in reg["always"]:
        out.append(("all", sid, "always"))
    for m, sid in ctx.get("extra_sources", []):
        out.append((m, sid, "extra"))
    return out


def _hit(market: str, reason: str, evidence: dict | None = None) -> dict:
    return {"market": market, "reason": reason, "evidence": evidence or {}}


def check_always(rule, ctx, market):
    return [_hit(market, "适用于本市场的固定提示")]


def check_source_status(rule, ctx, market):
    """同一来源合并为一条：列出用到它的市场与数据类别。"""
    reg = ctx["sources"]["sources"]
    used: dict[str, dict] = {}
    for m, sid, cat in _sources_used(ctx):
        if market != "all" and m not in (market, "all"):
            continue
        u = used.setdefault(sid, {"markets": [], "categories": []})
        u["markets"].append(m)
        u["categories"].append(cat)
    hits = []
    for sid, u in used.items():
        s = reg.get(sid)
        status = s["status"] if s else "unregistered"
        if status not in rule["check"]["status"]:
            continue
        ms = sorted(set(u["markets"]) - {"all"}) or ["all"]
        cats = "、".join(dict.fromkeys(CATEGORY_ZH.get(c, c) for c in u["categories"]))
        hits.append(_hit(",".join(ms), f"{s['name'] if s else sid}（{cats}）：{STATUS_ZH[status]}；{(s or {}).get('licence', '未在数据来源登记表中')}"
                         + (f"；{s['note']}" if s and s.get("note") else ""),
                         {"source": sid, "status": status, "categories": sorted(set(u["categories"]))}))
    return hits


def check_source_used(rule, ctx, market):
    sid = rule["check"]["source"]
    ms = sorted({m for m, s, _ in _sources_used(ctx) if s == sid})
    return [_hit(",".join(ms), f"使用了 {ctx['sources']['sources'].get(sid, {}).get('name', sid)}（{'、'.join(MARKET_ZH.get(m, m) for m in ms)}）",
                 {"source": sid, "markets": ms})] if ms else []


def check_data_visibility(rule, ctx, market):
    """授权的数据可见范围（BR-01、02）：用到的来源的可见级别须在授权允许的级别内；条款无法对应时提示人工确认。"""
    vis = ctx["sources"].get("visibility") or {}
    term = (ctx["mandate"].get("data_visibility") or "").strip()
    if not term:
        return []
    allowed = (vis.get("mandate_terms") or {}).get(term)
    if allowed is None:
        return [_hit("all", f"授权条款\"{term}\"没有对应的可见级别，须数据负责人确认", {"term": term})]
    levels = vis.get("levels") or {}
    reg = ctx["sources"]["sources"]
    bad: dict[str, set] = {}
    for m, sid, _ in _sources_used(ctx):
        lv = (reg.get(sid) or {}).get("visibility")
        if lv and lv not in allowed and (market == "all" or m in (market, "all")):
            bad.setdefault(sid, set()).add(m)
    return [_hit(",".join(sorted(ms)), f"{reg[sid]['name']}属于{levels.get(reg[sid]['visibility'], reg[sid]['visibility'])}，"
                 f"授权只允许{'、'.join(levels.get(x, x) for x in allowed)}（条款：{term}）",
                 {"source": sid, "visibility": reg[sid]["visibility"], "allowed": allowed, "term": term}) for sid, ms in bad.items()]


def check_data_gap(rule, ctx, market):
    reg = ctx["sources"]["category_sources"]
    gaps = [c for c in sorted(ctx["data_categories"]) if c not in reg]
    return [_hit("all", "研究需要但尚未接入的数据：" + "、".join(CATEGORY_ZH.get(c, c) for c in gaps), {"categories": gaps})] if gaps else []


def check_data_category(rule, ctx, market):
    cats = rule["check"]["category"]
    cats = [cats] if isinstance(cats, str) else cats
    used = [c for c in cats if c in ctx["data_categories"]]
    return [_hit(market, "研究使用了" + "、".join(CATEGORY_ZH.get(c, c) for c in used) + "数据", {"categories": used})] if used else []


def check_cross_border(rule, ctx, market):
    home = rule["check"]["home"]
    region = ctx["mandate"].get("service_region") or ""
    if MARKET_ZH[home] not in region:
        return []
    foreign = [m for m in ctx["markets"] if m != home]
    reasons = []
    if foreign:
        reasons.append(f"服务地区为{MARKET_ZH[home]}，使用{'、'.join(MARKET_ZH[m] for m in foreign)}的数据")
    llm = ctx["sources"]["sources"].get("llm_provider")
    if llm:
        reasons.append("研究文本发送至外部大模型服务")
    return [_hit(home, "；".join(reasons), {"foreign_markets": foreign})] if reasons else []


def check_short_allowed(rule, ctx, market):
    return [] if ctx["mandate"].get("long_only", True) else [_hit(market, "客户授权允许卖空")]


def check_mandate_sustainability(rule, ctx, market):
    s = ctx["mandate"].get("sustainability") or ""
    return [_hit(market, f"授权条款：{s}", {"sustainability": s})] if s and "无" not in s else []


def check_restricted_in_universe(rule, ctx, market):
    rs = set(ctx["mandate"].get("restricted_securities") or [])
    hits = []
    for m in ctx["markets"]:
        found = sorted(rs & set(ctx["universe"].get(m, [])))
        if found:
            hits.append(_hit(m, f"股票池中有 {len(found)} 只受限证券：{'、'.join(found[:10])}", {"securities": found}))
    return hits


def _positions(ctx, m):
    return (ctx.get("portfolio") or {}).get(m, {}).get("positions")


def check_restricted_holdings(rule, ctx, market):
    rs = set(ctx["mandate"].get("restricted_securities") or [])
    hits = []
    for m in ctx["markets"]:
        pos = _positions(ctx, m)
        if pos is None or not rs:
            continue
        held = pos[pos["weight"] != 0]
        found = sorted(set(held["symbol"]) & rs)
        if found:
            days = held[held["symbol"].isin(found)]["date"].nunique()
            hits.append(_hit(m, f"组合在 {days} 个交易日持有受限证券：{'、'.join(found)}", {"securities": found, "days": int(days)}))
    return hits


def check_short_positions(rule, ctx, market):
    if not ctx["mandate"].get("long_only", True):
        return []
    hits = []
    for m in ctx["markets"]:
        pos = _positions(ctx, m)
        if pos is None:
            continue
        neg = pos[pos["weight"] < 0]
        if len(neg):
            hits.append(_hit(m, f"授权只做多，组合在 {neg['date'].nunique()} 个交易日出现卖空（最小权重 {neg['weight'].min():.1%}）",
                             {"days": int(neg["date"].nunique()), "examples": sorted(neg["symbol"].unique())[:5]}))
    return hits


def check_max_weight(rule, ctx, market):
    lim = rule["check"]["limit"]
    hits = []
    for m in ctx["markets"]:
        pos = _positions(ctx, m)
        if pos is None or pos.empty:
            continue
        top = pos.loc[pos["weight"].idxmax()]
        if top["weight"] > lim + 1e-9:
            share = (pos.groupby("date")["weight"].max() > lim + 1e-9).mean()
            hits.append(_hit(m, f"最大单只权重 {top['weight']:.1%}（{top['symbol']}，{str(top['date'])[:10]}），"
                                f"{share:.0%} 的交易日超过上限 {lim:.0%}", {"max_weight": float(top["weight"]), "limit": lim}))
    return hits


def _diag(ctx, m):
    return (ctx.get("portfolio") or {}).get(m, {}).get("diagnostics")


def check_cost_sensitivity(rule, ctx, market):
    c = rule["check"]
    hits = []
    for m in ctx["markets"]:
        d = _diag(ctx, m)
        if not d:
            continue
        hd, be = d["cost"].get("holding_days"), d["cost"].get("breakeven_cost_multiple")
        why = []
        if hd is not None and hd < c["min_holding_days"]:
            why.append(f"平均持有 {hd:.0f} 天")
        if be is not None and be < c["min_breakeven"]:
            why.append(f"成本达到现在的 {be:.1f} 倍时超额归零")
        if why:
            hits.append(_hit(m, "；".join(why) + (f"；年化换手 {d['cost']['turnover_annual']:.1f} 倍" if d["cost"].get("turnover_annual") is not None else ""),
                             {"holding_days": hd, "breakeven": be}))
    return hits


def check_economic_fail(rule, ctx, market):
    hits = []
    for m in ctx["markets"]:
        d = _diag(ctx, m)
        if d and d["economic"]["reasons"]:
            hits.append(_hit(m, "；".join(d["economic"]["reasons"]), {"label": d["economic"]["label"]}))
    return hits


def check_pit_status(rule, ctx, market):
    pit = ctx.get("pit")
    if not pit or pit.get("point_in_time") or not ctx.get("portfolio"):
        return []
    return [_hit("all", "；".join(pit["reasons"]), {"label": pit["label"]})]


def check_excluded_industries(rule, ctx, market):
    text = " ".join(ctx["mandate"].get("free_text") or []) + (ctx["mandate"].get("sustainability") or "")
    wanted = {k: v for k, v in rule["check"]["keywords"].items() if k in text}
    if not wanted:
        return []
    hits = []
    for m in ctx["markets"]:
        pos = _positions(ctx, m)
        if pos is None:
            continue
        held = set(pos.loc[pos["weight"] > 0, "symbol"])
        for label, inds in wanted.items():
            found = sorted(s for s in held if (ctx["industries"].get(s) or "") in inds)
            if found:
                hits.append(_hit(m, f"授权要求回避{label}，持仓中有：{'、'.join(found)}（行业：{ctx['industries'][found[0]]}）",
                                 {"securities": found, "keyword": label}))
    return hits


CHECKS = {name[6:]: fn for name, fn in globals().items() if name.startswith("check_")}


# ---------------------------------------------------------------- 预检

def evaluate(ctx: dict, stages: list[str]) -> dict:
    ctx = {**ctx, "sources": ctx.get("sources") or load_sources()}
    hits = []
    packs = select(ctx["markets"])
    for pack in packs:
        for rule in pack["rules"]:
            run_stages = [s for s in rule["stages"] if s in stages]
            if not run_stages:
                continue
            fn = CHECKS.get(rule["check"]["type"])
            if fn is None:
                raise RuleError(f"规则 {rule['id']} 的检查方式未实现：{rule['check']['type']}")
            for h in fn(rule, ctx, pack["market"]):
                hits.append({**h, "rule_id": rule["id"], "title": rule["title"], "severity": rule["severity"],
                             "severity_zh": SEVERITY_ZH[rule["severity"]], "stage": run_stages[0],
                             "stage_zh": STAGE_ZH[run_stages[0]], "pack": pack["pack"], "pack_version": pack["version"],
                             "pack_status": pack["status"], "owner": pack["owner"], "source": rule["source"],
                             "impact": rule["impact"], "approvers": _expand(rule["approvers"], h["market"], ctx["markets"]),
                             "market_zh": "、".join(MARKET_ZH.get(x, x) for x in str(h["market"]).split(","))})
    hits.sort(key=lambda h: (SEVERITIES.index(h["severity"]), h["rule_id"]))
    counts = {s: sum(h["severity"] == s for h in hits) for s in SEVERITIES}
    per_market_info = {m: any(h["market"] in (m, "all") or m in str(h["market"]).split(",") for h in hits) for m in ctx["markets"]}
    return {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stages": stages,
            "stages_zh": [STAGE_ZH[s] for s in stages], "blocked": counts["block"] > 0, "counts": counts, "hits": hits,
            "packs": [{"pack": p["pack"], "version": p["version"], "status": p["status"], "owner": p["owner"],
                       "file": p["file"]} for p in packs],
            "sources_version": ctx["sources"]["version"], "every_market_hit": per_market_info,
            "approval_path": approval_path(hits, ctx["markets"]), "disclaimer": DISCLAIMER}


def _expand(roles: list[str], market: str, markets: list[str]) -> list[str]:
    """"地区合规"展开为命中市场的合规人员。"""
    out = []
    ms = [m for m in str(market).split(",") if m in MARKET_ZH and m != "all"] or markets
    for r in roles:
        if r == "地区合规":
            out += [f"{MARKET_ZH[m]}合规" for m in ms]
        else:
            out.append(r)
    return list(dict.fromkeys(out))


def approval_path(hits: list[dict], markets: list[str]) -> dict:
    """建议审批路径（BR-39）：基本顺序 + 警告 / 阻断规则要求的审批人；阻断时须先修改。"""
    required: dict[str, list[str]] = {"发起研究员": ["提交研究"]}
    for m in markets:
        required[f"本地研究员（{MARKET_ZH[m]}）"] = ["本地数据定义、语言理解、市场适用性"]
    for h in hits:
        if h["severity"] == "info":
            continue
        for r in h["approvers"]:
            required.setdefault(r, []).append(h["rule_id"])
    required.setdefault("投资组合经理", []).append("最终决定：批准、退回、限定地区或拒绝")
    required.setdefault("运营 / 技术负责人", []).append("核对获批版本、部署范围与监控阈值")
    steps = [{"role": r, "reasons": list(dict.fromkeys(required[r]))} for r in ROLE_ORDER if r in required]
    steps += [{"role": r, "reasons": v} for r, v in required.items() if r not in ROLE_ORDER]
    blocked = any(h["severity"] == "block" for h in hits)
    return {"steps": steps, "blocked": blocked,
            "note": "存在阻断规则：须修改后重新预检，才能进入审批" if blocked else
                    "警告规则须由列出的审批人确认签字；提示写入报告"}
