"""③ 打分、④ 时间对齐、⑤ 检验（BR-10、BR-15、BR-49；Spec 第 11.5 节）。

打分：每篇公告、每个概念的风险语气 = Σ(方向 × 置信度) / 段落数 × 100（每百段的净风险表述），使用复核后的标注。
变化 = 本期 − 同一公司、同一语言的上一期（真实数据按同类公告比较，年报比年报，避免篇幅差异）；每个分数附带贡献最大的段落作为引用（来源、段落、发布时间、可获取时间）。

时间对齐（无未来数据泄漏）：
- business_date：公告日期（发布当地日期）；published_at：正式发布时间（UTC）；
- available_at：published_at + 处理延迟（下载、抽取、翻译、理解，按数据集配置）；
- effective_date：收盘时刻（交易所日历，含夏令时）不早于 available_at 的第一个交易日——当天收盘后才使用，
  收盘后发布的公告顺延到下一交易日。回测按 Qlib 惯例次日起持有，因此再多留一天余量。

日度信号：生效日起沿用最近一次变化，最长 HOLD_DAYS 个交易日（一个季度）；同一天生效的多篇（如法文版与英文版）取平均。

检验：未来 N 日收益（次日起，Qlib 口径）与信号的逐日横截面 Rank IC、Newey-West t、三分组收益差、公告后超额收益（事件研究）。
假设方向：风险表述变化越大（越悲观），未来收益越低 → 预期 Rank IC 为负。
"""
import numpy as np
import pandas as pd

from factopia.factors.stats import newey_west_t
from factopia.text import review
from factopia.text.common import DATASETS, PRICES, concept_names, ds_dir, read_json, write_json

HOLD_DAYS = 63
MIN_STOCKS = 5
TOP_CITATIONS = 3


def prices(dataset: str) -> pd.DataFrame:
    path = ds_dir(dataset) / "prices.parquet" if DATASETS[dataset]["simulated"] \
        else PRICES / f"{DATASETS[dataset]['market']}.parquet"
    df = pd.read_parquet(path, columns=["symbol", "date", "close"])
    return df.pivot(index="date", columns="symbol", values="close").sort_index()


def sessions(dataset: str, start, end) -> pd.Series:
    """交易日 → 收盘时刻（UTC），来自交易所日历（含夏令时、半日市）。"""
    import exchange_calendars as xcals

    cal = xcals.get_calendar(DATASETS[dataset]["calendar"])
    lo = max(pd.Timestamp(start).tz_localize(None).normalize(), cal.first_session)
    hi = min(pd.Timestamp(end).tz_localize(None).normalize(), cal.last_session)
    return cal.schedule.loc[lo:hi, "close"]


def align(meta: pd.DataFrame, dataset: str) -> pd.DataFrame:
    cfg = DATASETS[dataset]
    pub = pd.to_datetime(meta["published_at"], utc=True)
    avail = pub + pd.Timedelta(minutes=cfg["processing_delay_min"])
    closes = sessions(dataset, pub.min() - pd.Timedelta(days=5), avail.max() + pd.Timedelta(days=15))
    close_vals = closes.dt.tz_convert(None).to_numpy()
    pos = np.searchsorted(close_vals, avail.dt.tz_convert(None).to_numpy(), side="left")
    eff = [closes.index[p] if p < len(closes) else pd.NaT for p in pos]
    out = meta.copy()
    out["business_date"] = pub.dt.tz_convert(cfg["tz"]).dt.strftime("%Y-%m-%d").to_numpy()
    out["available_at"] = avail.dt.strftime("%Y-%m-%dT%H:%M:%SZ").to_numpy()
    out["effective_date"] = pd.to_datetime(eff)
    out["after_close"] = [bool(pd.notna(e) and pd.Timestamp(e).strftime("%Y-%m-%d") != b)
                          for e, b in zip(out["effective_date"], out["business_date"])]
    return out


