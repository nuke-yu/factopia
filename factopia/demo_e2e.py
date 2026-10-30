# flake8: noqa
"""端到端演示脚本：模拟从数据收集到模拟盘运行的完整流程（一个进程内跑完全部七个阶段）。

  1 数据收集   —— 生成三层数据（standardized / raw / validate），与 factopia.data 四步流水线的产出格式一致
  2 线索池点火 —— ideas.create 提交第一条人工线索（研究线索池的冷启动入口：全新环境线索池为空，靠人工提交点火）
  3 研究产物   —— run.json / rounds.json / report.md（不写 price_source → 真实行情口径，可接入模拟盘）
  4 真实审批链 —— approval.submit + 5 步 approval.act：真实提交指纹、events.jsonl 哈希链、获批锁定
  5 接入模拟盘 —— engine.enroll：按 broker.mode() 自动判定撮合通道
  6 引擎周期   —— engine.cycle 跑两轮：首轮调仓下单 + 估值快照，二轮结算挂单 + 幂等守卫（只重算报告）
  7 交付核验   —— enrolled.json / state.json / orders.jsonl / paper.json 四件套 + nav.csv / positions.csv

行情与研究产物是确定性 RNG 演示数据（结构真实、数值演示）；审批、信号目录、模拟盘引擎全部走真实代码。

运行（runner 容器内；宿主机 cwd 为项目根目录）：
  docker compose -f docker-compose.yml -f compose.local.yml --profile research exec -T runner python -m factopia.demo_e2e

撮合模式（自动判定）：
  - shadow：无需凭证。本地撮合，T 日收盘截面决策、T+1 开盘价成交、规则包成本。
  - longport：.env 配齐 LONGPORT_APP_KEY / APP_SECRET / ACCESS_TOKEN 时启用。真实提交市价单到长桥
    模拟账户，每手 100 股取整（演示级局限；真实每手股数不同的标的会被券商拒单并留痕，引擎继续）。

幂等：行情 / 研究产物总是重生成（确定性）；线索与审批只在不存在时走；enroll 重复接入 = 重置从头开始。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from factopia import approval, ideas, rules, signals
from factopia.paper import engine, broker
from factopia import seed_e2e as seed

RUNTIME = approval.RUNTIME
BY = "端到端演示脚本"
RESEARCH_ID, VERSION, MARKET = "demo_e2e", 1, "hk"
SIGNAL_ID = f"{RESEARCH_ID}.v{VERSION}.{MARKET}"
RUN_ID = "demo_e2e_run"
CAPITAL = 1_000_000.0
IDEA_TITLE = "演示线索：港股动量延续性假设"
REF_START, REF_END = "2026-04-01", "2026-08-31"          # 参照期 = run.json 的测试期
PAPER_DIR = RUNTIME / "paper" / SIGNAL_ID


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 1 数据收集

def stage_data() -> None:
    seed.seed_market_data()                              # 复用 E2E 种子：20 只港股 × 265 日，三层落盘
    print(f"[demo] ① 数据收集：standardized / raw / validate 三层已写入 {RUNTIME / 'data'}")


# ---------------------------------------------------------------- 2 线索池点火

def stage_idea() -> None:
    if any(i["title"] == IDEA_TITLE for i in ideas.all_ideas()):
        print(f"[demo] ② 线索池：已存在「{IDEA_TITLE}」，跳过（避免重复堆积）")
        return
    idea = ideas.create({
        "channel": "hypothesis", "title": IDEA_TITLE,
        "body": "港股个股在数月 regime 内存在价格漂移，20 日动量对后续收益有弱正向预测力，"
                "适合月度调仓的多头组合验证。全新环境线索池为空，人工提交是冷启动入口。",
        "markets": [MARKET], "submitted_by": BY, "confirm_no_client_data": True,
    })
    print(f"[demo] ② 线索池点火：{idea['id']}（{idea['channel_zh']}，哈希链 {ideas.verify_chain(idea['id'])['events']} 条）")


# ---------------------------------------------------------------- 3 研究产物

def stage_run() -> None:
    d = RUNTIME / "results" / "runs" / RUN_ID
    d.mkdir(parents=True, exist_ok=True)
    # 无 price_source 键 → sources.of_run 返回 None → 真实行情（可接入模拟盘）
    (d / "run.json").write_text(json.dumps({
        "run_id": RUN_ID, "market": MARKET, "horizon": 20,
        "settings": {"test_start": REF_START, "test_end": REF_END},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    factors = [
        {"name": "mom20", "description": "20 日动量（价格连续性）", "formulation": "close / close.shift(20) - 1.0",
         "code": seed.FACTOR_CODE.format(window=20, sign=""), "kind": "price_volume", "dataset": "daily_pv",
         "source": "端到端演示", "simulated": False},
        {"name": "reversal5", "description": "5 日反转（短期均值回复）", "formulation": "-(close / close.shift(5) - 1.0)",
         "code": seed.FACTOR_CODE.format(window=5, sign="-"), "kind": "price_volume", "dataset": "daily_pv",
         "source": "端到端演示", "simulated": False},
    ]
    (d / "rounds.json").write_text(json.dumps([{"loop": 1, "factors": factors}], ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    (d / "report.md").write_text(
        f"# 端到端演示报告 · {RESEARCH_ID} v{VERSION}\n\n"
        f"假设：20 日动量对 20 日前瞻收益有弱正向预测力，5 日反转提供短期补充。\n"
        f"证伪：参照期（{REF_START} ~ {REF_END}）rank IC 显著为负或截面覆盖不足。\n\n"
        f"市场：香港（演示子集 20 只）· 因子：mom20、reversal5 · 月度调仓、前 20% 等权、只做多\n",
        encoding="utf-8")
    print(f"[demo] ③ 研究产物：{d}")


# ---------------------------------------------------------------- 4 真实审批链

def build_m() -> dict:
    import hashlib
    p = RUNTIME / "data" / "standardized" / f"{MARKET}.parquet"
    sha = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
    packs = [{"pack": x["pack"], "version": x["version"], "status": x["status"],
              "owner": x["owner"], "file": x["file"]} for x in rules.select([MARKET])]
    return {
        "spec": {
            "id": RESEARCH_ID, "version": VERSION, "markets": [MARKET],
            "title_zh": "端到端演示：港股动量+反转", "hypothesis": "港股 regime 漂移使 20 日动量有弱预测力",
            "hypothesis_original": None,
            "rationale": "演示从数据收集到模拟盘运行的完整链路",
            "falsification": "参照期 rank IC 显著为负或截面覆盖不足",
            "mandate": "demo_crossborder_hk", "horizon": 20, "locked_at": now(),
        },
        "study": {"markets": {MARKET: {
            "focus": {"loop": 1, "metrics": {"rank_ic": 0.031}, "significance": {"t": 1.9},
                      "credibility": {"label": "credible（演示）", "state": "credible"}},
            "run_id": RUN_ID, "kind": "rdagent", "price_source": {"simulated": False}, "insufficient": False,
        }}},
        "rules": {
            "setup": {"packs": packs, "hits": [], "counts": {"block": 0, "warn": 0, "info": 0}, "blocked": False},
            "portfolio": {"packs": packs, "hits": [], "counts": {"block": 0, "warn": 0, "info": 0}, "blocked": False},
        },
        "portfolio": {
            "terms": {"top": 0.2, "rebalance": "monthly", "long_only": True},
            "built_at": now(), "base_currency": "HKD",
            "period": {"start": REF_START, "end": REF_END},
            "metrics": {"excess_ann": 0.04, "sharpe": 0.8, "max_drawdown": -0.08},
            "attribution": {"mom20": 0.7, "reversal5": 0.3},
            "fx_split": {"local": 0.96, "fx": 0.04},
            "feedback": "演示组合",
        },
        "report": {"path": RUNTIME / "results" / "runs" / RUN_ID / "report.md", "name": "report.md"},
        "quality": {"markets": {MARKET: {"sha256": sha}}},
    }


def stage_approval() -> None:
    try:
        if signals.entry(SIGNAL_ID)["state"] == "active":
            print(f"[demo] ④ 审批：{SIGNAL_ID} 已是 active，跳过")
            return
    except signals.CatalogError:
        pass
    m = build_m()
    bad = approval.preconditions(m)
    if bad:
        raise SystemExit(f"提交前置条件不满足：{bad}")
    sub = approval.submit(m, by=BY, note="端到端演示：数据收集 → 审批 → 模拟盘运行")
    sid = sub["id"]
    print(f"[demo] ④ 审批提交：{sid}（{len(sub['steps'])} 步：{' → '.join(s['role'] for s in sub['steps'])}）")
    for role in ("本地研究员（香港）", "数据负责人", "模型风险", "投资组合经理"):
        sub = approval.act(sid, role, by=BY, action="approve", note="")
        print(f"[demo]   批准：{role}")
    sub = approval.act(sid, "运营 / 技术负责人", by=BY, action="complete", markets=[MARKET],
                       thresholds={"rolling_rank_ic_60d_min": -0.05, "drawdown_vs_bench_max": -0.15},
                       current_fingerprint=approval.current_fingerprint(sid, m))
    print(f"[demo]   完成部署核对：{sub['status']}，获批市场 {sub['approved_markets']}，"
          f"哈希链校验 {approval.verify_chain(sid)}")
    e = signals.entry(SIGNAL_ID)
    assert e["state"] == "active" and e["production"], "信号未进入生产状态"
    print(f"[demo]   信号目录：{SIGNAL_ID} state={e['state']} production={e['production']} simulated={e['simulated']}")


# ---------------------------------------------------------------- 5-6 接入与引擎周期

def stage_paper() -> None:
    mode = broker.mode()
    print(f"[demo] ⑤ 接入模拟盘：撮合模式 = {mode}"
          + ("（长桥模拟账户，真实下单）" if mode == "longport" else "（本地影子撮合，T+1 开盘价成交）"))
    engine.enroll(SIGNAL_ID, capital=CAPITAL)
    print(f"[demo] ⑥ 引擎周期第 1 轮：结算挂单（无）→ 重算因子 → 月度调仓 → 快照 → 报告")
    engine.cycle(SIGNAL_ID)
    print(f"[demo] ⑥ 引擎周期第 2 轮：结算挂单（第 1 轮订单回查 / 撮合）→ 幂等守卫（行情无新日期，只重算报告）")
    engine.cycle(SIGNAL_ID)


# ---------------------------------------------------------------- 7 交付核验

def stage_verify() -> None:
    rec = json.loads((PAPER_DIR / "enrolled.json").read_text(encoding="utf-8"))
    state = json.loads((PAPER_DIR / "state.json").read_text(encoding="utf-8"))
    evs = [json.loads(x) for x in (PAPER_DIR / "orders.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    paper = json.loads((RUNTIME / "signals" / SIGNAL_ID / "paper.json").read_text(encoding="utf-8"))
    nav = (PAPER_DIR / "nav.csv").read_text(encoding="utf-8").strip().splitlines()

    print(f"[demo] ⑦ 交付四件套（{PAPER_DIR}）")
    print(f"    enrolled.json：mode={rec['mode']} capital={rec['capital']:.0f} {rec['base_currency']} "
          f"active={rec['active']} 阈值={rec['thresholds']}")
    print(f"    state.json  ：last_cycle={state['last_cycle']} 持仓 {len(state.get('positions') or {})} 只 "
          f"现金={state.get('cash'):.0f} 挂单 {len(state.get('pending') or [])} 笔 nav={state.get('nav')}")
    from collections import Counter
    by_event = Counter(e["event"] for e in evs)
    print(f"    orders.jsonl：{len(evs)} 条事件 {dict(by_event)}（placed/filled/cancelled 全留痕）")
    print(f"    paper.json  ：mode={paper['mode']} nav={paper['nav']['last']} "
          f"建议={paper['suggestion_zh']} 挂单={paper['positions']['pending']} 预警={len(paper['alerts'])}")
    print(f"    nav.csv     ：{len(nav) - 1} 个交易日净值 {nav[1:]}")
    print("[demo] 端到端完成：数据收集 → 线索池 → 研究产物 → 审批 → 接入 → 引擎周期 → 报告交付")


def main() -> None:
    stage_data()
    stage_idea()
    stage_run()
    stage_approval()
    stage_paper()
    stage_verify()


if __name__ == "__main__":
    main()
