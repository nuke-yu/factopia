"""证券主数据（BR-13、BR-17）。

来源（均为开放数据，结果为"当前快照"，记录取数时间）：
- Yahoo Finance：名称、行业（Morningstar 口径，非 GICS / ICB）、总部国家、财务年度结束月、财务报告币种
- OpenFIGI：FIGI、综合 FIGI、股份类别 FIGI（不同交易所挂牌的同一股份类别相同）、证券类型（普通股 / 存托凭证）
- GLEIF：法律实体识别码（LEI）、法定名称、注册地（按名称检索，名称一致才采用）
- AMF：法国股票的 ISIN（第 14.9 节 isin_map.csv）；其他市场的 ISIN 需数据商授权

发行人：优先用 LEI 归并；没有 LEI 时用股份类别 FIGI。
跨市场挂牌（OpenFIGI 按名称检索，只看 markets.yaml 中的主要交易所，名称规范化后一致才采用，标为"自动匹配，待确认"）：
- same_share_class：同一股份类别在多个交易所挂牌（如汇丰 0005.HK 与 HSBA.L）
- depositary_receipt：存托凭证（如美国 ADR）
- other_share_class：同一发行人的其他股份类别（如 H 股对应的上海 / 深圳 A 股）
主要上市地：发行人挂牌的交易所中，与注册地同一国家的唯一一个；无法唯一确定时为"待确认"。overrides.yaml 可人工覆盖。
"""
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
REF = RUNTIME / "refdata"
RAW = REF / "raw"
MARKETS = ("hk", "uk", "fr")
VENUE_COUNTRY = {"US": "US", "LN": "GB", "HK": "HK", "FP": "FR", "NA": "NL", "BB": "BE", "IM": "IT", "SM": "ES",
                 "SE": "CH", "CG": "CN", "CS": "CN", "AT": "AU", "SP": "SG", "JT": "JP"}
RELATION_ZH = {"same_share_class": "同一股份多地上市", "depositary_receipt": "存托凭证",
               "other_share_class": "同一发行人的其他股份类别"}
SUFFIXES = r"\b(plc|ltd|limited|holdings?|group|co|company|corp|corporation|inc|sa|se|nv|ag|spa|the|and|de|la|le|les|" \
           r"class [a-z]|cl [a-z]|sp adr|adr|ads|gdr|sdr|unspon|spons|sponsored|lt|reg|ord|shs|h|a|b)\b"
YF_KEYS = ("longName", "shortName", "sector", "industry", "country", "exchange", "currency", "financialCurrency",
           "quoteType", "lastFiscalYearEnd", "website")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def markets_cfg() -> dict:
    return yaml.safe_load((CONFIGS / "refdata" / "markets.yaml").read_text(encoding="utf-8"))


def overrides() -> dict:
    p = CONFIGS / "refdata" / "overrides.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {}


