"""OBSERVE：把一次研究版本的结果整理成带编号的事实包（Spec 第 18.4、18.11 节）。

原则（AGT-02）：
- 每条事实有编号 F#、来源类别与期间；
- 决策事实只用验证期及以前的数据；测试期 / holdout 只以状态和定性描述出现，**不含具体数值**；
- 完整数值留在各运行目录中，由页面按权限展示，不进入 Agent 的提示词。

P0 只读已导出的 run.json / rounds.json / diagnostics / validation / quantile，
不重跑回测、不调用大模型。
"""
import json
import os
import re
from pathlib import Path

from factopia.agents import store as astore
from factopia.research import store as rstore
from factopia.research import verdict

RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", str(astore.RUNTIME / "results")))

CHECK_LABELS = {
    "rank_ic": "rank IC direction",
    "return": "return above the baseline",
    "drawdown": "drawdown not worse than the baseline",
}
CRED_LABELS = {
    "significance": "statistical significance",
    "ties": "score discrimination / tie handling",
    "robust_test": "multiple-backtest robustness",
    "holdout": "holdout period (2024+)",
    "reproduce": "factor reproducibility",
    "placebo": "random-factor placebo",
}


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _runs(research_id: str, version: int) -> list[dict]:
    d = RESULTS / "runs"
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("*/run.json"), reverse=True):
        run = _read_json(path)
        if not isinstance(run, dict):
            continue
        r = run.get("research") or {}
        if r.get("id") == research_id and r.get("version") == version:
            if run.get("context_leak"):
                continue                         # 受数据泄漏影响的运行不能作为 Agent 决策证据（MINE-11）
            out.append(run)
    return out


def build(research_id: str, version: int) -> dict:
    """构建一轮事实包；失败时抛出研究规格不存在等异常，不静默造事实。"""
    spec = rstore.load_version(research_id, version)
    rule = verdict.rules(spec)
    markets = list(rule["markets"])
    runs = _runs(research_id, version)
    jobs = [j for j in rstore.list_jobs()
            if j.get("research_id") == research_id and j.get("version") == version
            and j.get("kind") not in ("monitor", "paper")]
    facts: list[dict] = []

    def add(key: str, text: str, market: str | None = None, period: str = "spec", redacted: bool = False) -> None:
        facts.append({"id": f"F{len(facts) + 1}", "key": key, "market": market,
                      "period": period, "text": " ".join(str(text).split()), "redacted": bool(redacted)})

    _spec_facts(add, spec, rule)
    market_state = {m: _market_facts(add, m, runs, jobs, rule) for m in markets}
    overall = _overall_facts(add, market_state, rule)
    _library_facts(add)
    _past_facts(add, markets, int(spec.get("horizon") or 20))
    _idea_facts(add)
    return {"research_id": research_id, "version": int(version), "markets": markets,
            "horizon": int(spec.get("horizon") or 20), "facts": facts,
            "summary": {"fact_count": len(facts), **overall, "built_at": astore.now()}}


def _spec_facts(add, spec: dict, rule: dict) -> None:
    add("spec.hypothesis", f"Research hypothesis: {spec.get('hypothesis') or spec.get('hypothesis_zh') or '-'}")
    add("spec.falsification", f"Pre-registered falsification test: {spec.get('falsification') or '-'}")
    add("spec.scope", f"Markets: {', '.join(rule['markets'])}; prediction horizon: "
                      f"{int(spec.get('horizon') or 20)} trading days; signal source: "
                      f"{spec.get('signal_source') or 'price_volume'}.")
    directions = [str(x) for x in spec.get("directions") or [] if str(x).strip()]
    if directions:
        add("spec.directions", "Candidate directions: " + "; ".join(directions))
    exclusions = [str(x) for x in spec.get("exclusions") or [] if str(x).strip()]
    if exclusions:
        add("spec.exclusions", "Directions already excluded by the researcher: " + "; ".join(exclusions))


