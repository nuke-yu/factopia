"""refdata 域接表后的验收：四张表必须与 parquet/CSV 逐值一致，且 db 模式不回落文件。

三个阶段：

1. **往返**：用与 `refdata/master.py` 完全相同的列结构造夹具 → 写库 → 读回 → 逐值比对；
   再重跑一次，证明幂等（行数不变、`ord` 行序不变）。
2. **不回落**：把 `blobstore.REFDATA` 指到一个空目录，db 模式下
   `/api/refdata/securities`、`/api/refdata/actions`、`blobstore.membership()` 仍要返回同一份数据。
   这一阶段是"切了就是切了"的证明——只要还有一处读 parquet 或 CSV，就会与阶段 1 对不上。
3. **清理**：删掉本脚本写入的夹具与元数据，不许留下任何痕迹。

跑法（需要隧道，且 .env 已配好 PG）：

    python scripts/db/smoke_refdata.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("FACTOPIA_RUNTIME", str(ROOT / "runtime"))
os.environ.setdefault("FACTOPIA_CONFIGS_DIR", str(ROOT / "configs"))
os.environ.setdefault("FACTOPIA_STORAGE", "db")
os.environ["FACTOPIA_STORAGE_REFDATA"] = "db"

import pandas as pd  # noqa: E402

from factopia import blobstore, db  # noqa: E402
from factopia.repo import misc as repo_misc  # noqa: E402
from factopia.repo import refdata as R  # noqa: E402

PASS, FAIL = [], []

# 夹具用 T 前缀的假代码：字段组合取真实数据里最难的三种（缺值 None、布尔 None、跨市场同一发行人），
# 但代码是假的，清理时只删这些行——否则会误删真实迁移进库的证券与成分股
SYMBOLS = ("T0005.HK", "TSBA.L")
ISSUER = "TEST:ISSUER"

SEC = pd.DataFrame([
    {"symbol": "T0005.HK", "market": "hk", "name": "汇丰控股", "long_name": "HSBC Holdings plc",
     "exchange": "HKEX", "currency": "HKD", "figi": "BBG000X", "composite_figi": "BBG000Y",
     "share_class_figi": "BBG000Z", "figi_name": "HSBC HOLDINGS", "security_type": "Common Stock",
     "isin": None, "lei": "TESTLEI0001", "legal_name": "HSBC Holdings plc", "lei_method": "name_match",
     "jurisdiction": "GB", "legal_country": "GB", "sector": "Financials", "industry": "Banks",
     "hq_country": "GB", "reporting_currency": "USD", "fiscal_year_end_month": 12,
     "accounting": "IFRS", "accounting_basis": "按注册地推断", "reporting": "半年度",
     "snapshot_at": "2026-11-01T00:00:00", "issuer_id": "TEST:ISSUER", "search_name": "HSBC HOLDINGS",
     "primary_listing": "LN", "primary_listing_basis": "注册地所在交易所", "is_primary": False},
    {"symbol": "TSBA.L", "market": "uk", "name": "HSBC", "long_name": None,
     "exchange": "LSE", "currency": "GBP", "figi": None, "composite_figi": None,
     "share_class_figi": "BBG000Z", "figi_name": "HSBC HOLDINGS", "security_type": "Depositary Receipt",
     "isin": "GB0005405286", "lei": "TESTLEI0001", "legal_name": "HSBC Holdings plc", "lei_method": "isin",
     "jurisdiction": "GB", "legal_country": "GB", "sector": "Financials", "industry": "Banks",
     "hq_country": "GB", "reporting_currency": "USD", "fiscal_year_end_month": None,
     "accounting": "IFRS", "accounting_basis": "市场默认值", "reporting": "半年度",
     "snapshot_at": "2026-11-01T00:00:00", "issuer_id": "TEST:ISSUER", "search_name": "HSBC HOLDINGS",
     "primary_listing": None, "primary_listing_basis": "待确认", "is_primary": None},
])

REL = pd.DataFrame([
    {"issuer_id": "TEST:ISSUER", "venue": "LN", "venue_zh": "伦敦", "ticker": "HSBA",
     "name": "HSBC HOLDINGS", "security_type": "Common Stock", "figi": "BBG1",
     "share_class_figi": "BBG000Z", "relation": "same_share_class", "relation_zh": "同一股份多地上市",
     "in_universe": True, "status": "股份类别 FIGI 一致", "note": "均在本平台股票池"},
    {"issuer_id": None, "venue": "US", "venue_zh": "美国", "ticker": None, "name": "HSBC ADR",
     "security_type": "Depositary Receipt", "figi": "BBG2", "share_class_figi": None,
     "relation": "depositary_receipt", "relation_zh": "存托凭证", "in_universe": False,
     "status": "人工补录（）", "note": ""},
], columns=["issuer_id", "venue", "venue_zh", "ticker", "name", "security_type", "figi",
            "share_class_figi", "relation", "relation_zh", "in_universe", "status", "note"])

ACT = pd.DataFrame([
    {"symbol": "T0005.HK", "market": "hk", "date": "2026-03-12", "type": "dividend", "value": 0.32,
     "detail": "每股 0.3200", "source": "Yahoo Finance", "type_zh": "分红"},
    {"symbol": "T0005.HK", "market": "hk", "date": "2026-06-01", "type": "split", "value": 2.0,
     "detail": "比例 2", "source": "Yahoo Finance", "type_zh": "拆股"},
], columns=["symbol", "market", "date", "type", "value", "detail", "source", "type_zh"])

# 成分股夹具用假市场代码 zz：`save_membership` 是"整块替换一个市场"，
# 用 hk 会把真实成分股名单清掉（这正是第一次跑验收时踩到的坑）
MEM_MARKET = "zz"
MEM = pd.DataFrame([
    {"symbol": "T0700.HK", "start": "2020-01-02", "end": "", "source": "current_list",
     "note": "当前成分股，非历史名单"},
    {"symbol": "T0005.HK", "start": "2019-05-06", "end": "", "source": "current_list",
     "note": "当前成分股，非历史名单"},
])


def check(name: str, ok: bool, note: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'} {name}{('  ' + note) if note else ''}")


def same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """按"接口会返回的样子"比：`to_json` 之后再比（等价于 API 的 `_records()`）。"""
    return (json.loads(a.to_json(orient="records", force_ascii=False))
            == json.loads(b.to_json(orient="records", force_ascii=False)))


def first_diff(a: pd.DataFrame, b: pd.DataFrame) -> str:
    x = json.loads(a.to_json(orient="records", force_ascii=False))
    y = json.loads(b.to_json(orient="records", force_ascii=False))
    if len(x) != len(y):
        return f"行数 文件 {len(x)} / 库 {len(y)}"
    for i, (r1, r2) in enumerate(zip(x, y)):
        for k in r1:
            if r1.get(k) != r2.get(k):
                return f"第 {i + 1} 行 {k}：{r1.get(k)!r} / {r2.get(k)!r}"
    return "列集合不同"


def cleanup() -> None:
    """只删本脚本造的行：按夹具主键精确删除，不动别的数据。"""
    with db.tx() as cur:
        for sym in SYMBOLS:
            cur.execute("DELETE FROM factopia.security_master WHERE symbol = %s", (sym,))
        cur.execute("DELETE FROM factopia.security_relation WHERE issuer_id IN (%s, '')", (ISSUER,))
        cur.execute("DELETE FROM factopia.corporate_action WHERE symbol = ANY(%s)", (list(SYMBOLS),))
        # 只删夹具的两行成分股：按 (index_code, symbol) 精确删除，别清整个市场
        cur.execute("DELETE FROM factopia.index_membership WHERE index_code = %s", (MEM_MARKET,))
    for name in ("securities", "relations", "corporate_actions", f"membership.{MEM_MARKET}"):
        repo_misc.drop_state(R.META_KEY.format(name=name))


def main() -> int:
    health = db.health()
    if not health.get("ok"):
        print(f"数据库不可用：{health.get('error')}")
        return 2
    print(f"库 {health['database']} / {health['size']}；refdata 模式 = {db.mode('refdata')}\n")

    cleanup()          # 上一次失败留下的残留先清掉，保证本轮从零开始

    print("阶段 1：写入与读回")
    R.save_securities(SEC, prune=True)
    R.save_relations(REL, prune=True)
    R.save_corporate_actions(ACT, prune=True)
    R.save_membership(MEM_MARKET, MEM)
    got = {"securities": R.load_securities(), "relations": R.load_relations(),
           "corporate_actions": R.load_corporate_actions()}
    for name, want in (("securities", SEC), ("relations", REL), ("corporate_actions", ACT)):
        ok = same(want, got[name])
        check(f"{name} 往返逐值一致", ok, "" if ok else first_diff(want, got[name]))
    mem_got = R.load_membership(MEM_MARKET)
    ok = mem_got is not None and same(MEM, mem_got)
    check("membership 往返逐值一致", ok, "" if ok else (first_diff(MEM, mem_got) if mem_got is not None else "库中无数据"))

    counts = (R.count_securities(), R.count_relations(), R.count_corporate_actions(),
              R.count_membership(MEM_MARKET))
    R.save_securities(SEC, prune=True)
    R.save_relations(REL, prune=True)
    R.save_corporate_actions(ACT, prune=True)
    R.save_membership(MEM_MARKET, MEM)
    counts2 = (R.count_securities(), R.count_relations(), R.count_corporate_actions(),
               R.count_membership(MEM_MARKET))
    check("重跑幂等（行数不变）", counts == counts2, f"{counts} → {counts2}")
    check("重跑后行序不变", same(SEC, R.load_securities()) and same(REL, R.load_relations()))

    print("\n阶段 2：db 模式不回落文件（把 refdata 目录指到空目录）")
    empty = Path(tempfile.mkdtemp(prefix="factopia_refdata_empty_"))
    old = blobstore.REFDATA
    blobstore.REFDATA = empty
    try:
        check("refdata 目录为空时 securities 仍可读", same(SEC, blobstore.refdata_table("securities")))
        check("refdata 目录为空时 relations 仍可读", same(REL, blobstore.refdata_table("relations")))
        check("refdata 目录为空时公司行动仍可读", same(ACT, blobstore.refdata_table("corporate_actions")))
        check("has_refdata 由库判断", blobstore.has_refdata("securities") and blobstore.has_refdata("corporate_actions"))
        check("membership 由库读取", same(MEM, blobstore.membership(MEM_MARKET)))
    finally:
        blobstore.REFDATA = old

    print("\n阶段 3：接口层（db 模式 + 空 refdata 目录）")
    from fastapi.testclient import TestClient
    from factopia.api import main as api_main
    empty2 = Path(tempfile.mkdtemp(prefix="factopia_refdata_empty_api_"))
    empty_cfg = Path(tempfile.mkdtemp(prefix="factopia_configs_empty_api_"))
    old2 = api_main.blobstore.REFDATA
    api_main.blobstore.REFDATA = empty2
    try:
        client = TestClient(api_main.app)
        r = client.get("/api/refdata/securities")
        body = r.json() if r.status_code == 200 else {}
        secs = body.get("securities") or []
        got_syms = {x["symbol"] for x in secs}
        check("/api/refdata/securities 200 且含夹具", r.status_code == 200 and got_syms == set(SYMBOLS),
              f"{r.status_code} / {sorted(got_syms)}")
        rel_rows = body.get("relations") or []
        check("/api/refdata/securities 带出挂牌关系", len(rel_rows) >= 1, f"{len(rel_rows)} 条")
        r = client.get("/api/refdata/actions")
        acts = (r.json().get("items") if r.status_code == 200 else []) or []
        check("/api/refdata/actions 200 且条目数一致", r.status_code == 200 and len(acts) == len(ACT),
              f"{r.status_code} / {len(acts)} 条")
        # 字典页的成分股统计必须来自库：把成分股目录指到空目录，
        # 任何一处回落 CSV 都会让 rows 变 0（fields.yaml 仍从真实 configs 读，不受影响）
        from factopia.refdata import membership as mem_mod
        old_mdir = mem_mod.MDIR
        mem_mod.MDIR = empty_cfg
        try:
            r = client.get("/api/refdata/dictionary")
            mem_info = ((r.json() if r.status_code == 200 else {}).get("membership") or {}).get("hk") or {}
            check("/api/refdata/dictionary 的成分股来自库",
                  r.status_code == 200 and mem_info.get("rows") == R.count_membership("hk")
                  and mem_info.get("rows"), f"{mem_info}")
        finally:
            mem_mod.MDIR = old_mdir
    finally:
        api_main.blobstore.REFDATA = old2

    print("\n清理夹具 …")
    cleanup()
    left = (R.count_securities(), R.count_relations(), R.count_corporate_actions(), R.count_membership(MEM_MARKET))
    check("零残留（真实成分股未被测试影响）", left == (0, 0, 0, 0) and R.count_membership("hk") > 0,
          f"残留 {left}，真实 hk 名单 {R.count_membership('hk')} 行")

    print(f"\n结论：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    db.close_pool()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
