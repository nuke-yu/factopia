"""行情与汇率（`daily_bar` / `fx_rate`）：批量写入走 COPY，读取按列裁剪。

这两张表是**唯一进"大体量"方向的表**：三个市场 × 数年 × 数千只股票，
逐行 `executemany` 会慢到不可用（每行一次往返）。所以写入路径只有一条：

    DataFrame → 临时表（COPY 文本格式，一次一个批次）→ INSERT … ON CONFLICT DO UPDATE

COPY 不支持 UPSERT，所以必须借临时表中转。临时表用 `LIKE … INCLUDING DEFAULTS`
（不带分区），`ON COMMIT DROP` 由事务结束时清掉。

三个容易踩的点：

1. **`market` 大小写**。`standardize` 产出的 parquet 里 `market` 是 `HK`，
   而 `daily_bar` 是 LIST 分区、只接受 `hk`/`uk`/`fr`——写库前必须 `lower()`，
   否则直接报 "no partition of relation found"，而不是静默丢行。
2. **`date` 的类型**。pandas 是 `datetime64[ns]`，psycopg 会当 timestamp 发过去，
   与 `date` 列比较时可能出现时区/时间部分问题；统一 `strftime('%Y-%m-%d')` 落文本再由 PG 转换。
3. **空字符串 ≠ NULL**。`dq_flag` 的空串表示"没有标记"，不是"未知"。
   COPY 文本格式里 NULL 只能写成 `\\N`，所以空串按原样写出（两个制表符之间什么都没有），
   读回时才不会把"正常"变成"未知"。

读侧只做一件事：按 `columns` 裁剪 + 类型还原，让调用方拿到的 DataFrame
与文件时代形状一致（`date` 是 `datetime64`，数值是 `float64`）。
"""
from __future__ import annotations

import io
import logging
import math
from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd

from factopia import db

log = logging.getLogger(__name__)

DOMAIN = "bars"

# 与 `data/standardize.py` 的输出列顺序一致；表里没有的列（`ret`）不在此列
COLUMNS = ("symbol", "market", "date", "ts_utc", "available_at", "open", "high", "low", "close",
           "volume", "currency", "quote_currency", "dq_flag")
NUMERIC = ("open", "high", "low", "close", "volume")
TIMESTAMPS = ("ts_utc", "available_at")
BATCH = 200_000


def _cell(value: Any) -> str:
    """COPY 文本格式的单元格。`\\N` 是 NULL，其余字符按 PG 规则转义。"""
    if value is None:
        return "\\N"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "\\N"
    if value is pd.NaT:
        return "\\N"
    if isinstance(value, (pd.Timestamp, datetime)):
        if value.tzinfo is not None:
            value = value.tz_convert("UTC").tz_localize(None)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if text == "":
        return ""                      # 空串不是 NULL
    return (text.replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))


def _copy_text(rows: Iterable[tuple]) -> str:
    buf = io.StringIO()
    for row in rows:
        buf.write("\t".join(_cell(v) for v in row))
        buf.write("\n")
    return buf.getvalue()


