"""平台自身大模型调用的费用：单价、逐次记录、按日 / 用途汇总。

- 单价：.env 中 FACTOPIA_LLM_PRICE_IN / OUT（美元 / 百万 token，按服务商价目表填写）。
- 记录：每次调用（draft._chat）追加一行到 runtime/llm_costs.jsonl：时间、用途、模型、token、费用。
  token 用接口返回的实际用量（含思考 token），重试中途失败、没有返回用量的调用无法计入。
- RD-Agent 的费用另由运行任务记录（runner，按任务预算上限）。
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = Path(os.environ.get("FACTOPIA_RUNTIME", "/workspace/runtime"))
LEDGER = RUNTIME / "llm_costs.jsonl"


def prices() -> tuple[float, float]:
    return float(os.environ.get("FACTOPIA_LLM_PRICE_IN", "0.27")), float(os.environ.get("FACTOPIA_LLM_PRICE_OUT", "1.10"))


def usd(prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = prices()
    return (prompt_tokens * pin + completion_tokens * pout) / 1e6


def record(task: str, model: str, usage: dict) -> float:
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    cost = usd(pt, ct)
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "task": task, "model": model,
                                "prompt_tokens": pt, "completion_tokens": ct, "usd": round(cost, 6)}, ensure_ascii=False) + "\n")
    except OSError:                                    # 记录失败不影响调用本身
        pass
    return cost


def summary(days: int = 30) -> dict:
    by = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0})
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = (r["at"][:10], r["task"])
            for f in ("prompt_tokens", "completion_tokens", "usd"):
                by[k][f] += r[f]
            by[k]["calls"] += 1
    rows = [{"date": d, "task": t, **{k: (round(v, 4) if k == "usd" else v) for k, v in x.items()}} for (d, t), x in sorted(by.items(), reverse=True)]
    dates = sorted({r["date"] for r in rows}, reverse=True)[:days]
    return {"rows": [r for r in rows if r["date"] in dates], "price_per_million": dict(zip(("in", "out"), prices()))}
