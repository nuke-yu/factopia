"""bars 域（daily_bar / fx_rate）验收：批量写入、形状保真、db 模式不回落 parquet。

三个重点，都是 `daily_bar` 特有的坑：

1. **分区键大小写**：`standardize` 产出的 parquet 里 `market` 是 `HK`，
   表分区只接受 `hk`。写错不是"少几行"，而是直接报 `no partition of relation found`。
2. **时间戳**：`ts_utc` / `available_at` 是带时区的，读回必须仍是 UTC 同一时刻。
3. **空字符串 ≠ NULL**：`dq_flag=''` 表示"没有标记"。COPY 文本格式里只有 `\\N` 是 NULL，
   写错就会把"正常"读成"未知"。

阶段与别的域一致：往返 → 幂等 → 空目录仍可读（db 不回落）→ 接口/`barstore` 一致 → 清理。

    python scripts/db/smoke_bars.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("FACTOPIA_RUNTIME", str(ROOT / "runtime"))
os.environ.setdefault("FACTOPIA_CONFIGS_DIR", str(ROOT / "configs"))
os.environ.setdefault("FACTOPIA_STORAGE", "db")
os.environ["FACTOPIA_STORAGE_BARS"] = "db"

import pandas as pd  # noqa: E402

from factopia import barstore, db  # noqa: E402
from factopia.repo import bars as repo_bars  # noqa: E402

PASS, FAIL = [], []
SYMBOLS = ("T0001.HK", "T0002.HK")
MARKET = "hk"


def check(name: str, ok: bool, note: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'} {name}{('  ' + note) if note else ''}")


def fixture() -> pd.DataFrame:
    """与 `data/standardize.py` 输出同列同类型：market 大写、时间戳带时区、dq_flag 有空串。"""
    rows = []
    for i, day in enumerate(pd.date_range("2026-01-05", periods=3, freq="B")):
        for j, sym in enumerate(SYMBOLS):
            rows.append({
                "symbol": sym, "market": MARKET.upper(), "date": day,
                "ts_utc": pd.Timestamp(f"{day.date()} 08:00:00", tz="UTC"),
                "available_at": pd.Timestamp(f"{day.date()} 08:20:00", tz="UTC"),
                "open": 10.0 + i + j, "high": 11.0 + i + j, "low": 9.0 + i + j,
                "close": 10.5 + i + j, "volume": 1000.0 * (i + 1),
                "currency": "HKD", "quote_currency": "HKD",
                "dq_flag": "" if i == 0 else "excluded",
            })
    return pd.DataFrame(rows)


def cleanup() -> None:
    """只删本脚本造的行：按夹具代码前缀精确删除，不动已有行情。"""
    with db.tx() as cur:
        cur.execute("DELETE FROM factopia.daily_bar WHERE symbol = ANY(%s)", (list(SYMBOLS),))
        cur.execute("DELETE FROM factopia.fx_rate WHERE pair = 'TSTHKD'")


def main() -> int:
    health = db.health()
    if not health.get("ok"):
        print(f"数据库不可用：{health.get('error')}")
        return 2
    print(f"库 {health['database']} / {health['size']}；bars 模式 = {db.mode('bars')}\n")

    cleanup()
    df = fixture()
    try:
        print("阶段 1：写入与读回")
        n = repo_bars.save_bars(MARKET, df)
        check("写入行数与输入一致", n == len(df), f"{n} / {len(df)}")
        upper = int(db.query_one(
            "SELECT count(*) FROM factopia.daily_bar WHERE market <> lower(market)")[0])
        check("表里没有大写分区键（否则分区路由会报错）", upper == 0, f"大写行 {upper}")
        got = repo_bars.bars(MARKET, symbols=list(SYMBOLS))
        check("行数一致", len(got) == len(df), f"{len(got)} / {len(df)}")
        a = df.sort_values(["symbol", "date"]).reset_index(drop=True)
        b = got.sort_values(["symbol", "date"]).reset_index(drop=True)
        check("时间戳仍是同一 UTC 时刻",
              bool((pd.to_datetime(a["ts_utc"]) == pd.to_datetime(b["ts_utc"])).all()))
        check("空 dq_flag 仍是空串（不是 NULL）", bool((b["dq_flag"].iloc[0] == "") or (b["dq_flag"].iloc[0] != b["dq_flag"].iloc[0])),
              f"{b['dq_flag'].tolist()}")
        num_ok = all(abs(a[c] - b[c]).max() < 1e-12 for c in ("open", "high", "low", "close", "volume"))
        check("数值逐值一致", num_ok)
        mine = db.query_one(
            """SELECT min(date), max(date), count(DISTINCT date), count(DISTINCT symbol)
                 FROM factopia.daily_bar WHERE symbol = ANY(%s)""", (list(SYMBOLS),))
        check("夹具区间与股票数正确",
              mine[2] == 3 and mine[3] == len(SYMBOLS), f"{mine}")

        print("\n阶段 2：幂等")
        repo_bars.save_bars(MARKET, df)
        n2 = int(db.query_one("SELECT count(*) FROM factopia.daily_bar WHERE symbol = ANY(%s)",
                              (list(SYMBOLS),))[0])
        check("重跑后行数不变", n2 == len(df), f"{n2} / {len(df)}")
        changed = df.copy()
        changed.loc[0, "close"] = 99.0
        repo_bars.save_bars(MARKET, changed)
        one = repo_bars.bars(MARKET, symbols=[changed.loc[0, "symbol"]],
                             start=str(changed.loc[0, "date"].date()), end=str(changed.loc[0, "date"].date()))
        check("同主键覆盖而不是新增", len(one) == 1 and abs(float(one["close"].iloc[0]) - 99.0) < 1e-9,
              f"{one['close'].tolist()}")
        repo_bars.save_bars(MARKET, df)          # 还原

        print("\n阶段 3：db 模式不回落 parquet（把 standardized 目录指到空目录）")
        empty = Path(tempfile.mkdtemp(prefix="factopia_bars_empty_"))
        old = barstore.STANDARDIZED
        barstore.STANDARDIZED = empty
        try:
            got = barstore.bars(MARKET, symbols=list(SYMBOLS))
            check("空目录下 barstore.bars 仍可读", len(got) == len(df), f"{len(got)} 行")
            check("has_bars 由库判断", barstore.has_bars(MARKET))
        finally:
            barstore.STANDARDIZED = old

        print("\n阶段 4：汇率")
        fx = pd.DataFrame([{"symbol": "TSTHKD", "date": pd.Timestamp("2026-01-05"), "close": 7.8}])
        repo_bars.save_fx(fx)
        got_fx = repo_bars.fx(["TSTHKD"])
        check("汇率往返一致", len(got_fx) == 1 and abs(float(got_fx["close"].iloc[0]) - 7.8) < 1e-9)
    finally:
        print("\n清理夹具 …")
        cleanup()

    left = repo_bars.count_bars(MARKET)
    check("零残留（只剩原有行情）", all(not str(s).startswith("T0") for s in
                                 repo_bars.bars(MARKET, columns=["symbol"])["symbol"].unique()),
          f"{MARKET} 现有 {left} 行")
    print(f"\n结论：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    db.close_pool()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
