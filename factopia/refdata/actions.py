"""公司行动（BR-16）。

| 类型 | 来源 | 说明 |
|---|---|---|
| 分红 dividend | Yahoo Finance | 除息日、每股金额（交易币种） |
| 拆股 / 合股 split | Yahoo Finance | 除权日、比例（如 2:1 记为 2.0） |
| 代码变更 ticker_change | AMF 历史记录（法国） | 同一 ISIN 先后使用的交易代码及日期区间 |
| 疑似停牌 suspension | 标准化行情 | 上市期间连续 ≥ SUSPEND_MIN 个交易日无行情，或连续零成交 |
| 合并、配股、退市、名称变更 | overrides（人工） | 免费数据源没有可靠记录，需数据商；当前股票池为现有成分股，无退市样本（第 14.10 节） |

行情为 Yahoo Finance 复权价（分红与拆股均已复权，收益为总收益）；本表用于展示、核对与复权检查：
拆股日前后未复权价应出现与比例一致的跳变，复权价不应有该跳变（check_adjustment）。
"""
import json
import os
from pathlib import Path

import pandas as pd

from factopia.refdata.master import REF, RUNTIME, _cache, _save, now, universe

SUSPEND_MIN = 3
TYPE_ZH = {"dividend": "分红", "split": "拆股 / 合股", "ticker_change": "代码变更", "suspension": "疑似停牌",
           "manual": "人工登记"}


def fetch_actions(symbols: list[str]) -> dict:
    import yfinance as yf

    p, cache = _cache("yahoo_actions")
    for s in symbols:
        if s in cache:
            continue
        try:
            a = yf.Ticker(s).actions
            rows = []
            for dt, r in a.iterrows():
                d = pd.Timestamp(dt).tz_localize(None).strftime("%Y-%m-%d")
                if r.get("Dividends"):
                    rows.append({"date": d, "type": "dividend", "value": float(r["Dividends"])})
                if r.get("Stock Splits"):
                    rows.append({"date": d, "type": "split", "value": float(r["Stock Splits"])})
            cache[s] = {"rows": rows, "fetched_at": now()}
        except Exception as e:
            cache[s] = {"rows": [], "error": str(e)[:200], "fetched_at": now()}
        _save(p, cache)
    return cache


def ticker_changes() -> list[dict]:
    """法国：AMF 记录中同一 ISIN 使用过的交易代码（第 14.9 节采集时的元数据）。"""
    path = RUNTIME / "text" / "fr" / "ticker_history.json"
    if not path.exists():
        return []
    out = []
    for isin, rec in json.loads(path.read_text(encoding="utf-8")).items():
        ts = sorted(rec["tickers"], key=lambda t: t["first"])
        if len(ts) < 2:
            continue
        for a, b in zip(ts, ts[1:]):
            out.append({"symbol": rec["instrument"], "date": b["first"][:10], "type": "ticker_change",
                        "value": None, "detail": f"{a['ticker']} → {b['ticker']}（ISIN {isin}，AMF 记录）"})
    return out


def suspensions(market: str) -> list[dict]:
    import exchange_calendars as xcals

    from factopia.data.standardize import MARKETS as STD

    path = RUNTIME / "data" / "standardized" / f"{market}.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path, columns=["symbol", "date", "volume"])
    cal = xcals.get_calendar(STD[market]["calendar"])
    out = []
    for sym, g in df.groupby("symbol"):
        sessions = cal.sessions_in_range(g["date"].min(), g["date"].max())
        have = pd.Series(True, index=pd.DatetimeIndex(g["date"]))
        traded = g.set_index("date")["volume"].reindex(sessions).fillna(0) > 0
        idle = ~traded                      # 缺行情或零成交
        run, start = 0, None
        for d, x in idle.items():
            if x:
                run, start = run + 1, start or d
            else:
                if run >= SUSPEND_MIN:
                    missing = int((~sessions[(sessions >= start) & (sessions < d)].isin(have.index)).sum())
                    out.append({"symbol": sym, "date": start.strftime("%Y-%m-%d"), "type": "suspension", "value": float(run),
                                "detail": f"连续 {run} 个交易日无成交（其中 {missing} 天无行情），至 {d.strftime('%Y-%m-%d')} 恢复"})
                run, start = 0, None
    return out


MIN_JUMP = 0.15     # 只检查未复权跳变 ≥ 15% 的拆股 / 合股


