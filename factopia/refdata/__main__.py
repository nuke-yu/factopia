"""参考数据命令行（app 容器中运行）。

  python -m factopia.refdata master [--no-cross-listings] [--only 0005.HK HSBA.L]   # 证券主数据
  python -m factopia.refdata actions [--only ...]                                     # 公司行动
  python -m factopia.refdata membership [--force]                                     # 成分股起止日期
  python -m factopia.refdata pit                                                      # 时间点一致性状态
  python -m factopia.refdata all                                                      # 以上全部
  python -m factopia.refdata import-membership --market uk --file x.csv --vendor LSEG [--dry-run]  # 数据商历史成分股
  python -m factopia.refdata import-actions --file x.csv --vendor LSEG [--dry-run]                 # 数据商公司行动
"""
import argparse
import json

from factopia.refdata import actions, master, membership, pit, vendor


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m factopia.refdata")
    p.add_argument("cmd", choices=["master", "actions", "membership", "pit", "all", "import-membership", "import-actions"])
    p.add_argument("--market")
    p.add_argument("--file")
    p.add_argument("--vendor", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", nargs="*")
    p.add_argument("--no-cross-listings", action="store_true")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    if a.cmd.startswith("import-"):
        try:
            r = (vendor.import_membership(a.market, a.file, a.vendor, a.dry_run) if a.cmd == "import-membership"
                 else vendor.import_actions(a.file, a.vendor, a.dry_run))
        except vendor.VendorError as e:
            print("导入失败，未写入任何文件：")
            print("\n".join(f"  - {x}" for x in e.errors))
            raise SystemExit(1)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    if a.cmd in ("membership", "all"):
        for m in master.MARKETS:
            df = membership.init(m, a.force)
            print(m, len(df), "行", "历史成分股" if membership.is_point_in_time(m) else "当前成分股")
    if a.cmd in ("master", "all"):
        print(json.dumps(master.build(not a.no_cross_listings, a.only), ensure_ascii=False))
    if a.cmd in ("actions", "all"):
        try:
            from factopia.text import amf
            amf.ticker_history()
        except Exception as e:      # AMF 不可用时跳过代码变更
            print(f"AMF 代码历史获取失败，跳过：{e}")
        s = actions.build(a.only)
        print(json.dumps({k: s[k] for k in ("total", "by_type", "symbols_with_actions", "adjustment_failures")}, ensure_ascii=False))
    if a.cmd in ("pit", "all"):
        s = pit.build()
        print(s["label"], s["reasons"])


if __name__ == "__main__":
    main()
