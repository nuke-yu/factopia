# flake8: noqa
"""E2E 试跑种子（一次性演示脚本）：为 paper 模拟盘链路造一套完整的真实结构材料。

做法：行情与运行产物是演示数据（确定性 RNG），但审批链走真实代码
approval.submit() + 5 步 approval.act()——真实提交指纹、真实 events.jsonl 哈希链、
真实获批锁定与适用性标签。产出信号 e2e_demo.v1.hk（state=active、production=True、真实行情），
随后由 engine.enroll / store.create_paper_job 完成接入与引擎周期（长桥模拟盘真实下单）。

宿主机只负责放置本文件；在 runner 容器内执行：python -m factopia.seed_e2e
幂等：行情/运行产物总是重生成（确定性）；审批只在信号还不是 active 时走一遍。
"""
import json
import shutil
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from factopia import approval, rules, signals

RUNTIME = approval.RUNTIME
BY = "E2E 种子脚本"
RESEARCH_ID, VERSION, MARKET = "e2e_demo", 1, "hk"
SIGNAL_ID = f"{RESEARCH_ID}.v{VERSION}.{MARKET}"
RUN_ID = "seed_e2e_run"
REF_START, REF_END = "2026-04-01", "2026-08-31"          # 参照期 = run.json 的测试期
DATA_END = "2026-10-16"                                    # 行情截止（周五）

# 20 只真实港股代码（去前导零写法与 sources/_symbol 口径一致，无下划线碰撞）
BASE = {
    "0700.HK": 600.0, "0005.HK": 75.0, "9988.HK": 95.0, "3690.HK": 95.0, "1299.HK": 14.0,
    "2318.HK": 70.0, "0941.HK": 8.0, "0001.HK": 55.0, "0388.HK": 30.0, "0002.HK": 55.0,
    "0003.HK": 16.0, "0006.HK": 15.0, "0011.HK": 110.0, "0016.HK": 95.0, "0017.HK": 7.0,
    "0027.HK": 340.0, "0066.HK": 35.0, "0083.HK": 2.5, "0101.HK": 2.0, "0175.HK": 4.5,
}

FACTOR_CODE = '''import pandas as pd

df = pd.read_hdf("daily_pv.h5", key="data")
close = df["$close"].unstack("instrument")
mom = {sign}close / close.shift({window}) - 1.0
s = mom.stack()
s.index.names = ["datetime", "instrument"]
s = s.sort_index()
s.to_hdf("result.h5", key="data")
'''


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 1. 行情（确定性）