def strip_accents(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def norm_name(name: str | None) -> str:
    s = strip_accents(name or "").lower().replace("&", " and ").replace("-", " ").replace(".", " ").replace(",", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cache(name: str) -> tuple[Path, dict]:
    p = RAW / f"{name}.json"
    return p, (json.loads(p.read_text(encoding="utf-8")) if p.exists() else {})


def _save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _http(url: str, body: dict | list | None = None, headers: dict | None = None, retries: int = 5):
    h = {"User-Agent": "factopia-poc (research)", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    for k in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:                      # 超出频率限制：等待后重试
                time.sleep(int(e.headers.get("Retry-After") or 30))
                continue
            if e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(3 * (k + 1))
    raise RuntimeError(f"请求失败：{url}")


def universe() -> pd.DataFrame:
    frames = []
    for m in MARKETS:
        u = pd.read_csv(CONFIGS / "universe" / f"{m}.csv")
        u["market"] = m
        frames.append(u)
    return pd.concat(frames, ignore_index=True)


def figi_query(symbol: str) -> dict:
    code, suffix = symbol.rsplit(".", 1)
    exch = {"HK": "HK", "L": "LN", "PA": "FP", "AS": "NA"}[suffix]
    if exch == "HK":
        code = code.lstrip("0") or "0"
    return {"idType": "TICKER", "idValue": code.replace("-", "/"), "exchCode": exch}


# ---------------------------------------------------------------- 取数（带缓存，可中断后继续）

def fetch_yahoo(symbols: list[str]) -> dict:
    import yfinance as yf

    p, cache = _cache("yahoo")
    for s in symbols:
        if s in cache:
            continue
        try:
            info = yf.Ticker(s).info
            cache[s] = {**{k: info.get(k) for k in YF_KEYS}, "fetched_at": now()}
        except Exception as e:      # 个别代码取不到：记录原因，不中断
            cache[s] = {"error": str(e)[:200], "fetched_at": now()}
        _save(p, cache)
    return cache


def fetch_figi(symbols: list[str]) -> dict:
    p, cache = _cache("openfigi")
    todo = [s for s in symbols if s not in cache]
    key = os.environ.get("OPENFIGI_API_KEY")
    headers = {"X-OPENFIGI-APIKEY": key} if key else {}
    for k in range(0, len(todo), 10):          # 每次请求最多 10 个代码；无密钥时每分钟 25 次
        batch = todo[k:k + 10]
        res = _http("https://api.openfigi.com/v3/mapping", [figi_query(s) for s in batch], headers)
        for s, r in zip(batch, res):
            rows = [d for d in (r.get("data") or []) if d.get("marketSector") == "Equity"]
            cache[s] = {"data": rows[:3], "warning": r.get("warning"), "fetched_at": now()}
        _save(p, cache)
        time.sleep(0.5 if key else 2.6)
    return cache


def _gleif(params: dict) -> list[dict]:
    url = "https://api.gleif.org/api/v1/lei-records?" + urllib.parse.urlencode({**params, "page[size]": 8})
    out = []
    for d in _http(url).get("data") or []:
        a, e = d["attributes"], d["attributes"]["entity"]
        out.append({"lei": a["lei"], "legal_name": e["legalName"]["name"], "jurisdiction": e.get("jurisdiction"),
                    "legal_country": (e.get("legalAddress") or {}).get("country"),
                    "status": e.get("status"), "category": e.get("category")})
    time.sleep(1.1)                 # GLEIF：每分钟最多 60 次
    return out


def name_variants(names: list[str]) -> list[str]:
    """法定名称的常见写法：原样、去重音、去掉股份类别后缀、plc / Ltd 展开。"""
    out = []
    for n in names:
        if not n:
            continue
        base = re.sub(r"[-\s]+(H|A|B|SP ADR|ADR|REG)$", "", strip_accents(n).strip(), flags=re.I)
        for v in (n, strip_accents(n), base, re.sub(r"\bplc$", "public limited company", base, flags=re.I),
                  re.sub(r"\bltd\.?$", "limited", base, flags=re.I), re.sub(r",.*$", "", base)):
            if v and v not in out:
                out.append(v)
    return out


def fetch_gleif(names: dict[str, list[str]], isins: dict[str, str]) -> dict:
    """证券 → 法律实体（GLEIF）。顺序：ISIN 精确查找 → 法定名称精确匹配（多种写法）→ 全文检索；只接受名称规范化后一致的 ACTIVE 记录。"""
    p, cache = _cache("gleif")
    for s, cands in names.items():
        if s in cache and "match" in cache[s]:
            continue
        want = {norm_name(n) for n in cands if n}
        found, how, tried = None, None, []
        try:
            if isins.get(s):
                rows = _gleif({"filter[isin]": isins[s]})
                found, how = (rows[0], "ISIN") if rows else (None, None)
            for v in ([] if found else name_variants(cands)):
                rows = _gleif({"filter[entity.legalName]": v})
                tried.append(v)
                found = next((c for c in rows if c["status"] == "ACTIVE" and norm_name(c["legal_name"]) in
                              want | {norm_name(v)}), None)
                if found:
                    how = "法定名称精确匹配"
                    break
            if not found and cands:
                rows = _gleif({"filter[fulltext]": next(c for c in cands if c)})
                found = next((c for c in rows if c["status"] == "ACTIVE" and norm_name(c["legal_name"]) in want), None)
                how = "全文检索" if found else None
            cache[s] = {"match": found, "method": how, "tried": tried, "fetched_at": now()}
        except Exception as e:
            cache[s] = {"error": str(e)[:200], "fetched_at": now()}
        _save(p, cache)
    return cache


def fetch_listings(issuers: dict[str, str]) -> dict:
    """发行人名称 → 各交易所的股票挂牌（OpenFIGI 检索；无密钥时每分钟 5 次，较慢，可中断后继续）。键为发行人编号。"""
    p, cache = _cache("openfigi_search")
    key = os.environ.get("OPENFIGI_API_KEY")
    headers = {"X-OPENFIGI-APIKEY": key} if key else {}
    venues = set(markets_cfg()["cross_listing_venues"])
    for issuer, name in issuers.items():
        if not name or (issuer in cache and not cache[issuer].get("failed")):
            continue
        rows = []
        for st in ("Common Stock", "Depositary Receipt"):
            try:
                res = _http("https://api.openfigi.com/v3/search",
                            {"query": name, "marketSecDes": "Equity", "securityType2": st}, headers)
            except Exception as e:
                rows.append({"error": str(e)[:200]})
                continue
            rows += [d for d in res.get("data") or [] if d.get("exchCode") in venues]
            time.sleep(3.5 if key else 12.5)
        failed = any("error" in r for r in rows)        # 有失败的请求：下次重试
        cache[issuer] = {"query": name, "data": [r for r in rows if "error" not in r], "failed": failed, "fetched_at": now()}
        _save(p, cache)
        print(f"  {issuer} {name}: {len(rows)} 条挂牌", flush=True)
    return cache


# ---------------------------------------------------------------- 合成主数据表

def _fr_isins() -> dict:
    p = RUNTIME / "text" / "fr" / "isin_map.csv"
    if not p.exists():
        return {}
    m = pd.read_csv(p).dropna(subset=["isin"])
    return dict(zip(m["instrument"], m["isin"]))


def build(cross_listings: bool = True, symbols: list[str] | None = None) -> dict:
    cfg = markets_cfg()
    ov = overrides()
    uni = universe()
    if symbols:
        uni = uni[uni["symbol"].isin(symbols)]
    syms = uni["symbol"].tolist()
    print(f"Yahoo Finance：{len(syms)} 只", flush=True)
    yfc = fetch_yahoo(syms)
    print("OpenFIGI 代码映射", flush=True)
    fg = fetch_figi(syms)
    names = {s: [(yfc.get(s) or {}).get("longName"), ((fg.get(s) or {}).get("data") or [{}])[0].get("name"), n]
             for s, n in zip(uni["symbol"], uni["name"])}
    isins = _fr_isins()
    print("GLEIF 法律实体", flush=True)
    gl = fetch_gleif(names, isins)
    rows = []
    for r in uni.to_dict("records"):
        s, m = r["symbol"], r["market"]
        y = yfc.get(s) or {}
        f = ((fg.get(s) or {}).get("data") or [{}])[0]
        lei = (gl.get(s) or {}).get("match")
        juris = (lei or {}).get("jurisdiction") or ""
        juris_cc = juris.split("-")[0] if juris else None
        fye = y.get("lastFiscalYearEnd")
        rows.append({
            "symbol": s, "market": m, "name": r["name"], "long_name": y.get("longName"),
            "exchange": cfg["markets"][m]["exchange"], "currency": cfg["markets"][m]["currency"],
            "figi": f.get("figi"), "composite_figi": f.get("compositeFIGI"), "share_class_figi": f.get("shareClassFIGI"),
            "figi_name": f.get("name"), "security_type": f.get("securityType2"),
            "isin": isins.get(s), "lei": (lei or {}).get("lei"), "legal_name": (lei or {}).get("legal_name"),
            "lei_method": (gl.get(s) or {}).get("method"),
            "jurisdiction": juris_cc, "legal_country": (lei or {}).get("legal_country"),
            "sector": y.get("sector"), "industry": y.get("industry"), "hq_country": y.get("country"),
            "reporting_currency": y.get("financialCurrency"),
            "fiscal_year_end_month": int(pd.Timestamp(fye, unit="s").month) if fye else None,
            "accounting": cfg["accounting_by_jurisdiction"].get(juris_cc) or cfg["markets"][m]["accounting_default"],
            "accounting_basis": "按注册地推断" if juris_cc in cfg["accounting_by_jurisdiction"] else "市场默认值",
            "reporting": cfg["markets"][m]["reporting"],
            "snapshot_at": y.get("fetched_at"),
        })
    sec = pd.DataFrame(rows)
    sec["issuer_id"] = sec["lei"].fillna("FIGI:" + sec["share_class_figi"].fillna(sec["symbol"]))
    # 检索用名称：OpenFIGI 的简短名称（去掉 -H / -A 等股份类别后缀），比完整法定名称更容易命中
    sec["search_name"] = (sec["figi_name"].fillna(sec["long_name"]).str.replace(r"-(H|A|B|R|SP ADR)$", "", regex=True)
                          .str.replace(r"\s+(LTD|LIMITED|PLC|SA|SE|NV|AG|CORP|INC|CO|SPA)\.?$", "", regex=True, case=False))
    issuers = sec.groupby("issuer_id").agg(name=("long_name", "first"), search_name=("search_name", "first"), legal_name=("legal_name", "first"),
                                           jurisdiction=("jurisdiction", "first"),
                                           symbols=("symbol", list)).reset_index()
    listings_raw = {}
    if cross_listings:
        print(f"OpenFIGI 跨市场挂牌检索：{len(issuers)} 个发行人（无密钥时约每家 25 秒）", flush=True)
        listings_raw = fetch_listings(dict(zip(issuers["issuer_id"], issuers["search_name"].fillna(""))))
    else:
        _, listings_raw = _cache("openfigi_search")
    reject = set(ov.get("relations_reject") or [])
    rel_rows = []
    own_figis = set(sec["figi"].dropna()) | set(sec["composite_figi"].dropna())
    for iss in issuers.to_dict("records"):
        members = sec[sec["issuer_id"] == iss["issuer_id"]]
        classes = set(members["share_class_figi"].dropna())
        want = norm_name(iss["search_name"])
        seen = set()
        for d in (listings_raw.get(iss["issuer_id"]) or {}).get("data") or []:
            if "error" in d or d.get("compositeFIGI") in own_figis or d.get("figi") in own_figis:
                continue
            got = norm_name(d.get("name"))
            # OpenFIGI 名称最长约 28 个字符，长名称会被截断：截断的名称允许前缀匹配
            truncated = len(d.get("name") or "") >= 24 and len(got) >= 6 and want.startswith(got)
            if (got != want and not truncated) or d.get("shareClassFIGI") in reject or d.get("figi") in reject:
                continue
            k = (d.get("exchCode"), d.get("ticker"))
            if k in seen:
                continue
            seen.add(k)
            dr = d.get("securityType2") == "Depositary Receipt"
            ticker = d.get("ticker") or ""
            if d.get("exchCode") == "US" and not dr and len(ticker) == 5 and ticker.endswith("F"):
                continue        # 美国场外的外国普通股报价（五个字母、F 结尾），不是正式挂牌
            kind = ("depositary_receipt" if dr else "same_share_class" if d.get("shareClassFIGI") in classes
                    else "other_share_class")
            note = ("港币—人民币双柜台（人民币柜台）" if d.get("exchCode") == "HK" and len(ticker) == 5 and ticker.startswith("8")
                    else "场外交易的存托凭证" if dr and d.get("exchCode") == "US" and len(ticker) == 5 else "")
            rel_rows.append({"issuer_id": iss["issuer_id"], "venue": d.get("exchCode"),
                             "venue_zh": cfg["cross_listing_venues"].get(d.get("exchCode")), "ticker": d.get("ticker"),
                             "name": d.get("name"), "security_type": d.get("securityType2"), "figi": d.get("figi"),
                             "share_class_figi": d.get("shareClassFIGI"), "relation": kind,
                             "relation_zh": RELATION_ZH[kind], "in_universe": False, "status": "自动匹配，待确认",
                             "note": note})
        # 股票池内部：同一发行人在本平台多个市场（如汇丰在香港、伦敦）
        if len(members) > 1:
            for mm in members.to_dict("records"):
                same = (members["share_class_figi"] == mm["share_class_figi"]).sum() > 1
                kind = "same_share_class" if same else "other_share_class"
                rel_rows.append({"issuer_id": iss["issuer_id"], "venue": cfg["markets"][mm["market"]]["figi_exch"],
                                 "venue_zh": cfg["markets"][mm["market"]]["name"], "ticker": mm["symbol"],
                                 "name": mm["figi_name"], "security_type": mm["security_type"], "figi": mm["figi"],
                                 "share_class_figi": mm["share_class_figi"], "relation": kind,
                                 "relation_zh": RELATION_ZH[kind], "in_universe": True,
                                 "status": "股份类别 FIGI 一致" if same else "LEI 一致", "note": "均在本平台股票池"})
    for x in ov.get("relations_add") or []:     # 人工补录的关联（自动检索未覆盖的）
        kind = x["relation"]
        rel_rows.append({"issuer_id": sec.loc[sec["symbol"] == x["symbol"], "issuer_id"].iloc[0] if (sec["symbol"] == x["symbol"]).any() else None,
                         "venue": x["venue"], "venue_zh": cfg["cross_listing_venues"].get(x["venue"]), "ticker": x["ticker"],
                         "name": x.get("name"), "security_type": x.get("security_type"), "figi": x.get("figi"),
                         "share_class_figi": x.get("share_class_figi"), "relation": kind, "relation_zh": RELATION_ZH[kind],
                         "in_universe": False, "status": f"人工补录（{x.get('confirmed_by', '')} {x.get('confirmed_at', '')}）".strip(),
                         "note": x.get("note", "")})
    rel = pd.DataFrame(rel_rows, columns=["issuer_id", "venue", "venue_zh", "ticker", "name", "security_type", "figi",
                                          "share_class_figi", "relation", "relation_zh", "in_universe", "status", "note"])
    # 主要上市地：与注册地同一国家的唯一挂牌交易所
    prim = {}
    for iss in issuers.to_dict("records"):
        members = sec[sec["issuer_id"] == iss["issuer_id"]]
        venues = {cfg["markets"][m]["figi_exch"] for m in members["market"]}
        venues |= set(rel.loc[(rel["issuer_id"] == iss["issuer_id"]) & (rel["relation"] != "depositary_receipt"), "venue"])
        home = [v for v in venues if VENUE_COUNTRY.get(v) == iss["jurisdiction"]]
        prim[iss["issuer_id"]] = (home[0], "注册地所在交易所") if len(home) == 1 else \
            (next(iter(venues)), "唯一挂牌地") if len(venues) == 1 else (None, "待确认")
    sec["primary_listing"] = sec["issuer_id"].map(lambda i: prim[i][0])
    sec["primary_listing_basis"] = sec["issuer_id"].map(lambda i: prim[i][1])
    sec["is_primary"] = [p == cfg["markets"][m]["figi_exch"] if p else None
                         for p, m in zip(sec["primary_listing"], sec["market"])]
    for s, o in (ov.get("securities") or {}).items():       # 人工确认覆盖
        for k, v in o.items():
            if k in sec.columns:
                sec.loc[sec["symbol"] == s, k] = v
        sec.loc[sec["symbol"] == s, "primary_listing_basis"] = "人工确认"
    REF.mkdir(parents=True, exist_ok=True)
    sec.to_parquet(REF / "securities.parquet", index=False)
    rel.to_parquet(REF / "relations.parquet", index=False)
    summary = {"built_at": now(), "securities": len(sec), "issuers": int(sec["issuer_id"].nunique()),
               "with_lei": int(sec["lei"].notna().sum()), "with_figi": int(sec["figi"].notna().sum()),
               "with_isin": int(sec["isin"].notna().sum()),
               "relations": rel["relation"].value_counts().to_dict(),
               "multi_market_issuers": int((sec.groupby("issuer_id")["market"].nunique() > 1).sum()),
               "primary_unresolved": int(sec["primary_listing"].isna().sum()),
               "cross_listings_searched": bool(listings_raw), "markets_version": cfg["version"]}
    (REF / "master_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary
