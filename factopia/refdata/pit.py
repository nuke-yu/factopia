"""时间点一致性状态（BR-18）：每个数据域记录业务日期、正式发布时间、实际可获取时间的口径，并判断是否时间点一致。

结果写入 /workspace/runtime/results/pit_status.json，所有结果页面据此标注；任一用于回测的数据域不一致，
整体标为"非时间点一致"，并列出原因与影响。
"""
import json
import os
from pathlib import Path

import yaml

from factopia.refdata import membership
from factopia.refdata.master import CONFIGS, REF, RUNTIME, now

RESULTS = Path(os.environ.get("FACTOPIA_RESULTS_DIR", "/workspace/runtime/results"))
MARKET_ZH = {"hk": "香港", "uk": "英国", "fr": "法国"}


def build() -> dict:
    mk = yaml.safe_load((CONFIGS / "refdata" / "markets.yaml").read_text(encoding="utf-8"))
    domains = []

    def add(key, name, used, business, published, available, ok, note):
        domains.append({"key": key, "name": name, "used_in_backtest": used, "business_date": business,
                        "published_at": published, "available_at": available, "status": "ok" if ok else "warn",
                        "note": note})

    delays = "、".join(f"{MARKET_ZH[m]} {c['eod_delay_min']} 分钟" for m, c in mk["markets"].items())
    add("prices", "日线行情", True, "交易日（交易所日历）", "当日收盘时刻（UTC，含夏令时）",
        f"收盘时刻 + 日终数据延迟（{delays}）", True,
        "因子在当日收盘后计算，回测按 Qlib 惯例次日起持有，不使用当日尚未可得的数据")
    add("adjustment", "复权", True, "除权除息日", "—", "—", False,
        "Yahoo Finance 复权价按之后的分红、拆股回溯调整：收益正确，但历史价格水平不是当时所见；"
        "使用价格水平（而非收益）的因子可能受影响")
    pit = {m: membership.is_point_in_time(m) for m in MARKET_ZH}
    add("membership", "股票池成分股", True, "成分股生效日", "指数公司公告日", "公告日", all(pit.values()),
        "全部为历史成分股" if all(pit.values()) else
        "使用当前成分股（" + "、".join(MARKET_ZH[m] for m, v in pit.items() if not v) + "）：存在幸存者偏差，"
        "历史上被剔除、退市的股票不在样本中，回测结果可能偏高；需数据商的历史成分股（第 0.2 节）")
    add("fx", "汇率", True, "交易日", "伦敦收盘", "收盘后", True, "组合层换算使用当日收盘汇率")
    text = RUNTIME / "text" / "fr" / "summary.json"
    add("text", "公告文本信号", True, "公告日期（当地）", "正式发布时间（AMF 发往市场时间）",
        "正式发布 + 处理延迟，收盘后发布顺延到下一交易日", True,
        "第 14.9、14.17 节；用于情报台检验与文本信号研究的回测（按实际可获取时间生效）；不提供给 RD-Agent 挖掘" + ("" if text.exists() else "（尚未生成）"))
    add("master", "证券主数据与行业分类", False, "—", "—", "取数时间（当前快照）", False,
        "行业、财务年度、报告币种为当前快照，不是历史值；不用于回测选股，只用于诊断的行业中性化与行业权重（以当前行业近似历史行业）")
    used_warn = [d for d in domains if d["used_in_backtest"] and d["status"] == "warn"]
    out = {"built_at": now(), "point_in_time": not used_warn,
           "label": "时间点一致" if not used_warn else "非时间点一致",
           "reasons": [d["name"] + "：" + d["note"] for d in used_warn], "domains": domains,
           "membership": {m: ("历史成分股" if v else "当前成分股") for m, v in pit.items()},
           "markets_version": mk["version"]}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "pit_status.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    REF.mkdir(parents=True, exist_ok=True)
    return out