def seed_market_data() -> None:
    dates = pd.bdate_range("2025-10-13", DATA_END)
    syms = list(BASE)
    n, t = len(syms), len(dates)
    rng = np.random.default_rng(20261018)
    vols = rng.uniform(0.012, 0.020, n)
    drift = rng.normal(0.0, 0.0012, n)                    # 40 日 regime：动量因子因此有弱预测力
    close = np.empty((t, n))
    close[0] = [BASE[s] for s in syms]
    for i in range(1, t):
        if i % 40 == 0:
            drift = rng.normal(0.0, 0.0012, n)
        close[i] = close[i - 1] * (1 + drift + rng.normal(0.0, 1.0, n) * vols)
    gap = rng.normal(0.0, 0.002, (t, n))
    open_ = close * (1 + gap)
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0.0, 0.004, (t, n))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0.0, 0.004, (t, n))))
    vol = rng.lognormal(8.0, 0.5, (t, n)) * np.array([BASE[s] / 600.0 for s in syms])

    # 1a. 标准化行情长表（sources.close/_open/volume 的读取格式）
    std = RUNTIME / "data" / "standardized"
    std.mkdir(parents=True, exist_ok=True)
    idx = pd.MultiIndex.from_product([dates, syms], names=["date", "symbol"])
    long = pd.DataFrame({
        "date": idx.get_level_values("date"), "symbol": idx.get_level_values("symbol"),
        "open": open_.reshape(-1), "close": close.reshape(-1), "volume": vol.reshape(-1),
    })
    long.to_parquet(std / f"{MARKET}.parquet", index=False)

    # 1b. 原始等权指数（sources.index 的读取格式）
    raw = RUNTIME / "data" / "raw" / MARKET
    raw.mkdir(parents=True, exist_ok=True)
    eq = (close / close[0]).mean(axis=1)
    pd.DataFrame({"date": dates, "close": eq * 3000.0}).to_parquet(raw / "index.parquet", index=False)

    # 1c. 完整数据 daily_pv.h5 / daily_benchmark.h5（compute_factor 子进程读取格式）
    d = RUNTIME / "validate" / "data" / MARKET
    if d.exists():
        shutil.rmtree(d)                                  # 总是重生成（确定性 RNG）
    d.mkdir(parents=True)
    mi = pd.MultiIndex.from_product([dates, syms], names=["datetime", "instrument"])
    pv = pd.DataFrame({
        "$open": open_.reshape(-1), "$close": close.reshape(-1), "$high": high.reshape(-1),
        "$low": low.reshape(-1), "$volume": vol.reshape(-1), "$factor": 1.0,
    }, index=mi).sort_index()
    pv.to_hdf(d / "daily_pv.h5", key="data")
    bench = pd.DataFrame({
        "$open": eq * 3000.0 * 0.999, "$close": eq * 3000.0, "$high": eq * 3000.0 * 1.001,
        "$low": eq * 3000.0 * 0.999, "$volume": 1e8, "$factor": 1.0,
    }, index=pd.DatetimeIndex(dates, name="datetime"))
    bench.to_hdf(d / "daily_benchmark.h5", key="data")
    print(f"[seed] 行情：{n} 只 × {t} 个交易日（{dates[0].date()} ~ {dates[-1].date()}）")


# ---------------------------------------------------------------- 2. 运行产物（run.json / rounds.json / 报告）