def _prepare(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """把 parquet 里的形状整理成表要的形状：列缺失补 NULL、market 小写、date 去时间。"""
    out = df.copy()
    for col in COLUMNS:
        if col not in out.columns:
            out[col] = None
    out["market"] = market.lower()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    for col in TIMESTAMPS:
        out[col] = pd.to_datetime(out[col], errors="coerce", utc=True)
    for col in NUMERIC:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[list(COLUMNS)]


# ── 写入 ────────────────────────────────────────────────────────────────────────────

def save_bars(market: str, df: pd.DataFrame) -> int:
    """整批写入（幂等：同 (market, symbol, date) 覆盖）。返回写入行数。"""
    if df is None or df.empty:
        return 0
    data = _prepare(df, market)
    total = 0
    for start in range(0, len(data), BATCH):
        chunk = data.iloc[start:start + BATCH]
        with db.tx() as cur:
            cur.execute("""CREATE TEMP TABLE _bar_tmp (LIKE factopia.daily_bar INCLUDING DEFAULTS)
                           ON COMMIT DROP""")
            # psycopg3 的 copy() 在 Cursor 上，不在 Connection 上
            with cur.cursor() as c:
                with c.copy("COPY _bar_tmp (" + ", ".join(COLUMNS) + ") FROM STDIN") as copy:
                    copy.write(_copy_text(chunk.itertuples(index=False, name=None)))
            cur.execute(f"""
                INSERT INTO factopia.daily_bar ({', '.join(COLUMNS)})
                SELECT {', '.join(COLUMNS)} FROM _bar_tmp
                ON CONFLICT (market, symbol, date) DO UPDATE SET
                    {', '.join(f'{c} = EXCLUDED.{c}' for c in COLUMNS if c not in ('market', 'symbol', 'date'))}
            """)
        total += len(chunk)
    return total


def save_fx(df: pd.DataFrame) -> int:
    """汇率（`data/raw/fx.parquet`：symbol/pair、date、close）。"""
    if df is None or df.empty:
        return 0
    work = df.copy()
    pair_col = "pair" if "pair" in work.columns else "symbol"
    out = pd.DataFrame({"pair": work[pair_col].astype(str),
                        "date": pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d"),
                        "close": pd.to_numeric(work["close"], errors="coerce")})
    with db.tx() as cur:
        cur.execute("CREATE TEMP TABLE _fx_tmp (LIKE factopia.fx_rate INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.cursor() as c:
            with c.copy("COPY _fx_tmp (pair, date, close) FROM STDIN") as copy:
                copy.write(_copy_text(out.itertuples(index=False, name=None)))
        cur.execute("""INSERT INTO factopia.fx_rate (pair, date, close)
                       SELECT pair, date, close FROM _fx_tmp
                       ON CONFLICT (pair, date) DO UPDATE SET close = EXCLUDED.close""")
    return len(out)


# ── 读取 ────────────────────────────────────────────────────────────────────────────

def _frame(rows: list[tuple], cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=cols)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    for col in TIMESTAMPS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    for col in NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def bars(market: str, columns: list[str] | None = None, start: str | None = None,
         end: str | None = None, symbols: list[str] | None = None) -> pd.DataFrame:
    """按市场取行情。`columns` 只裁剪返回列，不改变语义（列名与 parquet 一致）。"""
    cols = [c for c in (columns or COLUMNS) if c in COLUMNS]
    if not cols:
        return pd.DataFrame()
    # 分区裁剪：market 同时进 WHERE，让计划器只扫一个子表
    where = ["market = %s"]
    params: list[Any] = [market.lower()]
    if start:
        where.append("date >= %s")
        params.append(start)
    if end:
        where.append("date <= %s")
        params.append(end)
    if symbols:
        where.append("symbol = ANY(%s)")
        params.append(list(symbols))
    sql = (f"SELECT {', '.join(cols)} FROM factopia.daily_bar "
           f"WHERE {' AND '.join(where)} ORDER BY symbol, date")
    return _frame(db.query(sql, tuple(params)), cols)


def fx(pairs: list[str] | None = None) -> pd.DataFrame:
    if pairs:
        rows = db.query("SELECT pair, date, close FROM factopia.fx_rate WHERE pair = ANY(%s) "
                        "ORDER BY pair, date", (list(pairs),))
    else:
        rows = db.query("SELECT pair, date, close FROM factopia.fx_rate ORDER BY pair, date")
    return _frame(rows, ["pair", "date", "close"])


# ── 计数与存在性（比读全量便宜，页面与健康检查用） ──────────────────────────────────

def count_bars(market: str) -> int:
    return int((db.query_one("SELECT count(*) FROM factopia.daily_bar WHERE market = %s",
                             (market.lower(),)) or (0,))[0])


def has_bars(market: str) -> bool:
    return db.query_one("SELECT 1 FROM factopia.daily_bar WHERE market = %s LIMIT 1",
                        (market.lower(),)) is not None


def span(market: str) -> dict | None:
    """`{first, last, sessions, symbols}`；空表返回 None。"""
    row = db.query_one(
        """SELECT min(date), max(date), count(DISTINCT date), count(DISTINCT symbol)
             FROM factopia.daily_bar WHERE market = %s""", (market.lower(),))
    if not row or row[0] is None:
        return None
    return {"first": row[0].isoformat(), "last": row[1].isoformat(),
            "sessions": int(row[2]), "symbols": int(row[3])}


def count_fx() -> int:
    return int((db.query_one("SELECT count(*) FROM factopia.fx_rate") or (0,))[0])