def _market_facts(add, m: str, runs: list[dict], jobs: list[dict], rule: dict) -> dict:
    """一个市场的事实；返回内部状态供 verdict.overall / transfer 使用（不写入 facts.json）。"""
    label = m.upper()
    jobs_by_run = {j["run_id"]: j for j in jobs if j.get("run_id")}
    mine = [r for r in runs if r.get("market") == m]
    done = [r for r in mine
            if (jobs_by_run.get(r["run_id"]) or {}).get("state", "done" if r.get("status") == "done" else "") == "done"]
    run = (done or mine or [None])[0]
    if not run:
        add(f"{m}.state", f"{label}: no run for this version and horizon yet.", market=m, period="run state")
        return {"state": "none", "focus": None}
    job = jobs_by_run.get(run["run_id"])
    state = job["state"] if job else ("done" if run.get("status") == "done" else "incomplete")
    if state != "done":
        add(f"{m}.state", f"{label}: latest run state is {state}; no conclusion yet.", market=m, period="run state")
        return {"state": state, "focus": None}
    rounds = _read_json(RESULTS / "runs" / run["run_id"] / "rounds.json") or []
    if not isinstance(rounds, list):
        rounds = []
    judged = {v["loop"]: v for v in verdict.judge_rounds(run, rounds, rule)}
    focus = verdict.focus_round(rounds)
    entry = {"state": "done", "focus": None, "run": run, "diagnostics": None}

    if focus:
        checks = (judged.get(focus["loop"]) or {}).get("checks") or []
        passed = (judged.get(focus["loop"]) or {}).get("passed")
        failed = [CHECK_LABELS.get(c.get("key"), c.get("key")) for c in checks if c.get("ok") is False]
        add(f"{m}.focus",
            f"{label}: the latest completed run has {len(rounds)} rounds; RD-Agent adopted round {focus['loop']}. "
            f"The adopted round {'passed' if passed else 'failed'} the pre-registered falsification test"
            + (f" on: {', '.join(failed)}." if failed else "."),
            market=m, period="test (status only)", redacted=True)
        entry["focus"] = {**focus, "checks": checks, "passed": passed,
                          "significance": _signal_sig(run["run_id"], focus["loop"], int(run.get("horizon") or 20))}
        _baseline_fact(add, m, run, focus)
    else:
        add(f"{m}.focus", f"{label}: no round was adopted; the hypothesis was not supported.",
            market=m, period="test (status only)", redacted=True)

    if focus:
        cred = _credibility(run["run_id"], focus["loop"], rule)
        add(f"{m}.credibility",
            f"{label}: credibility check status is {cred['state']}"
            + (f"; failed checks: {', '.join(cred['failed'])}." if cred["failed"] else "."),
            market=m, period="test/holdout (status only)", redacted=True)
        diag = _read_json(RESULTS / "runs" / run["run_id"] / "diagnostics" / f"{focus['loop']}.json")
        entry["diagnostics"] = diag if isinstance(diag, dict) else None
        observations = _diagnostic_observations(entry["diagnostics"])
        if observations:
            add(f"{m}.diagnostics", f"{label}: " + "; ".join(observations) + ".",
                market=m, period="test (qualitative)", redacted=True)
        quantile = _read_json(RESULTS / "runs" / run["run_id"] / "quantile" / f"{focus['loop']}.json")
        qobs = _quantile_observations(quantile, rule["ric_sign"])
        if qobs:
            add(f"{m}.quantile", f"{label}: " + "; ".join(qobs) + ".",
                market=m, period="test (qualitative)", redacted=True)
    return entry


def _baseline_fact(add, m: str, run: dict, focus: dict) -> None:
    base = (run.get("baseline") or {}).get("metrics") or {}
    fm = focus.get("metrics") or {}
    br, fr = base.get("annual_return_with_cost"), fm.get("annual_return_with_cost")
    if br is None or fr is None:
        return
    rel = "above" if fr > br else "below" if fr < br else "equal to"
    add(f"{m}.baseline", f"{m.upper()}: the adopted round's return after costs is {rel} the RD-Agent baseline.",
        market=m, period="test (status only)", redacted=True)