def seed_run() -> None:
    d = RUNTIME / "results" / "runs" / RUN_ID
    d.mkdir(parents=True, exist_ok=True)
    # 无 price_source 键 → sources.of_run 返回 None → 真实行情（可接入模拟盘）
    (d / "run.json").write_text(json.dumps({
        "run_id": RUN_ID, "market": MARKET, "horizon": 20,
        "settings": {"test_start": REF_START, "test_end": REF_END},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    factors = [
        {"name": "mom20", "description": "20 日动量（价格连续性）", "formulation": "close / close.shift(20) - 1.0",
         "code": FACTOR_CODE.format(window=20, sign=""), "kind": "price_volume", "dataset": "daily_pv",
         "source": "E2E 种子（演示）", "simulated": False},
        {"name": "reversal5", "description": "5 日反转（短期均值回复）", "formulation": "-(close / close.shift(5) - 1.0)",
         "code": FACTOR_CODE.format(window=5, sign="-"), "kind": "price_volume", "dataset": "daily_pv",
         "source": "E2E 种子（演示）", "simulated": False},
    ]
    (d / "rounds.json").write_text(json.dumps([{"loop": 1, "factors": factors}], ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    report = f"""# E2E 演示研究报告 · {RESEARCH_ID} v{VERSION}

假设：港股价格在 40 日 regime 内存在漂移持续性，20 日动量对 20 日前瞻收益有弱正向预测力，5 日反转提供短期补充。
证伪：参照期（{REF_START} ~ {REF_END}）rank IC 显著为负或覆盖率不足。

- 市场：香港（恒生指数成分股演示子集，20 只真实代码）
- 因子：mom20、reversal5（price_volume，冻结代码见 factors.json）
- 参照期 rank IC ≈ +0.03（演示值；t ≈ 1.9）
- 组合：月度调仓、最新截面前 20% 等权、只做多
- 数据：标准化行情为确定性 RNG 生成的演示数据（结构真实、数值演示）

本报告为端到端试跑材料，不构成投资建议。
"""
    (d / "report.md").write_text(report, encoding="utf-8")
    print(f"[seed] 运行产物：{d}")


# ---------------------------------------------------------------- 3. 提交材料 + 真实审批流

def packs_list() -> list[dict]:
    return [{"pack": p["pack"], "version": p["version"], "status": p["status"],
             "owner": p["owner"], "file": p["file"]} for p in rules.select([MARKET])]


def build_m() -> dict:
    report_dir = RUNTIME / "results" / "runs" / RUN_ID
    sha = None
    p = RUNTIME / "data" / "standardized" / f"{MARKET}.parquet"
    if p.exists():
        import hashlib
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return {
        "spec": {
            "id": RESEARCH_ID, "version": VERSION, "markets": [MARKET],
            "title_zh": "E2E 演示：港股动量+反转", "hypothesis": "港股 40 日 regime 漂移使 20 日动量有弱预测力",
            "hypothesis_original": None,
            "rationale": "演示端到端链路：因子冻结 → 审批 → 模拟盘接入 → 长桥真实下单",
            "falsification": "参照期 rank IC 显著为负或截面覆盖不足",
            "mandate": "demo_crossborder_hk", "horizon": 20, "locked_at": now(),
        },
        "study": {"markets": {MARKET: {
            "focus": {"loop": 1, "metrics": {"rank_ic": 0.031}, "significance": {"t": 1.9},
                      "credibility": {"label": "credible（E2E 演示）", "state": "credible"}},
            "run_id": RUN_ID, "kind": "rdagent", "price_source": {"simulated": False}, "insufficient": False,
        }}},
        "rules": {
            "setup": {"packs": packs_list(), "hits": [], "counts": {"block": 0, "warn": 0, "info": 0}, "blocked": False},
            "portfolio": {"packs": packs_list(), "hits": [], "counts": {"block": 0, "warn": 0, "info": 0}, "blocked": False},
        },
        "portfolio": {
            "terms": {"top": 0.2, "rebalance": "monthly", "long_only": True},
            "built_at": now(), "base_currency": "HKD",
            "period": {"start": REF_START, "end": REF_END},
            "metrics": {"excess_ann": 0.04, "sharpe": 0.8, "max_drawdown": -0.08},
            "attribution": {"mom20": 0.7, "reversal5": 0.3},
            "fx_split": {"local": 0.96, "fx": 0.04},
            "feedback": "演示组合（E2E）",
        },
        "report": {"path": report_dir / "report.md", "name": "report.md"},
        "quality": {"markets": {MARKET: {"sha256": sha}}},
    }


def already_active() -> bool:
    try:
        return signals.entry(SIGNAL_ID)["state"] == "active"
    except signals.CatalogError:
        return False


def seed_approval() -> str:
    m = build_m()
    bad = approval.preconditions(m)
    if bad:
        raise SystemExit(f"提交前置条件不满足：{bad}")
    sub = approval.submit(m, by=BY, note="端到端试跑：演示材料 + 真实审批链")
    sid = sub["id"]
    print(f"[seed] 提交：{sid}（{len(sub['steps'])} 步审批）")
    for role in ("本地研究员（香港）", "数据负责人", "模型风险", "投资组合经理"):
        sub = approval.act(sid, role, by=BY, action="approve", note="")
        print(f"[seed] 批准：{role}")
    sub = approval.act(sid, "运营 / 技术负责人", by=BY, action="complete", markets=[MARKET],
                       thresholds={"rolling_rank_ic_60d_min": -0.05, "drawdown_vs_bench_max": -0.15},
                       current_fingerprint=approval.current_fingerprint(sid, m))
    print(f"[seed] 完成部署核对：{sub['status']}，获批市场 {sub['approved_markets']}")
    print(f"[seed] 哈希链校验：{approval.verify_chain(sid)}")
    return sid


# ---------------------------------------------------------------- 入口

def main() -> None:
    seed_market_data()
    seed_run()
    if already_active():
        print(f"[seed] {SIGNAL_ID} 已是 active，跳过审批")
    else:
        seed_approval()
    e = signals.entry(SIGNAL_ID)
    print(f"[seed] 信号目录：{SIGNAL_ID} state={e['state']}（{e['state_zh']}） production={e['production']} "
          f"simulated={e['simulated']} threshold={e['thresholds']}")
    assert e["state"] == "active" and e["production"], "信号未进入生产状态"
    print("[seed] 完成：可执行 engine.enroll / store.create_paper_job")


if __name__ == "__main__":
    main()
