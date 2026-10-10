"""① 采集：法国 AMF 开放数据（info-financière，Licence Ouverte 2.0）中 CAC 40 公司的公告（BR-08、BR-15）。

- 股票池：configs/universe/fr.csv；按交易代码在 AMF 数据中查出 ISIN（同一代码取出现次数最多的 ISIN），
  结果写入 isin_map.csv 供人工核对；数据集自带的 cac40 标记是历史名单，不使用。
- 公告：默认只取可与上一期比较的定期公告（年报、半年报、季度财务信息）；内幕信息公告可选；按 ISIN 与发布时间筛选。
- 时间：published_at = 发往市场的时间（uin_dat_mar，UTC）；实际可获取时间与生效交易日由 signal 模块按
  处理延迟和交易所日历计算。
- 原文优先：保存原始 PDF（raw/<id>.pdf）与抽取出的段落（docs/<id>.json），译文另存，从不覆盖原文。
"""
import http.client
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from factopia.text.common import CONFIGS, DATASETS, ds_dir, read_json, sha, write_json

API = "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records"
UA = {"User-Agent": "factopia-poc (research; Licence Ouverte 2.0 open data)"}
FIELDS = {
    "uin_idt_uin": "id", "identificationsociete_iso_cd_isi": "isin", "identificationsociete_iso_nom_soc": "company",
    "uin_dat_mar": "published_at", "informationdeposee_inf_dat_emt": "transmitted_at",
    "informationdeposee_inf_tit_inf": "title", "informationdeposee_inf_lng_inf": "language",
    "type_d_information": "type_fr", "sous_type_d_information": "subtype_fr",
    "type_of_information": "type_en", "subtype_of_information": "subtype_en", "url_de_recuperation": "url",
}
LANG = {"Français": "fr", "Anglais": "en"}
# 管理层表述集中、且可与上一期比较的定期公告（AMF 字段 sous_type_d_information）；内幕信息公告数量大（单家公司每年上百篇），
# 只在 include_inside=True 时采集
PERIODIC = ("Rapports financiers et d'audit annuels", "Rapports financiers et d'audit semestriels/examens réduits",
            "Information financière trimestrielle")
INSIDE = ("Informations privilégiées",)
MAX_PAGES = 40          # 年报很长，只取前 40 页（管理层报告与风险章节通常在前部）
MIN_PARA, MAX_PARA = 60, 1500


def _get(params: dict, retries: int = 6) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    for k in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:      # 查询写错（400 等）：不重试，带上接口返回的原因
                raise RuntimeError(f"AMF 接口返回 {e.code}：{e.read()[:300].decode(errors='replace')}") from e
            if k == retries - 1:
                raise RuntimeError(f"AMF 接口请求失败：{e}") from e
            time.sleep(3 * (k + 1))
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            if k == retries - 1:
                raise RuntimeError(f"AMF 接口请求失败：{e}") from e
            time.sleep(3 * (k + 1))


def resolve_isins(symbols: list[str] | None = None) -> pd.DataFrame:
    """股票池代码 → ISIN（按 AMF 数据中的交易代码统计）；只查尚未解析的代码，逐个写入，可中断后继续。"""
    uni = pd.read_csv(CONFIGS / "universe" / "fr.csv")
    path = ds_dir("fr") / "isin_map.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    known = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["instrument", "name", "ticker", "isin", "n"])
    for sym, name in zip(uni["symbol"], uni["name"]):
        if (symbols and sym not in symbols) or sym in set(known["instrument"]):
            continue
        ticker = sym.split(".")[0]
        isin = "identificationsociete_iso_cd_isi"
        r = _get({"select": f"{isin}, count(*) as n", "where": f'identificationsociete_iso_code_tkr_iso_cd_tkr="{ticker}"',
                  "group_by": isin, "order_by": "n desc", "limit": 3})
        hit = next((x for x in r.get("results", []) if x.get(isin)), None)
        row = {"instrument": sym, "name": name, "ticker": ticker, "isin": hit[isin] if hit else None, "n": hit["n"] if hit else 0}
        known = pd.concat([known, pd.DataFrame([row])], ignore_index=True)
        known.to_csv(path, index=False)
        print(f"{sym} → {row['isin']}", flush=True)
    order = {s: k for k, s in enumerate(uni["symbol"])}
    return known.sort_values("instrument", key=lambda c: c.map(order)).reset_index(drop=True)


def _fix_times(f: dict) -> dict:
    """发布时间取有效时间中较晚的一个（保守，避免提前使用）；部分记录的发往市场时间是占位值 8888-01-01。"""
    valid = [t for t in (f["published_at"], f["transmitted_at"]) if t and "1990" <= t[:4] <= "2100"]
    note = None if f["published_at"] in valid else "发往市场时间缺失或为占位值，改用传输时间"
    f["published_at"] = max(valid) if valid else None
    f["time_note"] = note
    return f


def list_filings(isin: str, start: str, end: str | None, include_inside: bool = False) -> list[dict]:
    where = [f'identificationsociete_iso_cd_isi="{isin}"', f'informationdeposee_inf_dat_emt>="{start}"']
    if end:
        where.append(f'informationdeposee_inf_dat_emt<"{end}"')
    kinds = PERIODIC + (INSIDE if include_inside else ())
    types = " or ".join(f'sous_type_d_information="{t}"' for t in kinds)
    where.append(f"({types})")
    out, offset = [], 0
    while offset < 9900:       # 接口单次查询最多翻到 10000 条
        r = _get({"select": ",".join(FIELDS), "where": " and ".join(where), "order_by": "informationdeposee_inf_dat_emt",
                  "limit": 100, "offset": offset})
        batch = r.get("results", [])
        out += [_fix_times({FIELDS[k]: x.get(k) for k in FIELDS}) for x in batch]
        if len(batch) < 100:
            break
        offset += 100
    return out


def paragraphs(text: str) -> list[str]:
    """PDF 文本 → 段落：合并断行，按空行或句末换行切分，过滤页眉页脚等短行。"""
    text = text.replace("\r", "").replace(" ", " ")
    text = re.sub(r"-\n(?=[a-zà-ÿ])", "", text)                 # 断词连字符
    blocks = re.split(r"\n\s*\n|(?<=[.!?:;»])\s*\n(?=[A-ZÀ-Ý«•\-–])", text)
    out = []
    for b in blocks:
        p = re.sub(r"\s+", " ", b).strip()
        while len(p) > MAX_PARA:                                  # 过长的块按句子切开
            cut = p.rfind(". ", 0, MAX_PARA)
            cut = cut + 1 if cut > MIN_PARA else MAX_PARA
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(p) >= MIN_PARA and sum(ch.isalpha() for ch in p) > 0.5 * len(p):
            out.append(p)
    return out


def _download(url: str, retries: int = 6) -> bytes:
    """分块下载；连接中断时从已收到的位置续传（Range），多次重试。"""
    data = b""
    for k in range(retries):
        headers = dict(UA)
        if data:
            headers["Range"] = f"bytes={len(data)}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                if data and r.status != 206:     # 服务器不支持续传：从头下载
                    data = b""
                total = len(data) + int(r.headers.get("Content-Length") or 0)
                while chunk := r.read(1 << 16):
                    data += chunk
                if not total or len(data) >= total:
                    return data
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            pass
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"下载未完成（已收到 {len(data)} 字节）")


def fetch_doc(filing: dict) -> dict:
    from pypdf import PdfReader

    d = ds_dir("fr")
    raw = d / "raw" / f"{filing['id']}.pdf"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(_download(filing["url"]))
    reader = PdfReader(io.BytesIO(raw.read_bytes()))
    pages = reader.pages[:MAX_PAGES]
    text = "\n\n".join((p.extract_text() or "") for p in pages)
    paras = paragraphs(text)
    return {"id": filing["id"], "pages_total": len(reader.pages), "pages_read": len(pages),
            "paragraphs": [{"i": i, "text": p, "sha": sha(p)} for i, p in enumerate(paras)]}


def collect(companies: int | None = None, start: str = "2019-01-01", end: str | None = None,
            max_per_company: int | None = None, only: list[str] | None = None,
            include_inside: bool = False) -> pd.DataFrame:
    """采集元数据与原文；已下载的不重复下载（可中断后继续）。"""
    d = ds_dir("fr")
    cfg = DATASETS["fr"]
    uni = pd.read_csv(CONFIGS / "universe" / "fr.csv")["symbol"].tolist()
    wanted = only or (uni[:companies] if companies else uni)
    isins = resolve_isins(wanted)
    isins = isins[isins["instrument"].isin(wanted)].dropna(subset=["isin"])
    meta_path = d / "filings.parquet"
    old = pd.read_parquet(meta_path) if meta_path.exists() else pd.DataFrame()
    rows = []
    for inst, isin, name in zip(isins["instrument"], isins["isin"], isins["name"]):
        filings = [f for f in list_filings(isin, start, end, include_inside) if f["published_at"]]
        if max_per_company:
            filings = filings[-max_per_company:]            # 最近的若干篇
        print(f"{inst} {name} {isin}: {len(filings)} 篇", flush=True)
        for f in filings:
            doc_path = d / "docs" / f"{f['id']}.json"
            if not doc_path.exists():
                try:
                    write_json(doc_path, fetch_doc(f))
                except Exception as e:  # 个别 PDF 损坏或无法抽取文本：记录后跳过
                    print(f"  跳过 {f['id']}：{e}", flush=True)
                    continue
            doc = read_json(doc_path)
            rows.append({**f, "instrument": inst, "language": LANG.get(f["language"], f["language"]),
                         "n_paragraphs": len(doc["paragraphs"]), "source": cfg["source"], "licence": cfg["licence"],
                         "simulated": False})
    new = pd.DataFrame(rows)
    meta = pd.concat([old, new]).drop_duplicates("id", keep="last") if len(old) else new
    meta = meta.sort_values("published_at").reset_index(drop=True)
    meta.to_parquet(meta_path, index=False)
    return meta