def _credibility(run_id: str, loop, rule: dict) -> dict:
    val = _read_json(RESULTS / "runs" / run_id / "validation" / f"{loop}.json")
    if not isinstance(val, dict):
        return {"state": "unchecked", "failed": []}
    try:
        cred = verdict.credibility(val, None, rule, None)
    except Exception:
        return {"state": "unknown", "failed": []}
    failed = [CRED_LABELS.get(c.get("key"), c.get("key")) for c in cred.get("checks") or [] if c.get("ok") is False]
    return {"state": cred.get("state") or "unknown", "failed": failed}


def _signal_sig(run_id: str, loop, horizon: int) -> dict | None:
    path = RESULTS / "runs" / run_id / "signal" / f"{loop}.parquet"
    if not path.exists():
        return None
    try:
        import pandas as pd
        from factopia.validate import metrics as vm
        df = pd.read_parquet(path)
        return vm.significance(df.set_index("date")["rank_ic"], horizon)
    except Exception:
        return None


def _diagnostic_observations(diag: dict | None) -> list[str]:
    """把诊断数值转换成定性观察；不把测试期 / holdout 的具体数值写进事实。"""
    if not isinstance(diag, dict):
        return []
    out: list[str] = []
    c = diag.get("cost") or {}
    gross, net = c.get("gross_excess"), c.get("net_excess")
    if gross is not None and net is not None and net <= 0 < gross:
        out.append("transaction costs and taxes erase the gross excess")
    elif c.get("breakeven_cost_multiple") is not None and c["breakeven_cost_multiple"] < 2:
        out.append("the edge is fragile: a modest rise in costs would erase it")
    elif net is not None and net > 0:
        out.append("net excess remains positive after costs")
    if c.get("turnover_annual") is not None and c["turnover_annual"] > 4:
        out.append("annual turnover is high")
    if c.get("holding_days") is not None and c["holding_days"] < 30:
        out.append("the average holding period is short")

    lq = diag.get("liquidity") or {}
    if lq.get("capacity_base") is not None and lq["capacity_base"] < 50e6:
        out.append("capacity is below the platform floor")
    if lq.get("low_liquidity_weight") is not None and lq["low_liquidity_weight"] > 0.2:
        out.append("a large share of holdings is in the least liquid quintile")

    fx = diag.get("fx") or {}
    if not fx.get("same_currency") and fx.get("fx_contribution") is not None:
        out.append("currency moves materially affect the base-currency result")

    ex = diag.get("exposure") or {}
    if ex.get("max_sector_weight") is not None and ex["max_sector_weight"] > 0.5:
        out.append("sector concentration is high")

    sig = diag.get("signal") or {}
    retained = sig.get("neutral_retained")
    if retained is not None and retained < 0.5:
        out.append("most of the rank IC comes from style exposure")
    elif retained is not None:
        out.append("the signal survives style and sector neutralisation")
    if sig.get("year_sign_consistency") is not None and sig["year_sign_consistency"] < 0.6:
        out.append("the sign of the rank IC is unstable across years")
    baseline_corr = (sig.get("redundancy") or {}).get("baseline_model")
    if baseline_corr is not None:
        out.append("the signal is highly correlated with the baseline model" if baseline_corr > 0.7
                   else "the signal is not merely a copy of the baseline model")

    econ = diag.get("economic") or {}
    if econ.get("meaningful") is False:
        out.append("the platform classifies the signal as not economically meaningful after costs, FX or capacity")
    if diag.get("backtest_note"):
        out.append("the original backtest was affected by tie-order randomness, so its return is not reliable evidence")
    return out


