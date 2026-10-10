"""数据商参考数据导入（BR-16、18、36；Spec 第 14.21 节）：历史成分股与公司行动。

免费来源没有成分股变更历史，也没有可靠的合并、配股、退市、名称变更记录。取得数据商数据（LSEG、Bloomberg、FactSet、
指数公司等）后，按 configs/refdata/vendor_templates/ 的统一格式导出 CSV，用本模块导入；导入后整条链路自动生效：

- 历史成分股 → configs/universe/membership/<市场>.csv（source = 数据商名称；原文件备份为 .bak）
  → factopia.data.download 下载全部曾经的成分股的行情（含已退市股票，数据源有记录时）
  → factopia.data.to_qlib 按日生效的股票池 → 时间点状态（pit）变为"一致"，结果页面不再标注幸存者偏差；
- 公司行动 → runtime/refdata/vendor_actions.parquet，factopia.refdata.actions 合并进公司行动表（source = 数据商），
  "未覆盖"的类型按实际导入的类型更新；退市日同时检查是否与成分股结束日期一致。

导入前逐行校验，有错误时不写入任何文件，返回错误清单（行号 + 原因）。
"""
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from factopia.refdata import membership

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
REF = RUNTIME / "refdata"
SUFFIX = {"hk": ".HK", "uk": ".L", "fr": ".PA"}
ACTION_TYPES = {"merger": "合并 / 收购", "rights_issue": "配股", "delisting": "退市", "name_change": "名称变更",
                "spin_off": "分拆", "split": "拆股 / 合股", "dividend": "分红", "special_dividend": "特别分红",
                "ticker_change": "代码变更"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class VendorError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors[:10]) + (f"……共 {len(errors)} 处" if len(errors) > 10 else ""))
        self.errors = errors


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path, required: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise VendorError([f"缺少列：{', '.join(missing)}（格式见 configs/refdata/vendor_templates/README.md）"])
    return df


def check_membership(market: str, df: pd.DataFrame) -> list[str]:
    errs = []
    for i, r in enumerate(df.to_dict("records"), start=2):          # 第 1 行为表头
        s, a, b = r["symbol"].strip(), r["start"].strip(), r["end"].strip()
        if not s.endswith(SUFFIX[market]):
            errs.append(f"第 {i} 行：代码 {s} 不是{market}市场格式（应以 {SUFFIX[market]} 结尾）")
        if not DATE.match(a):
            errs.append(f"第 {i} 行：start 须为 YYYY-MM-DD")
        if b and not DATE.match(b):
            errs.append(f"第 {i} 行：end 须为 YYYY-MM-DD 或留空（仍在指数中）")
        if DATE.match(a) and b and DATE.match(b) and b < a:
            errs.append(f"第 {i} 行：end 早于 start")
    ok = df[df["start"].str.match(DATE) & (df["end"].eq("") | df["end"].str.match(DATE))]
    for s, g in ok.groupby("symbol"):                               # 同一股票的区间不能重叠
        g = g.sort_values("start")
        prev_end = None
        for r in g.to_dict("records"):
            if prev_end is not None and (prev_end == "" or r["start"] <= prev_end):
                errs.append(f"{s}：区间重叠（{r['start']} 开始时上一段尚未结束）")
            prev_end = r["end"]
    if ok.empty:
        errs.append("没有有效的成分股记录")
    return errs


