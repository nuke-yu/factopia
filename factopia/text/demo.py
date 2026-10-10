"""香港、英国模拟数据（Spec 第 11.9 节；BR-08 授权前的替代方案）。

- 公司全部虚构（代码 DEMO01.HK / DEMO01.L 等，名称含"示例" / "Example"），不为任何真实公司编造公告；
  所有记录 simulated=True，页面统一标注"模拟数据，仅用于流程演示"。
- 公告：每家公司每季度一篇（香港中文，部分附英文版；英国英文）；风险水平按随机游走变化，
  按水平生成悲观句和否定句（"需求并未转弱"），用于检验跨语言理解与否定句复核。
- 发布时间：午间、收盘后、开盘前随机（检验收盘后顺延）；行情：共同因子 + 个股噪声 + 植入规律。
- 植入规律（truth.json）：可交易的第一天起 20 个交易日，个股收益额外下降 EFFECT × 风险水平变化。
  若时间对齐错误（提前使用公告），检验结果会虚高——可用来核对无泄漏。
- 格式与真实数据相同（filings.parquet、docs/、prices.parquet），取得授权数据后直接替换。
"""
import numpy as np
import pandas as pd

from factopia.text.common import DATASETS, SIMULATED_LABEL, ds_dir, sha, write_json

START, END = "2018-01-01", "2026-09-30"
N_COMPANIES = 30
EFFECT = 0.02          # 风险水平每变化 1 级，随后 20 个交易日累计少涨 2%
EFFECT_DAYS = 20
SEED = {"demo_hk": 7, "demo_uk": 11}

HK_NAMES = ["科技", "地产", "能源", "物流", "零售", "银行", "保险", "电讯", "医药", "食品", "汽车", "航运", "建材", "教育", "传媒"]
UK_NAMES = ["Holdings", "Energy", "Retail", "Bank", "Insurance", "Telecom", "Pharma", "Foods", "Mining", "Logistics",
            "Media", "Utilities", "Housing", "Software", "Leisure"]

TEMPLATES = {
    "zh": {
        "risk_increase": (["公司面临的经营风险有所上升，前景不明。", "管理层认为下行风险加大，部分资产存在减值可能。"],
                          ["公司未见风险增加，业务保持稳健。"]),
        "demand_weakening": (["本期市场需求明显转弱，订单减少。", "客户态度趋于谨慎，主要产品销量下滑。"],
                             ["未出现需求转弱的迹象，订单保持稳定。"]),
        "cost_pressure": (["原材料价格上涨带来成本压力，毛利率有所下降。", "能源成本上升，成本压力持续。"],
                          ["成本压力没有进一步加大。"]),
        "regulatory_uncertainty": (["行业监管变化带来监管不确定性。", "公司正配合有关部门的监管调查。"],
                                   ["监管不确定性并未加大。"]),
        "neutral": ["本集团继续推进数字化转型，提升运营效率。", "董事会建议派发末期股息。", "本期集团持续优化资本结构，财务状况稳健。",
                    "集团于期内完成两项小型收购，整合进展顺利。", "管理层将继续关注股东回报与可持续发展。",
                    "本公告所载资料为模拟数据，仅用于流程演示。"],
        "titles": {"Q": "季度业务更新（模拟）", "H": "中期业绩公告（模拟）", "A": "年度业绩公告（模拟）"},
    },
    "en": {
        "risk_increase": (["The group faces heightened risk and an uncertain outlook.", "Management sees greater downside risk, with possible impairment of certain assets."],
                          ["We see no heightened risk at this stage and the business remains resilient."]),
        "demand_weakening": (["Demand weakened markedly during the period, with lower volumes.", "Customers turned cautious and we saw declining sales in core products."],
                             ["We have not seen weak demand; order books remained stable."]),
        "cost_pressure": (["Rising costs from raw materials created margin pressure.", "Energy costs rose and cost pressure persisted."],
                          ["Cost pressure did not intensify further."]),
        "regulatory_uncertainty": (["Regulatory change in the sector creates regulatory uncertainty.", "The company is cooperating with a regulator investigation."],
                                   ["Regulatory uncertainty has not increased."]),
        "neutral": ["The group continued its digital transformation programme to improve efficiency.", "The board proposes a final dividend.",
                    "The group further optimised its capital structure and the balance sheet remains sound.",
                    "Two small acquisitions were completed and integration is on track.",
                    "Management remains focused on shareholder returns and sustainability.",
                    "This announcement contains simulated data for workflow demonstration only."],
        "titles": {"Q": "Quarterly trading update (simulated)", "H": "Interim results (simulated)", "A": "Annual results (simulated)"},
    },
}
SUBTYPE = {"Q": "quarterly", "H": "interim", "A": "annual"}
# 发布时刻（当地时间）：午间、收盘后、开盘前
TIMES = {"demo_hk": ["12:15", "16:45", "07:30"], "demo_uk": ["12:00", "17:05", "07:00"]}


def companies(dataset: str) -> list[dict]:
    hk = dataset == "demo_hk"
    names = HK_NAMES if hk else UK_NAMES
    out = []
    for k in range(N_COMPANIES):
        base = names[k % len(names)] + ("" if k < len(names) else ("二" if hk else " II"))
        code = f"DEMO{k + 1:02d}" + (".HK" if hk else ".L")
        out.append({"instrument": code, "company": f"示例{base}（模拟）" if hk else f"Example {base} plc (simulated)"})
    return out