def check_adjustment(market: str, splits: pd.DataFrame) -> list[dict]:
    """复权检查：拆股日复权收盘价的日收益应正常（|收益| < 1 - 1/比例 的一半），否则说明复权缺失。"""
    path = RUNTIME / "data" / "standardized" / f"{market}.parquet"
    if splits.empty or not path.exists():
        return []
    px = pd.read_parquet(path, columns=["symbol", "date", "close"]).sort_values(["symbol", "date"])
    px["ret"] = px.groupby("symbol")["close"].pct_change()
    out = []
    first = px.groupby("symbol")["date"].min()
    for r in splits.to_dict("records"):
        if pd.isna(r["value"]) or r["value"] in (0, 1) or r["symbol"] not in first.index or pd.Timestamp(r["date"]) <= pd.Timestamp(first[r["symbol"]]):
            continue                                    # 行情开始之前的拆股：无法检查
        g = px[(px["symbol"] == r["symbol"]) & (px["date"] >= r["date"])].head(1)
        if g.empty or pd.isna(g["ret"].iloc[0]):
            continue
        ret = float(g["ret"].iloc[0])
        jump = abs(1 / r["value"] - 1)
        if jump < MIN_JUMP:                             # 比例接近 1（如 1.02 送股）：跳变小于正常日波动，无法据此判断
            continue
        ok = abs(ret) < jump / 2
        out.append({"symbol": r["symbol"], "date": r["date"], "ratio": r["value"], "adjusted_return": round(ret, 4),
                    "raw_jump": round(jump, 4), "ok": bool(ok)})
    return out


def build(symbols: list[str] | None = None) -> dict:
    uni = universe()
    if symbols:
        uni = uni[uni["symbol"].isin(symbols)]
    cache = fetch_actions(uni["symbol"].tolist())
    rows = []
    for s, m in zip(uni["symbol"], uni["market"]):
        for r in (cache.get(s) or {}).get("rows", []):
            rows.append({"symbol": s, "market": m, **r,
                         "detail": f"每股 {r['value']:.4f}" if r["type"] == "dividend" else f"比例 {r['value']:g}"})
    mk = dict(zip(uni["symbol"], uni["market"]))
    for r in ticker_changes():
        if r["symbol"] in mk:
            rows.append({**r, "market": mk[r["symbol"]]})
    for m in sorted(set(uni["market"])):
        rows += [{**r, "market": m} for r in suspensions(m) if r["symbol"] in mk]
    df = pd.DataFrame(rows, columns=["symbol", "market", "date", "type", "value", "detail"])
    df["source"] = df["type"].map({"dividend": "Yahoo Finance", "split": "Yahoo Finance", "ticker_change": "AMF",
                                   "suspension": "标准化行情（推断）"})
    # 数据商公司行动（第 14.21 节）：合并、配股、退市、名称变更等；与免费来源同类型同日期的记录以数据商为准
    from factopia.refdata import vendor
    va = vendor.vendor_actions()
    if va is not None and not va.empty:
        if symbols:
            va = va[va["symbol"].isin(symbols)]
        key = set(zip(va["symbol"], va["date"], va["type"]))
        df = df[[k not in key for k in zip(df["symbol"], df["date"], df["type"])]]
        df = pd.concat([df, va[["symbol", "market", "date", "type", "value", "detail", "source"]]], ignore_index=True)
    df["type_zh"] = df["type"].map(lambda t: TYPE_ZH.get(t) or vendor.ACTION_TYPES.get(t, t))
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    REF.mkdir(parents=True, exist_ok=True)
    df.to_parquet(REF / "corporate_actions.parquet", index=False)
    checks = []
    for m in sorted(set(uni["market"])):
        checks += check_adjustment(m, df[(df["type"] == "split") & (df["market"] == m)])
    summary = {"built_at": now(), "total": len(df), "by_type": df["type"].value_counts().to_dict(),
               "symbols_with_actions": int(df["symbol"].nunique()), "adjustment_checks": checks,
               "adjustment_failures": [c for c in checks if not c["ok"]],
               "vendor_rows": int((~df["source"].isin(["Yahoo Finance", "AMF", "标准化行情（推断）"])).sum()),
               "not_covered": [zh for t, zh in {"merger": "合并", "rights_issue": "配股", "delisting": "退市",
                                                "name_change": "名称变更"}.items() if t not in set(df["type"])]}
    (REF / "actions_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary
