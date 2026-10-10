"""地区差异清单（业务需求 BR-34；Spec 第 14.18 节）。

研究立项时，按所选市场与授权从平台配置汇总各地区在研究上的差异，与研究规格一起锁定（spec["region_diff"]）：

| 类别 | 来源 |
|---|---|
| 交易机制：交易所、交易时段、涨跌停、交收、卖空、每手股数、假期 | configs/refdata/markets.yaml（数据负责人维护，带版本） |
| 成本：买入 / 卖出单边成本、其中税费 | 市场规则包 costs（RULE-04，回测与组合同一来源） |
| 规则：各地区规则包的规则（阻断 / 警告 / 提示）与负责审批的角色 | configs/rules/<市场>.yaml |
| 币种与基准：本地币种、基础币种换算、基准指数、股票池 | 授权、markets.yaml、成分股名单 |
| 披露与会计：报告周期、默认会计口径、公告语言 | markets.yaml |
| 数据：每项数据需求在各市场的现状（缺口） | 研究规格的数据需求 |

每行标注各市场取值是否不同（differs），以及对研究的影响（impact）；只是汇总已登记的信息，不作法律结论。
"""
import csv
import os
from functools import lru_cache
from pathlib import Path

import yaml

from factopia import rules

CONFIGS = Path(os.environ.get("FACTOPIA_CONFIGS_DIR", "/workspace/configs"))
SEVERITY_ZH = {"block": "阻断", "warn": "警告", "info": "提示"}


@lru_cache(maxsize=1)
def _markets() -> dict:
    return yaml.safe_load((CONFIGS / "refdata" / "markets.yaml").read_text(encoding="utf-8"))


def _universe_size(m: str) -> int | None:
    p = CONFIGS / "universe" / "membership" / f"{m}.csv"
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        return len({r["symbol"] for r in csv.DictReader(f) if not r.get("end")})


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%".replace(".00%", "%")


def differences(markets: list[str], mandate: dict | None = None, data_needs: list[dict] | None = None) -> dict:
    cfg = _markets()
    mk = {m: cfg["markets"][m] for m in markets}
    packs = {p["market"]: p for p in rules.select(markets) if p.get("market") in markets}
    base = (mandate or {}).get("base_currency")
    rows = []

    def add(group, item, values, impact="", source=""):
        vals = {m: (values.get(m) if values.get(m) not in (None, "") else "—") for m in markets}
        rows.append({"group": group, "item": item, "values": vals, "differs": len(set(map(str, vals.values()))) > 1,
                     "impact": impact, "source": source})

    src_m = f"markets.yaml {cfg['version']}"
    add("交易机制", "交易所", {m: v["exchange"] for m, v in mk.items()}, source=src_m)
    add("交易机制", "交易时段", {m: v.get("trading_hours") for m, v in mk.items()},
        "收盘时刻不同：日线信号的生效时间与可获取时间按各自收盘计算；三地交易时段只有部分重叠", src_m)
    add("交易机制", "涨跌停 / 波动机制", {m: v.get("price_limits") for m, v in mk.items()}, "", src_m)
    add("交易机制", "交收", {m: v.get("settlement") for m, v in mk.items()}, "交收周期影响资金占用与调仓节奏；英国、法国 2027 年改为 T+1", src_m)
    add("交易机制", "卖空", {m: v.get("short_selling") for m, v in mk.items()},
        "授权只做多时不影响；允许卖空时按各地区规则核对名单与申报门槛", src_m)
    add("交易机制", "每手股数", {m: v.get("lot_size") for m, v in mk.items()}, "香港每手股数不同，小规模组合的权重精度受限", src_m)
    add("交易机制", "假期与特殊停市", {m: v.get("holidays") for m, v in mk.items()}, "三地日历不同：跨市场组合按并集日历计算，休市日收益为 0", src_m)

    cost = {m: rules.costs(m) for m in markets}
    add("成本", "买入单边成本", {m: f"{_pct(c['buy']['total'])}（税费 {_pct(c['buy']['tax'])}）" for m, c in cost.items()},
        "买入成本差异大：英国印花税、法国金融交易税只在买入时收取，换手高的信号在英法受影响更大", "规则包 costs")
    add("成本", "卖出单边成本", {m: f"{_pct(c['sell']['total'])}（税费 {_pct(c['sell']['tax'])}）" for m, c in cost.items()}, "", "规则包 costs")

    add("币种与基准", "本地币种", {m: v["currency"] for m, v in mk.items()},
        f"授权基础币种 {base}：非 {base} 市场的收益需换算，报告区分本地收益、基础币种收益与汇率贡献" if base else "", src_m)
    add("币种与基准", "基准指数", {m: v.get("benchmark") for m, v in mk.items()}, "", src_m)
    add("币种与基准", "股票池（当前成分股）", {m: f"{n} 只" if (n := _universe_size(m)) else None for m in markets},
        "股票池大小不同：持股数按 20% 比例设置；法国 40 只、横截面差异有限", "configs/universe/membership")

    add("披露与会计", "定期报告", {m: v.get("reporting") for m, v in mk.items()}, "报告频率不同：基于财报或公告的信号在各市场的更新频率不同", src_m)
    add("披露与会计", "默认会计口径", {m: v.get("accounting_default") for m, v in mk.items()}, "", src_m)
    add("披露与会计", "公告语言", {m: v.get("disclosure_language") for m, v in mk.items()},
        "文本信号需多语言理解：同一概念在中 / 英 / 法文的表述经术语表统一", src_m)

    for m in markets:
        p = packs.get(m)
        if not p:
            continue
        for r in p["rules"]:
            if r["check"]["type"] == "always" and "TAX" in r["id"]:
                continue                                   # 税费已在"成本"中列出
            add("地区规则", f"{r['id']} {r['title']}", {x: (SEVERITY_ZH[r["severity"]] if x == m else "不适用") for x in markets},
                r.get("impact", ""), f"{p['pack']} {p['version']}（{p.get('status', '')}）")

    for n in data_needs or []:
        add("数据", n.get("name") or n.get("category"), n.get("status") or {}, n.get("detail", ""), "研究规格数据需求")

    return {"markets": markets, "markets_version": cfg["version"],
            "rule_packs": {m: f"{p['pack']} {p['version']}" for m, p in packs.items()},
            "rows": rows, "differs": sum(r["differs"] for r in rows),
            "note": "只汇总平台已登记的信息（市场配置、规则包、授权、数据需求），不作法律结论；规则包状态为待本地合规确认"}