def _quantile_observations(quantile: dict | None, sign: int) -> list[str]:
    if not isinstance(quantile, dict):
        return []
    out = []
    tie = quantile.get("tie_share")
    if tie is not None and tie > 0.2:
        out.append("many stocks receive identical scores each day; ranking depends on tie-breaking")
    spread = quantile.get("spread_mean")
    if spread is not None and spread * sign < 0:
        out.append("the top-scored group underperforms the bottom group")
    lead = quantile.get("spread_positive_share")
    if lead is not None and lead < 0.5:
        out.append("the top group does not consistently lead")
    return out


def _overall_facts(add, market_state: dict, rule: dict) -> dict:
    markets = {}
    diags = {}
    for m, e in market_state.items():
        focus = e.get("focus")
        markets[m] = {"state": e.get("state"),
                      "focus": {"checks": (focus or {}).get("checks") or [], "passed": (focus or {}).get("passed"),
                                "significance": (focus or {}).get("significance")} if focus else None,
                      "insufficient": (e.get("run") or {}).get("insufficient")}
        diags[m] = e.get("diagnostics")
    try:
        over = verdict.overall(markets, rule)
    except Exception:
        over = {"state": "unknown", "label": "无法判定"}
    add("overall", f"Platform verdict: {over.get('label')}.", period="test (status only)", redacted=True)
    if len(rule["markets"]) < 2:
        transfer = {"label": "不适用（单市场）", "statistical": None}
        add("transfer", "Transfer classification: not applicable (single-market study).", period="spec")
    else:
        try:
            transfer = verdict.transfer(markets, rule, diags)
        except Exception:
            transfer = {"label": "unknown", "statistical": None}
        add("transfer", f"Transfer classification: {transfer.get('label')}.", period="test (status only)", redacted=True)
    return {"overall": {"state": over.get("state"), "label": over.get("label")},
            "transfer": {"label": transfer.get("label"), "statistical": transfer.get("statistical")}}


def _library_facts(add) -> None:
    """因子库只给名称与说明；不提供测试期统计数值（MINE-11）。"""
    try:
        from factopia.research import library
        approved = library.approved()
    except Exception:
        approved = []
    if approved:
        names = [f"{x.get('name')} ({'/'.join(x.get('approved_markets') or [])})" for x in approved[:8]]
        add("library.approved", "Approved internal signals: " + "; ".join(names) + ".", period="library")
    try:
        from factopia.research import library
        prop = library.proprietary()
    except Exception:
        prop = []
    if prop:
        add("library.proprietary", "Team proprietary factor entries: "
            + "; ".join(f"{x.get('name')} — {x.get('description') or ''}" for x in prop[:8]) + ".", period="library")


def _past_facts(add, markets: list[str], horizon: int) -> None:
    """以往轮次只给假设方向与是否采纳（与 factopia.research.evidence 口径一致）。"""
    d = RESULTS / "runs"
    if not d.exists():
        return
    count = 0
    for path in sorted(d.glob("*/run.json"), reverse=True):
        run = _read_json(path)
        if not isinstance(run, dict) or run.get("market") not in markets or run.get("horizon") != horizon:
            continue
        if run.get("context_leak") or run.get("kind") == "text":
            continue
        rounds = _read_json(path.parent / "rounds.json") or []
        for r in rounds if isinstance(rounds, list) else []:
            if r.get("hypothesis") and r.get("decision") is not None:
                add("past.run", f"Past RD-Agent round ({run['market']}, "
                                f"{'adopted' if r['decision'] else 'not adopted'}): {r['hypothesis']}", period="past runs")
                count += 1
                if count >= 6:
                    return


def _idea_facts(add) -> None:
    """线索池只给标题与渠道；不提供测试期之后的监控数值。"""
    try:
        from factopia import ideas
        items = ideas.all_ideas()
    except Exception:
        items = []
    for idea in items[:5]:
        add("ideas", f"Idea pool ({idea.get('channel_zh')}, {idea.get('status')}): {idea.get('title')}", period="ideas")