def filing_text(rng, lang: str, level: int, kind: str) -> list[str]:
    t = TEMPLATES[lang]
    concepts = ["risk_increase", "demand_weakening", "cost_pressure", "regulatory_uncertainty"]
    paras = list(rng.choice(t["neutral"], size=rng.integers(4, 7), replace=True))
    for _ in range(level):                       # 风险水平越高，悲观句越多
        c = concepts[rng.integers(len(concepts))]
        paras.append(str(rng.choice(t[c][0])))
    for _ in range(max(0, 2 - level) + int(rng.random() < 0.3)):   # 风险低时多出现否定句
        c = concepts[rng.integers(len(concepts))]
        paras.append(str(rng.choice(t[c][1])))
    rng.shuffle(paras)
    return paras


def build(dataset: str) -> dict:
    import exchange_calendars as xcals

    cfg = DATASETS[dataset]
    rng = np.random.default_rng(SEED[dataset])
    d = ds_dir(dataset)
    cal = xcals.get_calendar(cfg["calendar"])
    sched = cal.schedule.loc[START:END]
    days = sched.index
    comps = companies(dataset)
    lang_main = "zh" if dataset == "demo_hk" else "en"
    filings, effects = [], np.zeros((len(days), len(comps)))
    for j, c in enumerate(comps):
        level = int(rng.integers(0, 3))
        for q in pd.date_range(START, END, freq="QE"):
            kind = {3: "A", 9: "H"}.get(q.month, "Q")
            day = q + pd.Timedelta(days=int(rng.integers(20, 45)))
            if day > pd.Timestamp(END):
                break
            level = int(np.clip(level + rng.choice([-2, -1, 0, 0, 1, 2]), 0, 5))
            hhmm = str(rng.choice(TIMES[dataset]))
            local = pd.Timestamp(f"{day:%Y-%m-%d} {hhmm}", tz=cfg["tz"])
            langs = [lang_main] + (["en"] if dataset == "demo_hk" and rng.random() < 0.3 else [])
            for lang in langs:
                paras = filing_text(rng, lang, level, kind)
                fid = f"{dataset}_{c['instrument'].split('.')[0]}_{day:%Y%m%d}_{lang}"
                write_json(d / "docs" / f"{fid}.json", {"id": fid, "pages_total": 1, "pages_read": 1,
                                                        "paragraphs": [{"i": i, "text": p, "sha": sha(p)} for i, p in enumerate(paras)]})
                filings.append({"id": fid, "isin": None, "company": c["company"], "instrument": c["instrument"],
                                "published_at": local.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                                "transmitted_at": None, "title": TEMPLATES[lang]["titles"][kind], "language": lang,
                                "type_fr": None, "subtype_fr": SUBTYPE[kind], "type_en": None, "subtype_en": SUBTYPE[kind],
                                "url": None, "n_paragraphs": len(paras), "source": SIMULATED_LABEL,
                                "licence": cfg["licence"], "simulated": True, "true_level": level})
    meta = pd.DataFrame(filings).sort_values("published_at").reset_index(drop=True)
    # 植入规律：按"真实"可交易时间（公告后第一个收盘之后的下一交易日起）施加
    closes = sched["close"].dt.tz_convert(None).to_numpy()
    main = meta[meta["language"] == lang_main].copy()
    main["prev_level"] = main.groupby("instrument")["true_level"].shift(1)
    col = {c["instrument"]: j for j, c in enumerate(comps)}
    for r in main.dropna(subset=["prev_level"]).to_dict("records"):
        pub = pd.Timestamp(r["published_at"]).tz_convert(None)
        k = int(np.searchsorted(closes, pub.to_datetime64(), side="left")) + 1     # 第一个可以交易的交易日
        delta = r["true_level"] - r["prev_level"]
        effects[k:k + EFFECT_DAYS, col[r["instrument"]]] -= EFFECT * delta / EFFECT_DAYS
    market = rng.normal(0.0002, 0.009, size=len(days))
    beta = rng.uniform(0.7, 1.3, size=len(comps))
    noise = rng.normal(0, 0.015, size=(len(days), len(comps)))
    ret = market[:, None] * beta[None, :] + noise + effects
    close = 50 * np.exp(np.cumsum(ret, axis=0))
    px = pd.DataFrame(close, index=days, columns=[c["instrument"] for c in comps])
    long = px.stack().rename("close").rename_axis(["date", "symbol"]).reset_index()
    long["ts_utc"] = long["date"].map(sched["close"])
    long["market"] = cfg["market"].upper()
    long.to_parquet(d / "prices.parquet", index=False)
    meta.to_parquet(d / "filings.parquet", index=False)
    truth = {"label": SIMULATED_LABEL, "rule": f"风险水平每上升 1 级，可交易的第一天起 {EFFECT_DAYS} 个交易日个股累计少涨 {EFFECT:.0%}",
             "effect": EFFECT, "effect_days": EFFECT_DAYS, "companies": len(comps), "filings": len(meta),
             "expected": "风险表述变化与未来 20 日收益负相关（Rank IC < 0）"}
    write_json(d / "truth.json", truth)
    return truth