def score_filing(dataset: str, filing: dict, dec: dict) -> dict | None:
    lab = read_json(ds_dir(dataset) / "labels" / f"{filing['id']}.json")
    if not lab:
        return None
    n = max(int(lab["paragraphs_total"]), 1)
    tone = {c: 0.0 for c in concept_names()}
    contrib = []
    pending = 0
    for x in lab["labels"]:
        eff = review.apply(x, dec.get(review.key(dataset, filing["id"], x["i"])))
        pending += eff["review_status"] == "pending"
        for c in eff["concepts"]:
            v = c["direction"] * c["confidence"]
            tone[c["concept"]] += v / n * 100
            contrib.append({"i": x["i"], "concept": c["concept"], "value": round(v / n * 100, 4),
                            "negated": c["negated"], "evidence": c.get("evidence", ""), "review": eff["review_status"]})
    return {"tone": {c: round(v, 4) for c, v in tone.items()}, "tone_total": round(sum(tone.values()), 4),
            "contrib": contrib, "pending_review": pending, "model": lab["model"],
            "glossary_version": lab["glossary_version"], "prompt_version": lab["prompt_version"]}


def scores(dataset: str, realign: bool = True, save: bool = True) -> pd.DataFrame:
    """realign=False：使用上次对齐的结果（aligned.parquet），供没有交易日历的镜像复现信号（文本信号研究的复现检查）。"""
    d = ds_dir(dataset)
    if realign:
        meta = align(pd.read_parquet(d / "filings.parquet"), dataset)
        if save:                                                 # 敏感性检验不覆盖已保存的对齐结果
            meta.to_parquet(d / "aligned.parquet", index=False)  # 检索与页面直接读取（api 镜像不含交易日历）
    else:
        meta = pd.read_parquet(d / "aligned.parquet")
    dec = review.decisions()
    rows = []
    for f in meta.to_dict("records"):
        s = score_filing(dataset, f, dec)
        if s:
            rows.append({**f, **s})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    by_kind = DATASETS[dataset]["series_by_subtype"]
    df["series"] = df["instrument"] + "|" + (df["subtype_fr"].fillna("") if by_kind else "") + "|" + df["language"]
    df = df.sort_values("published_at")
    prev = df.groupby("series")["tone_total"].shift(1)
    prev_id = df.groupby("series")["id"].shift(1)
    df["prev_id"] = prev_id
    df["change"] = df["tone_total"] - prev
    for c in concept_names():
        df[f"change_{c}"] = df["tone"].map(lambda t, c=c: t[c]) - df.groupby("series")["tone"].shift(1).map(
            lambda t, c=c: t[c] if isinstance(t, dict) else np.nan)
    return df


