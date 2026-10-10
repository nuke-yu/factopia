"""规则包命令行：校验格式并列出规则（RULE-01）。

  python -m factopia.rules            # 校验全部规则包与数据来源登记表
"""
from factopia import rules


def main() -> None:
    packs = rules.load_packs()
    src = rules.load_sources()
    for p in packs.values():
        sev = {s: sum(r["severity"] == s for r in p["rules"]) for s in rules.SEVERITIES}
        unknown = [r["id"] for r in p["rules"] if r["check"]["type"] not in rules.CHECKS]
        print(f"{p['file']:12} {p['version']}  {p['status']}  规则 {len(p['rules'])}（{sev}）"
              + (f"  未实现的检查：{unknown}" if unknown else ""))
        if p["market"] != "all":
            print(f"{'':12} 单边成本 {rules.costs(p['market'])}")
    print(f"data_sources {src['version']}  来源 {len(src['sources'])}")


if __name__ == "__main__":
    main()
