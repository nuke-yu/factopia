"""参考数据域（refdata）：证券主数据、发行人挂牌关系、指数成分、公司行动。

四份数据，四张表：

| 文件（原唯一真相）                            | 表                        | 主键 / 幂等键                          |
|----------------------------------------------|---------------------------|----------------------------------------|
| `runtime/refdata/securities.parquet`          | `security_master`         | `symbol`                               |
| `runtime/refdata/relations.parquet`           | `security_relation`       | `(issuer_id, venue, ticker)`           |
| `runtime/refdata/corporate_actions.parquet`   | `corporate_action`        | `(symbol, ex_date, action)`            |
| `configs/universe/membership/<market>.csv`    | `index_membership`        | `(index_code, symbol, start_date)`     |

**为什么不是"列全部拆开"**：这几份表由开放数据源（Yahoo / OpenFIGI / GLEIF / AMF）拼出来，
列会随数据源改版增删。拆成列只用于"要查、要排、要约束"的字段，其余整行进 `payload`，
读回时 **payload 优先、列值只补 payload 缺失的键**——这样 `GET /api/refdata/securities`
在文件与库两种模式下能逐字节一致（见 `scripts/db/smoke_refdata.py`）。

**列名与文件字段不完全同名**是刻意的：文件里的 `type`（split / dividend …）在表里叫
`action`（`type` 是 SQL 保留字风险 + 语义更明确），文件里的 `date` 叫 `ex_date`。
映射只在 `_sec_row/_rel_row/_act_row/_mem_row` 四个函数里，别处不再重复判断。

**精度与 dtype**：JSON 不保 dtype（parquet 里 `float64` 读回来会变 `object`），
所以每次写完把列顺序与 dtype 记在 `app_state['refdata.meta.<name>']`，读回时按它还原。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

import pandas as pd
from psycopg.types.json import Jsonb

from factopia import db
from factopia.repo import misc as repo_misc

log = logging.getLogger(__name__)

DOMAIN = "refdata"

META_KEY = "refdata.meta.{name}"

# 文件列 → 表列（None 表示"只进 payload"）
SEC_COLS = {
    "symbol": "symbol", "name": "name", "long_name": "long_name", "market": "market",
    "currency": "currency", "lei": "lei", "figi": "figi", "primary_listing": "primary_listing",
    "industry": "industry", "fiscal_year_end_month": "fiscal_year_end_month",
    "accounting": "accounting_std", "issuer_id": "issuer_id",
}
REL_COLS = {"issuer_id": "issuer_id", "venue": "venue", "ticker": "ticker",
            "relation": "relation", "in_universe": "in_universe"}
ACT_COLS = {"symbol": "symbol", "type": "action", "date": "ex_date",
            "ratio": "ratio", "amount": "amount", "currency": "currency", "source": "source"}
MEM_COLS = {"index_code": "index_code", "symbol": "symbol", "start": "start_date",
            "end": "end_date", "source": "source", "note": "note"}


def _inverse(mapping: dict) -> dict:
    """表列 → 文件字段。读回时把列值换回文件里的名字（`accounting_std` → `accounting`）。"""
    return {dst: src for src, dst in mapping.items() if dst and dst != src}


INV_SEC, INV_REL = _inverse(SEC_COLS), _inverse(REL_COLS)
INV_ACT, INV_MEM = _inverse(ACT_COLS), _inverse(MEM_COLS)


# ── 小工具：NaN / Timestamp / numpy 标量 → 可入 JSONB 的纯 Python ─────────────────────

def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and value != value:        # NaN
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):                              # numpy 标量
        try:
            return _plain(value.item())
        except (ValueError, AttributeError):
            return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _payload(row: dict, mapped: dict) -> dict:
    """整行原样存，但不重复已经在列里的键（省空间，也让"列改了但 payload 没改"不会互相矛盾）。

    例外：**原值为空也要留下**。`issuer_id=None` / `ticker=None` 落成列会变成 `''`，
    读回时 `''` 与 `None` 不等；把 null 留在 payload 里，读回才能还原成 None
    （`GET /api/refdata/securities` 里 null 与空串是两种含义：未知 vs 空）。
    """
    used = {src for src, dst in mapped.items() if dst}
    return {k: _plain(v) for k, v in row.items() if k not in used or v is None}


def _records(df: pd.DataFrame) -> list[dict]:
    return [_plain(r) for r in df.to_dict("records")]


# ── 元数据（列顺序 + dtype） ─────────────────────────────────────────────────────────

def _write_meta(name: str, df: pd.DataFrame) -> None:
    repo_misc.save_state(META_KEY.format(name=name),
                         {"columns": list(df.columns), "dtypes": {c: str(t) for c, t in df.dtypes.items()}})


def _read_meta(name: str) -> dict:
    return repo_misc.state(META_KEY.format(name=name)) or {}


def _cast(df: pd.DataFrame, dtypes: dict) -> pd.DataFrame:
    """按写库时记下的 dtype 还原。还原不了的列保持原样（宁可 dtype 不同，也不要丢值）。"""
    for col, want in (dtypes or {}).items():
        if col not in df.columns:
            continue
        try:
            if want.startswith(("float", "int")):
                df[col] = pd.to_numeric(df[col], errors="coerce")
                if want.startswith("int") and not df[col].isna().any():
                    df[col] = df[col].astype("int64")
            elif want == "bool":
                df[col] = df[col].map(lambda v: None if v is None else bool(v)).astype("object")
            elif want.startswith("datetime"):
                df[col] = pd.to_datetime(df[col], errors="coerce")
        except (TypeError, ValueError):     # 脏值不求人，保持原样
            continue
    return df


def _frame(name: str, rows: list[dict]) -> pd.DataFrame:
    meta = _read_meta(name)
    cols = meta.get("columns") or None
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols or [])
    if cols:                                # 只保留文件时代的列，payload 里的新增键不外泄
        df = df[[c for c in cols if c in df.columns]]
    return _cast(df, meta.get("dtypes") or {})


def _prune(table: str, key_cols: list[str], keys: list[tuple]) -> None:
    """重建后删掉"文件里已经没有"的行。

    只在**全量重建**时调用：`build(symbols=[...])` 是局部刷新，按缺失删除会误伤其他标的。
    多列主键不能靠 `NOT IN (VALUES …)`（`unnest` 二维数组展开不了多列），
    改成把键集合作为 JSON 传进去，用 `jsonb_to_recordset` 展开成一张临时表。
    """
    if not keys:
        return
    with db.tx() as cur:
        if len(key_cols) == 1:
            cur.execute(f"DELETE FROM factopia.{table} WHERE {key_cols[0]} <> ALL(%s)",
                        ([k[0] for k in keys],))
            return
        defs = ", ".join(f"{c} text" for c in key_cols)
        on = " AND ".join(f"k.{c} = t.{c}" for c in key_cols)
        cur.execute(
            f"""DELETE FROM factopia.{table} AS t
                 WHERE NOT EXISTS (
                       SELECT 1 FROM jsonb_to_recordset(%s::jsonb) AS k({defs}) WHERE {on})""",
            (json.dumps([dict(zip(key_cols, k)) for k in keys], ensure_ascii=False),))


# ── securities ─────────────────────────────────────────────────────────────────────

def save_securities(df: pd.DataFrame, prune: bool = False) -> int:
    """整表刷新。`prune=True` 时删除文件里已不存在的 symbol（仅全量重建可用）。"""
    rows = _records(df)
    with db.tx() as cur:
        cur.cursor().executemany(
            """INSERT INTO factopia.security_master
                   (symbol, name, long_name, market, currency, lei, figi, primary_listing, industry,
                    fiscal_year_end_month, accounting_std, is_depositary, issuer_id, payload, ord)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol) DO UPDATE SET
                   name = EXCLUDED.name, long_name = EXCLUDED.long_name, market = EXCLUDED.market,
                   currency = EXCLUDED.currency, lei = EXCLUDED.lei, figi = EXCLUDED.figi,
                   primary_listing = EXCLUDED.primary_listing, industry = EXCLUDED.industry,
                   fiscal_year_end_month = EXCLUDED.fiscal_year_end_month,
                   accounting_std = EXCLUDED.accounting_std, is_depositary = EXCLUDED.is_depositary,
                   issuer_id = EXCLUDED.issuer_id, payload = EXCLUDED.payload, ord = EXCLUDED.ord""",
            [(r.get("symbol"), r.get("name"), r.get("long_name"), r.get("market"), r.get("currency"),
              r.get("lei"), r.get("figi"), r.get("primary_listing"), r.get("industry"),
              r.get("fiscal_year_end_month"), r.get("accounting"), r.get("security_type") == "Depositary Receipt",
              r.get("issuer_id"), Jsonb(_payload(r, SEC_COLS)), i)
             for i, r in enumerate(rows) if r.get("symbol")])
    _write_meta("securities", df)
    if prune:
        _prune("security_master", ["symbol"], [(r["symbol"],) for r in rows if r.get("symbol")])
    return len(rows)


def load_securities() -> pd.DataFrame:
    cols = ("symbol", "name", "long_name", "market", "currency", "lei", "figi", "primary_listing",
            "industry", "fiscal_year_end_month", "accounting_std", "is_depositary", "issuer_id")
    out = []
    for row in db.query(f"SELECT {', '.join(cols)}, payload FROM factopia.security_master "
                        "ORDER BY ord NULLS LAST, symbol"):
        r = dict(zip(cols + ("payload",), row))
        d = dict(r.pop("payload") or {})          # payload 是逐字真相
        for col, v in r.items():                  # 列只补 payload 缺失的键，且换回文件里的名字
            if v is not None:
                d.setdefault(INV_SEC.get(col, col), v)
        if d.get("security_type") is None and r["is_depositary"]:
            d["security_type"] = "Depositary Receipt"
        out.append(d)
    return _frame("securities", out)


def count_securities() -> int:
    return int((db.query_one("SELECT count(*) FROM factopia.security_master") or (0,))[0])


# ── relations ──────────────────────────────────────────────────────────────────────

def save_relations(df: pd.DataFrame, prune: bool = False) -> int:
    rows = _records(df)
    with db.tx() as cur:
        cur.cursor().executemany(
            """INSERT INTO factopia.security_relation
                   (issuer_id, venue, ticker, relation, in_universe, payload, ord)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (issuer_id, venue, ticker) DO UPDATE SET
                   relation = EXCLUDED.relation, in_universe = EXCLUDED.in_universe,
                   payload = EXCLUDED.payload, ord = EXCLUDED.ord""",
            [(r.get("issuer_id") or "", r.get("venue") or "", r.get("ticker") or "",
              r.get("relation"), r.get("in_universe"), Jsonb(_payload(r, REL_COLS)), i)
             for i, r in enumerate(rows)])
    _write_meta("relations", df)
    if prune:
        _prune("security_relation", ["issuer_id", "venue", "ticker"],
               [(r.get("issuer_id") or "", r.get("venue") or "", r.get("ticker") or "") for r in rows])
    return len(rows)


def load_relations() -> pd.DataFrame:
    cols = ("issuer_id", "venue", "ticker", "relation", "in_universe", "payload")
    out = []
    for row in db.query(f"SELECT {', '.join(cols)} FROM factopia.security_relation "
                        "ORDER BY ord NULLS LAST, issuer_id, venue, ticker"):
        r = dict(zip(cols, row))
        d = dict(r.pop("payload") or {})
        for k, v in r.items():
            if v is not None:
                d.setdefault(INV_REL.get(k, k), v)
        out.append(d)
    return _frame("relations", out)


def count_relations() -> int:
    return int((db.query_one("SELECT count(*) FROM factopia.security_relation") or (0,))[0])


# ── corporate_actions ─────────────────────────────────────────────────────────────

def save_corporate_actions(df: pd.DataFrame, prune: bool = False) -> int:
    """`value` 一列两义（拆分是比例、分红是金额），表里拆成 ratio / amount 两列便于查询，
    但 `value` 原值留在 payload 里，读回时以 payload 为准。"""
    rows = _records(df)
    with db.tx() as cur:
        cur.cursor().executemany(
            """INSERT INTO factopia.corporate_action
                   (symbol, action, ex_date, ratio, amount, currency, source, payload, ord)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol, ex_date, action) DO UPDATE SET
                   ratio = EXCLUDED.ratio, amount = EXCLUDED.amount, currency = EXCLUDED.currency,
                   source = EXCLUDED.source, payload = EXCLUDED.payload, ord = EXCLUDED.ord""",
            [(r.get("symbol"), r.get("type"), r.get("date"),
              r.get("value") if r.get("type") == "split" else None,
              r.get("value") if r.get("type") != "split" else None,
              r.get("currency"), r.get("source"), Jsonb(_payload(r, ACT_COLS)), i)
             for i, r in enumerate(rows) if r.get("symbol") and r.get("date") and r.get("type")])
    _write_meta("corporate_actions", df)
    if prune:
        _prune("corporate_action", ["symbol"],
               list({(r["symbol"],) for r in rows if r.get("symbol")}))
    return len(rows)


def load_corporate_actions() -> pd.DataFrame:
    cols = ("symbol", "action", "ex_date", "ratio", "amount", "currency", "source", "payload")
    out = []
    for row in db.query(f"SELECT {', '.join(cols)} FROM factopia.corporate_action "
                        "ORDER BY ord NULLS LAST, symbol, ex_date"):
        r = dict(zip(cols, row))
        d = dict(r.pop("payload") or {})
        for k, v in r.items():
            if v is None:
                continue
            if hasattr(v, "isoformat"):            # date → ISO 字符串（文件里就是字符串）
                v = v.isoformat()
            elif k in ("ratio", "amount"):
                v = float(v)
            d.setdefault(INV_ACT.get(k, k), v)
        out.append(d)
    return _frame("corporate_actions", out)


def count_corporate_actions() -> int:
    return int((db.query_one("SELECT count(*) FROM factopia.corporate_action") or (0,))[0])


# ── index_membership（成分股） ─────────────────────────────────────────────────────

def save_membership(market: str, df: pd.DataFrame) -> int:
    """按市场整块替换：一个市场的名单只来自一份 CSV，不存在"局部更新"。
    空 start（CSV 里是 ""）落成 NULL —— 与原 `load()` 返回 "" 的差异在读回时补回。"""
    rows = [{**r, "index_code": market} for r in _records(df)]
    keys = []
    with db.tx() as cur:
        cur.execute("DELETE FROM factopia.index_membership WHERE index_code = %s", (market,))
        cur.cursor().executemany(
            """INSERT INTO factopia.index_membership
                   (index_code, symbol, start_date, end_date, source, note, ord)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (index_code, symbol, start_date) DO UPDATE SET
                   end_date = EXCLUDED.end_date, source = EXCLUDED.source, note = EXCLUDED.note,
                   ord = EXCLUDED.ord""",
            [(market, r.get("symbol"), r.get("start") or None, r.get("end") or None,
              r.get("source"), r.get("note"), i) for i, r in enumerate(rows)])
    _write_meta(f"membership.{market}", df)
    return len(rows)


def load_membership(market: str) -> pd.DataFrame | None:
    """`None` = 这个市场根本没有名单（与文件时代 `load()` 的语义一致）。"""
    q = ("SELECT symbol, start_date, end_date, source, note FROM factopia.index_membership "
         "WHERE index_code = %s ORDER BY ord NULLS LAST, symbol, start_date")
    rows = db.query(q, (market,))
    if not rows:
        return None
    out = [{"symbol": s, "start": st.isoformat() if st else "",
            "end": en.isoformat() if en else "", "source": src or "", "note": note or ""}
           for s, st, en, src, note in rows]
    return _frame(f"membership.{market}", out)


def count_membership(market: str) -> int:
    return int((db.query_one("SELECT count(*) FROM factopia.index_membership WHERE index_code = %s",
                             (market,)) or (0,))[0])


def markets_with_membership() -> list[str]:
    return [r[0] for r in db.query(
        "SELECT DISTINCT index_code FROM factopia.index_membership ORDER BY index_code")]