def import_membership(market: str, path: str | Path, vendor: str, dry_run: bool = False) -> dict:
    if market not in SUFFIX:
        raise VendorError([f"未知市场：{market}"])
    if not vendor.strip():
        raise VendorError(["须写明数据商名称（记录为 source）"])
    df = _read(Path(path), ["symbol", "start", "end"])
    errs = check_membership(market, df)
    if errs:
        raise VendorError(errs)
    out = pd.DataFrame({"symbol": df["symbol"].str.strip(), "start": df["start"].str.strip(), "end": df["end"].str.strip(),
                        "source": vendor.strip(), "note": df["note"] if "note" in df.columns else ""}, columns=membership.COLUMNS)
    current = set(pd.read_csv(CONFIGS / "universe" / f"{market}.csv")["symbol"])
    std = RUNTIME / "data" / "standardized" / f"{market}.parquet"
    have = set(pd.read_parquet(std, columns=["symbol"])["symbol"]) if std.exists() else set()
    syms = set(out["symbol"])
    report = {"market": market, "vendor": vendor, "rows": len(out), "symbols": len(syms),
              "still_in_index": int((out["end"] == "").sum()), "former_constituents": sorted(syms - set(out.loc[out["end"] == "", "symbol"])),
              "current_list_not_in_file": sorted(current - syms), "need_prices": sorted(syms - have),
              "start": out["start"].min(), "dry_run": dry_run}
    if not dry_run:
        p = membership.path(market)
        if p.exists():
            shutil.copy(p, p.with_suffix(".csv.bak"))
        out.to_csv(p, index=False)
        _log({"kind": "membership", **{k: v for k, v in report.items() if k not in ("former_constituents", "need_prices", "current_list_not_in_file")}})
    report["next_steps"] = ["python -m factopia.data.download（下载曾经的成分股的行情）", "python -m factopia.data.standardize",
                            "python -m factopia.data.to_qlib && python -m factopia.data.to_qlib --rdagent", "python -m factopia.refdata pit"]
    return report


def import_actions(path: str | Path, vendor: str, dry_run: bool = False) -> dict:
    if not vendor.strip():
        raise VendorError(["须写明数据商名称（记录为 source）"])
    df = _read(Path(path), ["symbol", "date", "type"])
    errs = []
    market_of = {suf: m for m, suf in SUFFIX.items()}
    for i, r in enumerate(df.to_dict("records"), start=2):
        if r["type"] not in ACTION_TYPES:
            errs.append(f"第 {i} 行：类型 {r['type']} 不在 {', '.join(ACTION_TYPES)} 中")
        if not DATE.match(r["date"]):
            errs.append(f"第 {i} 行：date 须为 YYYY-MM-DD")
        if not any(r["symbol"].endswith(s) for s in market_of):
            errs.append(f"第 {i} 行：代码 {r['symbol']} 不属于香港 / 英国 / 法国")
        if r["type"] in ("split", "rights_issue") and r.get("value"):
            try:
                float(r["value"])
            except ValueError:
                errs.append(f"第 {i} 行：value 须为数字（比例）")
    if errs:
        raise VendorError(errs)
    out = pd.DataFrame({"symbol": df["symbol"], "market": df["symbol"].map(lambda s: next(m for suf, m in market_of.items() if s.endswith(suf))),
                        "date": df["date"], "type": df["type"],
                        "value": pd.to_numeric(df.get("value", ""), errors="coerce"),
                        "detail": df["detail"] if "detail" in df.columns else "", "source": vendor.strip()})
    # 退市日与成分股结束日期对照
    mismatch = []
    for r in out[out["type"] == "delisting"].to_dict("records"):
        mem = membership.load(r["market"])
        rows = mem[mem["symbol"] == r["symbol"]] if mem is not None else pd.DataFrame()
        if rows.empty or (rows["end"] == "").any() or rows["end"].max() > r["date"]:
            mismatch.append(f"{r['symbol']} 退市日 {r['date']}，成分股名单中" + ("没有记录" if rows.empty else "结束日期不一致"))
    report = {"vendor": vendor, "rows": len(out), "by_type": out["type"].value_counts().to_dict(),
              "delisting_vs_membership": mismatch, "dry_run": dry_run}
    if not dry_run:
        REF.mkdir(parents=True, exist_ok=True)
        out.to_parquet(REF / "vendor_actions.parquet", index=False)
        _log({"kind": "actions", "vendor": vendor, "rows": len(out), "by_type": report["by_type"]})
    report["next_steps"] = ["python -m factopia.refdata actions（合并进公司行动表）", "python -m factopia.refdata pit"]
    return report


def vendor_actions() -> pd.DataFrame | None:
    p = REF / "vendor_actions.parquet"
    return pd.read_parquet(p) if p.exists() else None


def _log(ev: dict) -> None:
    REF.mkdir(parents=True, exist_ok=True)
    with (REF / "vendor_imports.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**ev, "at": _now()}, ensure_ascii=False) + "\n")


def imports() -> list[dict]:
    p = REF / "vendor_imports.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []
