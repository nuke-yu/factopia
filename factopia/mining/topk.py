"""按股票池大小设置 RD-Agent 回测的持股数（topk）和每日换仓数（n_drop），写入各市场模板。

RD-Agent 原版模板为 A 股沪深 300 设计：topk=50、n_drop=5。直接用于恒指（84 只）、富时 100、
CAC 40（40 只）时，持股占股票池 50%–100%，选股基本失效。改为按比例设置：

- 持股数 = 股票池的 20%（与因子评估的五分位口径一致），且不少于 8 只
- 每日换仓数 = 持股数的 10%，至少 1 只（控制换手和交易成本）

这是 RD-Agent 检验因子的回测规则，不是最终客户组合（客户组合由组合层按授权约束构建）。
股票池变化后重新运行即可。

运行：docker compose -f docker-compose.yml -f compose.local.yml run --rm app python -m factopia.mining.topk
"""
import os
import re
from pathlib import Path

TOPK_RATIO = 0.20
MIN_TOPK = 8
DROP_RATIO = 0.10
MIN_DROP = 1

CONFIG_DIR = Path("configs/rdagent")
RDAGENT_HOME = Path(os.environ.get("FACTOPIA_RDAGENT_HOME", "/workspace/runtime/rdagent_markets"))
POOLS = {"hk": "hsi", "uk": "ftse100", "fr": "cac40"}


def pool_size(market: str) -> int:
    path = RDAGENT_HOME / market / "qlib_data" / "cn_data" / "instruments" / f"{POOLS[market]}.txt"
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def sizing(n: int) -> tuple[int, int]:
    topk = max(MIN_TOPK, round(n * TOPK_RATIO))
    n_drop = max(MIN_DROP, round(topk * DROP_RATIO))
    return topk, n_drop


def main() -> None:
    for market in POOLS:
        n = pool_size(market)
        topk, n_drop = sizing(n)
        note = (f"# 持股数按股票池比例设置（factopia/mining/topk.py）：股票池 {n} 只，"
                f"topk={topk}（{TOPK_RATIO:.0%}，最少 {MIN_TOPK}），n_drop={n_drop}（持股数的 {DROP_RATIO:.0%}，最少 {MIN_DROP}）\n")
        files = sorted((CONFIG_DIR / market).glob("*/*.yaml"))
        for f in files:
            s = f.read_text()
            s = re.sub(r"^# 持股数按股票池比例设置.*\n", "", s, flags=re.M)
            s = re.sub(r"(\btopk:\s*)\d+", rf"\g<1>{topk}", s)
            s = re.sub(r"(\bn_drop:\s*)\d+", rf"\g<1>{n_drop}", s)
            lines = s.splitlines(keepends=True)
            insert_at = next(i for i, line in enumerate(lines) if not line.startswith("#"))
            lines.insert(insert_at, note)
            f.write_text("".join(lines))
        print(f"{market}: 股票池 {n} 只 → topk={topk}, n_drop={n_drop}（更新 {len(files)} 个模板）")


if __name__ == "__main__":
    main()
