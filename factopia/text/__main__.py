"""文本流程命令行（在 app 容器中运行，路径均为容器内路径）。

  python -m factopia.text collect --companies 5 --max-per-company 12      # 法国 AMF：采集原文
  python -m factopia.text demo                                              # 香港、英国模拟数据
  python -m factopia.text understand --dataset fr [--lexicon] [--max-docs N]   # 只显示预计花费，不调用大模型
  python -m factopia.text understand --dataset fr --yes [--budget 2]          # 确认后运行；达到预算上限即停止
  python -m factopia.text understand --dataset fr --refresh [--yes]           # 补做：术语表扩充或切换全文后，只判断新纳入的段落
  python -m factopia.text glossary-suggest --dataset fr [--yes]               # AI 从未命中的段落里找候选术语（人工在情报台批准）
  python -m factopia.text signal --dataset fr [--horizon 20]
  python -m factopia.text compare
  python -m factopia.text sensitivity --dataset demo_hk                   # 处理延迟 × 沿用期 敏感性检验
  python -m factopia.text all [--lexicon]                                   # 以上全部
"""
import argparse
import json

from factopia.text import amf, demo, glossary_suggest, review, signal, understand
from factopia.text.common import DATASETS


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m factopia.text")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--companies", type=int)
    c.add_argument("--only", nargs="*")
    c.add_argument("--start", default="2019-01-01")
    c.add_argument("--end")
    c.add_argument("--max-per-company", type=int)
    c.add_argument("--include-inside", action="store_true")
    sub.add_parser("demo")
    u = sub.add_parser("understand")
    u.add_argument("--dataset", required=True, choices=DATASETS)
    u.add_argument("--lexicon", action="store_true")
    u.add_argument("--max-docs", type=int)
    u.add_argument("--redo", action="store_true")
    u.add_argument("--refresh", action="store_true", help="补做：保留已有标注，只判断按当前预筛方式 / 新术语纳入的段落")
    u.add_argument("--yes", action="store_true", help="确认预计花费后运行（不加时只显示估算）")
    u.add_argument("--budget", type=float, help=f"本次运行的花费上限（美元），默认 {understand.DEFAULT_BUDGET}")
    gs = sub.add_parser("glossary-suggest")
    gs.add_argument("--dataset", required=True, choices=DATASETS)
    gs.add_argument("--sample", type=int)
    gs.add_argument("--yes", action="store_true")
    gs.add_argument("--budget", type=float, default=0.5)
    s = sub.add_parser("signal")
    s.add_argument("--dataset", required=True, choices=DATASETS)
    s.add_argument("--horizon", type=int, default=20)
    se = sub.add_parser("sensitivity")
    se.add_argument("--dataset", required=True, choices=DATASETS)
    se.add_argument("--horizon", type=int, default=20)
    cp = sub.add_parser("compare")
    cp.add_argument("--horizon", type=int, default=20)
    a = sub.add_parser("all")
    a.add_argument("--lexicon", action="store_true")
    a.add_argument("--yes", action="store_true", help="确认大模型标注的预计花费（不加时 all 只用已有标注，不调用大模型）")
    a.add_argument("--budget", type=float)
    a.add_argument("--horizon", type=int, default=20)
    args = p.parse_args()
    review.ensure_dir()

    if args.cmd == "collect":
        meta = amf.collect(args.companies, args.start, args.end, args.max_per_company, args.only, args.include_inside)
        print(f"共 {len(meta)} 篇公告")
    elif args.cmd == "demo":
        for ds in ("demo_hk", "demo_uk"):
            print(ds, json.dumps(demo.build(ds), ensure_ascii=False))
    elif args.cmd == "understand":
        budget = understand.DEFAULT_BUDGET if args.budget is None else args.budget
        if not args.lexicon and not args.yes:
            e = understand.estimate(args.dataset, args.max_docs, args.redo, args.refresh)
            print(understand.describe(e, budget))
            print("未调用大模型。确认后加 --yes 运行" + ("（可用 --budget 调整上限）" if e["usd"] > 0 else ""))
            return
        print(understand.run(args.dataset, args.lexicon, args.max_docs, args.redo, budget, args.refresh))
    elif args.cmd == "glossary-suggest":
        e = glossary_suggest.estimate(args.dataset, args.sample)
        print(f"{args.dataset}：抽取未命中段落 {e['paragraphs']} 段（约 {e['calls']} 次调用），预计约 ${e['usd']}；预算上限 ${args.budget:g}")
        if not args.yes:
            print("未调用大模型。确认后加 --yes 运行")
            return
        print(json.dumps(glossary_suggest.run(args.dataset, args.sample, args.budget), ensure_ascii=False))
    elif args.cmd == "signal":
        out = signal.run(args.dataset, args.horizon)
        print(json.dumps({k: out[k] for k in ("n_filings", "n_companies", "n_changes", "evaluation")}, ensure_ascii=False)[:1500])
    elif args.cmd == "sensitivity":
        out = signal.sensitivity(args.dataset, horizon=args.horizon)
        for r in out["rows"]:
            print(r)
        print("稳定" if out["stable"] else "不稳定", out["verdicts"])
    elif args.cmd == "compare":
        print(json.dumps(signal.compare(args.horizon), ensure_ascii=False, indent=1))
    elif args.cmd == "all":
        for ds in DATASETS:
            if not (signal.ds_dir(ds) / "filings.parquet").exists():
                continue
            if args.lexicon or args.yes:
                print(ds, understand.run(ds, args.lexicon, budget=args.budget))
            else:
                print(understand.describe(understand.estimate(ds), args.budget or understand.DEFAULT_BUDGET), "（未加 --yes，跳过标注）")
            out = signal.run(ds, args.horizon)
            print(ds, out["evaluation"]["rank_ic_mean"], out["evaluation"]["rank_ic_t"])
        print(json.dumps(signal.compare(args.horizon), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