def daily_signal(df: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """(日期 × 股票) 风险表述变化；生效日起沿用，最长 HOLD_DAYS 个交易日。"""
    ev = df.dropna(subset=["change", "effective_date"])
    ev = ev[ev["instrument"].isin(px.columns)]
    if ev.empty:
        return pd.DataFrame(index=px.index, columns=px.columns, dtype=float)
    # 生效日没有行情（数据缺失）时，顺延到下一个有行情的交易日
    pos = px.index.searchsorted(ev["effective_date"])
    ev = ev[pos < len(px.index)].assign(day=px.index[pos[pos < len(px.index)]])
    daily = ev.groupby(["day", "instrument"])["change"].mean().unstack().reindex(px.index)
    return daily.ffill(limit=HOLD_DAYS).reindex(columns=px.columns)


def evaluate(sig: pd.DataFrame, px: pd.DataFrame, events: pd.DataFrame, horizon: int) -> dict:
    fwd = px.shift(-(horizon + 1)) / px.shift(-1) - 1
    ric, spread, cover = {}, {}, {}
    for dt in sig.index:
        s, y = sig.loc[dt], fwd.loc[dt]
        ok = s.notna() & y.notna()
        cover[dt] = int(s.notna().sum())
        if ok.sum() < MIN_STOCKS:
            continue
        s, y = s[ok], y[ok]
        ric[dt] = s.rank().corr(y.rank())
        q = s.rank(pct=True)
        spread[dt] = y[q > 2 / 3].mean() - y[q <= 1 / 3].mean()
    ric, spread = pd.Series(ric, dtype=float), pd.Series(spread, dtype=float)
    # 事件研究：生效日后 N 日相对同市场等权平均的超额收益，按变化方向分组
    ex = fwd.sub(fwd.mean(axis=1), axis=0)
    rows = []
    for e in events.dropna(subset=["change", "effective_date"]).to_dict("records"):
        if e["instrument"] in ex.columns and e["effective_date"] in ex.index:
            v = ex.at[e["effective_date"], e["instrument"]]
            if pd.notna(v):
                rows.append({"change": e["change"], "excess": v})
    es = pd.DataFrame(rows)

    def grp(mask):
        sub = es[mask] if len(es) else es
        return {"n": int(len(sub)), "mean_excess": None if sub.empty else round(float(sub["excess"].mean()), 6)}

    ev_stats = {"pessimistic": grp(es["change"] > 0) if len(es) else grp(None),
                "optimistic": grp(es["change"] < 0) if len(es) else grp(None), "rank_corr": None, "diff_t": None}
    if len(es) >= 10:
        ev_stats["rank_corr"] = round(float(es["change"].rank().corr(es["excess"].rank())), 4)
        a, b = es.loc[es["change"] > 0, "excess"], es.loc[es["change"] < 0, "excess"]
        if len(a) > 2 and len(b) > 2:   # 两组均值差的 t 值（Welch）
            ev_stats["diff_t"] = round(float((a.mean() - b.mean()) / np.sqrt(a.var() / len(a) + b.var() / len(b))), 3)

    def nz(x):
        return None if x is None or pd.isna(x) else round(float(x), 6)

    return {
        "horizon": horizon, "n_days": int(ric.notna().sum()),
        "rank_ic_mean": nz(ric.mean()), "rank_ic_t": nz(newey_west_t(ric, horizon)) if len(ric) else None,
        "positive_ratio": nz((ric > 0).mean()) if len(ric) else None,
        "tercile_spread_mean": nz(spread.mean()), "tercile_spread_t": nz(newey_west_t(spread, horizon)) if len(spread) else None,
        "avg_coverage": nz(pd.Series(cover).mean()),
        "events": ev_stats,
        "ic_monthly": {k.strftime("%Y-%m"): round(float(v), 4) for k, v in ric.resample("ME").mean().dropna().items()}
        if len(ric) else {},
    }


def _citations(row: dict, dataset: str, doc_cache: dict) -> list[dict]:
    if row["id"] not in doc_cache:
        doc_cache[row["id"]] = read_json(ds_dir(dataset) / "docs" / f"{row['id']}.json")
    doc = doc_cache[row["id"]]
    top = sorted(row["contrib"], key=lambda c: -abs(c["value"]))[:TOP_CITATIONS]
    names = concept_names()
    return [{**c, "name": names[c["concept"]], "text": doc["paragraphs"][c["i"]]["text"][:600]} for c in top]


def run(dataset: str, horizon: int = 20) -> dict:
    d = ds_dir(dataset)
    df = scores(dataset)
    if df.empty:
        raise RuntimeError(f"{dataset}：尚无理解结果，请先运行 understand")
    px = prices(dataset)
    sig = daily_signal(df, px)
    sig.stack().rename("risk_change").rename_axis(["datetime", "instrument"]).to_frame().to_parquet(d / "signal.parquet")
    ev = evaluate(sig, px, df, horizon)
    doc_cache: dict = {}
    names = concept_names()
    filings = []
    for r in df.to_dict("records"):
        filings.append({
            "id": r["id"], "instrument": r["instrument"], "company": r["company"], "title": r["title"],
            "language": r["language"], "subtype": r.get("subtype_fr"), "url": r.get("url"),
            "business_date": r["business_date"], "published_at": r["published_at"], "available_at": r["available_at"],
            "effective_date": None if pd.isna(r["effective_date"]) else pd.Timestamp(r["effective_date"]).strftime("%Y-%m-%d"),
            "after_close": r["after_close"], "tone": r["tone"], "tone_total": r["tone_total"],
            "change": None if pd.isna(r["change"]) else round(float(r["change"]), 4), "prev_id": None if pd.isna(r["prev_id"]) else r["prev_id"],
            "changes": {c: (None if pd.isna(r[f"change_{c}"]) else round(float(r[f"change_{c}"]), 4)) for c in names},
            "pending_review": int(r["pending_review"]), "model": r["model"],
            "glossary_version": r["glossary_version"], "prompt_version": r["prompt_version"],
            "citations": _citations(r, dataset, doc_cache),
        })
    cfg = DATASETS[dataset]
    out = {"dataset": dataset, "market": cfg["market"], "simulated": cfg["simulated"], "source": cfg["source"],
           "licence": cfg["licence"], "processing_delay_min": cfg["processing_delay_min"], "hold_days": HOLD_DAYS,
           "concepts": names, "n_filings": len(df), "n_companies": int(df["instrument"].nunique()),
           "n_changes": int(df["change"].notna().sum()), "evaluation": ev, "filings": filings[::-1]}
    write_json(d / "summary.json", out)
    return out


RULE_NOTE = ("结论以逐日 Rank IC 的 t 值（Newey-West）为主口径：t < −2 且事件检验不反向 → 有效；"
             "主口径不显著时，事件检验显著只作辅助证据，结论仍为不显著（2026-10-11 收紧，原规则为两个口径任一显著即有效）")


def judge(n_days: int, t: float | None, et: float | None) -> str:
    """三个市场检验的结论（第 11.10 节）。主口径：逐日 Rank IC 的 Newey-West t 值；事件检验（Welch t）只作辅助。
    原规则"两个口径任一显著即有效"相当于多试一次，偏宽松，已收紧。"""
    if not n_days or t is None:
        return "数据不足（每天少于 5 只股票有信号）"
    ev_neg, ev_pos = et is not None and et < -2, et is not None and et > 2
    if t < -2:
        return "结论不一致（事件检验方向相反）" if ev_pos else "有效（方向符合假设）"
    if t > 2:
        return "结论不一致（事件检验方向相反）" if ev_neg else "方向相反"
    if ev_neg:
        return "不显著（仅事件检验显著，作辅助证据）"
    if ev_pos:
        return "不显著（事件检验方向相反）"
    return "不显著"


def sensitivity(dataset: str, delays=(60, 120, 240), holds=(21, 63, 126), horizon: int = 20) -> dict:
    """敏感性检验（第 11.10 节）：处理延迟与信号沿用期取不同值，看结论是否稳定；不改动已保存的信号与对齐结果。"""
    global HOLD_DAYS
    cfg = DATASETS[dataset]
    base_delay, base_hold = cfg["processing_delay_min"], HOLD_DAYS
    px = prices(dataset)
    rows = []
    try:
        for dl in delays:
            cfg["processing_delay_min"] = dl
            df = scores(dataset, realign=True, save=False)
            if df.empty:
                continue
            for hd in holds:
                HOLD_DAYS = hd
                e = evaluate(daily_signal(df, px), px, df, horizon)
                rows.append({"delay_min": dl, "hold_days": hd, "rank_ic_mean": e["rank_ic_mean"], "rank_ic_t": e["rank_ic_t"],
                             "event_diff_t": e["events"]["diff_t"], "n_days": e["n_days"],
                             "verdict": judge(e["n_days"], e["rank_ic_t"], e["events"]["diff_t"]),
                             "baseline": dl == base_delay and hd == base_hold})
    finally:
        cfg["processing_delay_min"], HOLD_DAYS = base_delay, base_hold
    verdicts = {r["verdict"] for r in rows}
    out = {"dataset": dataset, "simulated": cfg["simulated"], "horizon": horizon, "rows": rows,
           "stable": len(verdicts) == 1, "verdicts": sorted(verdicts),
           "note": "基准为处理延迟 60 分钟、沿用 63 个交易日；结论只在部分参数下成立说明不稳健"}
    write_json(ds_dir(dataset) / "sensitivity.json", out)
    return out


def compare(horizon: int = 20) -> dict:
    """⑤ 三个市场分别检验后的对比（演示主线第 6 步）；模拟数据的市场明确标注。"""
    rows = []
    for ds, cfg in DATASETS.items():
        s = read_json(ds_dir(ds) / "summary.json")
        if not s:
            continue
        e = s["evaluation"]
        t, et = e.get("rank_ic_t"), e["events"].get("diff_t")
        verdict = judge(e["n_days"], t, et)
        rows.append({"dataset": ds, "market": cfg["market"], "simulated": cfg["simulated"], "n_filings": s["n_filings"],
                     "n_companies": s["n_companies"], "rank_ic_mean": e["rank_ic_mean"], "rank_ic_t": t,
                     "tercile_spread_mean": e["tercile_spread_mean"], "n_days": e["n_days"],
                     "event_pessimistic": e["events"]["pessimistic"], "event_optimistic": e["events"]["optimistic"],
                     "event_diff_t": et, "verdict": verdict})
    real = [r for r in rows if not r["simulated"] and r["n_days"]]
    note = ("只有法国是真实数据；香港、英国为模拟数据（植入已知规律），用于验证流程能否找出规律，不构成研究结论"
            if any(r["simulated"] for r in rows) else "")
    out = {"horizon": horizon, "markets": rows, "note": note, "real_markets": [r["market"] for r in real], "rule": RULE_NOTE}
    write_json(ds_dir("fr").parent / "compare.json", out)
    return out
