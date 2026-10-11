"""文本流程命令行（在 app 容器中运行，路径均为容器内路径）。

  python -m factopia.text collect --companies 5 --max-per-company 12      # 法国 AMF：采集原文
  python -m factopia.text demo                                              # 香港、英国模拟数据
  python -m factopia.text understand --dataset fr [--lexicon] [--max-docs N]
  python -m factopia.text signal --dataset fr [--horizon 20]
  python -m factopia.text compare
  python -m factopia.text sensitivity --dataset demo_hk                   # 处理延迟 × 沿用期 敏感性检验
  python -m factopia.text all [--lexicon]                                   # 以上全部
"""
import argparse
import json

from factopia.text import amf, demo, review, signal, understand
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
        print(understand.run(args.dataset, args.lexicon, args.max_docs, args.redo))
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
            print(ds, understand.run(ds, args.lexicon))
            out = signal.run(ds, args.horizon)
            print(ds, out["evaluation"]["rank_ic_mean"], out["evaluation"]["rank_ic_t"])
        print(json.dumps(signal.compare(args.horizon), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
