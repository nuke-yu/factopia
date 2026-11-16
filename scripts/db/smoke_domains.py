#!/usr/bin/env python
"""七个域的存储层验收脚本（对真实数据库跑，自带清理）。

覆盖 approval / signals / ideas / agents / portfolio / paper / access·llm_cost。
与 `smoke_storage.py`（research + jobs）互补，重点验证"文件时代的语义有没有在库里走形"：

  1. 回填：七域文件 → 库，二次执行全部书签命中
  2. 对账：`migrate_files.py --verify` 报"全部一致"
  3. 哈希链**逐条等值**：文件里的 at/prev/hash 搬进库后重算必须一模一样（重算=篡改的证明）
  4. 只追加：approval_event / signal_event / idea_event 的 UPDATE / DELETE 被触发器拒绝
  5. db 模式续链：库里追加的下一条，prev 必须等于上一条的 hash
  6. 去重唯一键：同一来源并发写入只有一条落地，另一条拿到 DuplicateIdea
  7. 访问留痕与费用：链续得上、汇总口径不变

用法（需先把隧道开在 15432）：

    export PGPORT=15432 PGHOST=127.0.0.1 PGDATABASE=team12 PGUSER=team12 PGPASSWORD=...
    python scripts/db/smoke_domains.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = Path(os.environ.get("FACTOPIA_DOMAIN_SMOKE_RUNTIME", "/tmp/factopia_rt_domains"))
RID = "demo_smk"
SID = f"{RID}.v1.hk"
SUB = "smk_appr_1"
IID = "idea_smk_1"
AID = "agt_smk_1"
ZERO = "0" * 64

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def chain(events: list[dict]) -> list[dict]:
    """按文件时代的口径给事件补 at/prev/hash（键排序、不转义中文）。"""
    from factopia.repo import chain_hash
    prev, out = ZERO, []
    for ev in events:
        body = {**ev, "prev": prev}
        body["hash"] = chain_hash(body)
        out.append(body)
        prev = body["hash"]
    return out


# ── 样本 ─────────────────────────────────────────────────────

def build_fixture() -> None:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    for sub in ("approvals", "ideas", "signals", "agents", "portfolio", "paper", "access"):
        (FIXTURE / sub).mkdir(parents=True, exist_ok=True)

    # ① 审批：一份提交 + 3 条事件（提交 / 意见 / 例外处理）
    d = FIXTURE / "approvals" / SUB
    d.mkdir()
    submission = {
        "id": SUB, "research_id": RID, "version": 1, "markets": ["hk"], "approved_markets": None,
        "status": "in_review", "current": 1, "fingerprint": "f" * 64,
        "created_at": "2026-11-15T00:00:00+00:00", "created_by": "smk",
        "materials": {"title": "冒烟信号", "code_fingerprint": "c" * 16,
                      "prechecks": {"setup": {"packs": [{"pack": "base", "version": 1}]}}},
        "conditions": [],
        "lock": None,
        "steps": [{"role": "合规负责人", "market": "hk", "duty": "合规复核",
                   "state": "pending", "hits": ["WB-01"], "model_exceptions": []}],
        "rule_decisions": {"合规负责人|WB-01": {"rule": "WB-01", "action": "exception",
                                               "by": "smk", "role": "合规负责人",
                                               "note": "已核", "at": "2026-11-15T00:02:00+00:00"}},
    }
    (d / "submission.json").write_text(json.dumps(submission, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    appr_events = chain([
        {"action": "submit", "role": "研究员", "by": "smk", "note": "提交审批", "at": "2026-11-15T00:00:00+00:00"},
        {"action": "comment", "role": "合规负责人", "by": "smk", "note": "看过了", "at": "2026-11-15T00:01:00+00:00"},
        {"action": "exception", "role": "合规负责人", "by": "smk", "note": "已核", "step": 0,
         "rule": "WB-01", "at": "2026-11-15T00:02:00+00:00"},
    ])
    (d / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in appr_events), encoding="utf-8")

    # ② 信号：全局事件链 2 条 + 一次监控快照
    sig_events = chain([
        {"signal_id": SID, "market": "hk", "action": "pause", "role": "投资组合经理",
         "by": "smk", "note": "回撤超阈值", "at": "2026-11-15T00:03:00+00:00"},
        {"signal_id": SID, "market": "hk", "action": "resume", "role": "投资组合经理",
         "by": "smk", "note": "已恢复", "at": "2026-11-15T00:04:00+00:00"},
    ])
    (FIXTURE / "signals" / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sig_events), encoding="utf-8")
    mon = {"signal_id": SID, "computed_at": "2026-11-15T00:05:00+00:00", "suggestion": "ok",
           "suggestion_zh": "正常", "alerts": [], "data_drift": {}, "decay": {}, "factors": {},
           "currency": {}}
    (FIXTURE / "signals" / SID).mkdir(parents=True, exist_ok=True)
    (FIXTURE / "signals" / SID / "monitor.json").write_text(
        json.dumps(mon, ensure_ascii=False, indent=1), encoding="utf-8")

    # ③ 线索：一条线索 + 1 条事件 + 来源索引
    dd = FIXTURE / "ideas" / IID
    dd.mkdir()
    idea = {"id": IID, "channel": "web", "title": "港股动量", "body": "试试动量",
            "language": "zh", "markets": ["hk"], "submitted_by": "smk", "role": "研究员",
            "source_ref": None, "status": "new", "created_at": "2026-11-15T00:06:00+00:00",
            "document": {}, "pii_checked": True, "links": [], "fields": {}}
    (dd / "idea.json").write_text(json.dumps(idea, ensure_ascii=False, indent=1), encoding="utf-8")
    idea_events = chain([
        {"action": "create", "by": "smk", "note": "", "at": "2026-11-15T00:06:00+00:00"},
    ])
    (dd / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in idea_events), encoding="utf-8")
    (FIXTURE / "ideas" / "sources.json").write_text(
        json.dumps({"web|港股动量|hk": IID}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ④ Agent：状态 + 2 个轮次文件 + 记忆 + 留痕 + 报告
    ad = FIXTURE / "agents" / AID
    (ad / "rounds" / "001").mkdir(parents=True)
    agent = {"agent_id": AID, "kind": "iteration", "state": "running",
             "root": {"research_id": RID, "version": 1, "markets": ["hk"], "horizon": 20},
             "policy": {}, "current_round": 1, "round_limit": 5, "cost_usd": 0.12,
             "usage": {"calls": 3}, "created_at": "2026-11-15T00:07:00+00:00"}
    (ad / "agent.json").write_text(json.dumps(agent, ensure_ascii=False, indent=1), encoding="utf-8")
    (ad / "rounds" / "001" / "facts.json").write_text(json.dumps({"ic": 0.031}, ensure_ascii=False),
                                                      encoding="utf-8")
    (ad / "rounds" / "001" / "plan.json").write_text(json.dumps({"next": ["加反转"]}, ensure_ascii=False),
                                                     encoding="utf-8")
    (ad / "memory.json").write_text(json.dumps({"attempts": [{"round": 1, "result": "rejected"}],
                                                "exclusions": ["不做日内"]}, ensure_ascii=False),
                                    encoding="utf-8")
    (ad / "trace.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        {"at": "2026-11-15T00:08:00+00:00", "kind": "llm", "model": "smk", "usd": 0.01},
        {"at": "2026-11-15T00:09:00+00:00", "kind": "tool", "name": "backtest"},
    ]), encoding="utf-8")
    (ad / "report.md").write_text("# 冒烟迭代报告\n第 1 轮：加反转。\n", encoding="utf-8")

    # ⑤ 组合：summary + 逐日曲线 + 逐股权重 + 反馈
    pd_ = __import__("pandas")
    pd_dir = FIXTURE / "portfolio" / f"{RID}_v1"
    pd_dir.mkdir()
    summary = {"research_id": RID, "version": 1, "built_at": "2026-11-15T00:10:00+00:00",
               "base_currency": "USD", "market": "hk", "mandate": "balanced",
               "period": {"start": "2026-11-13", "end": "2026-11-14", "days": 2, "rebalances": 1},
               "metrics": {"ann_return": 0.12, "sharpe": 1.1},
               "attribution": {"hk": 0.01}, "fx_split": {}, "constraints": []}
    (pd_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    pd_.DataFrame({"date": ["2026-11-13", "2026-11-14"], "port": [1.0, 1.01], "bench": [1.0, 1.0],
                   "hk": [1.0, 1.01]}).to_parquet(pd_dir / "daily.parquet", index=False)
    pd_.DataFrame({"date": ["2026-11-13", "2026-11-13"], "market": ["hk", "hk"],
                   "symbol": ["700", "5"], "weight_in_market": [0.6, 0.4],
                   "weight": [0.6, 0.4]}).to_parquet(pd_dir / "weights.parquet", index=False)
    (pd_dir / "feedback.json").write_text(json.dumps([
        {"at": "2026-11-15T00:11:00+00:00", "by": "smk", "note": "集中度偏高", "kind": "risk"},
    ], ensure_ascii=False, indent=1), encoding="utf-8")

    # ⑥ 模拟盘：登记 / 当前态 / 订单流 / 快照 / 运行报告
    pp = FIXTURE / "paper" / SID
    pp.mkdir()
    (pp / "enrolled.json").write_text(json.dumps({
        "signal_id": SID, "market": "hk", "mode": "shadow", "capital": 100000.0, "horizon": 20,
        "thresholds": {"mdd": 0.15}, "ref_start": "2026-10-01", "ref_end": "2026-11-01",
        "base_currency": "HKD", "enrolled_at": "2026-11-15T00:12:00+00:00", "active": True,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    (pp / "state.json").write_text(json.dumps({
        "last_cycle": "2026-11-14", "last_rebalance": None,
        "positions": {"700": 100.0}, "cash": 50000.0, "nav": 52000.0,
        "positions_value": 2000.0, "pending": [],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    (pp / "orders.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        {"signal_id": SID, "event": "placed", "order_id": "smk_o1", "symbol": "700", "side": "buy",
         "qty": 100, "placed_on": "2026-11-13", "mode": "shadow"},
        {"signal_id": SID, "event": "filled", "order_id": "smk_o1", "symbol": "700", "side": "buy",
         "qty": 100, "price": 20.0, "fill_date": "2026-11-14", "cost": 1.0},
        {"signal_id": SID, "event": "placed", "symbol": "5", "side": "buy", "qty": 0,
         "placed_on": "2026-11-14", "mode": "shadow"},
    ]), encoding="utf-8")
    (pp / "nav.csv").write_text("date,nav,cash,positions_value\n2026-11-14,52000.0,50000.0,2000.0\n",
                                encoding="utf-8")
    (pp / "positions.csv").write_text(
        "date,symbol,qty,price,value\n2026-11-14,700,100.0,20.0,2000.0\n", encoding="utf-8")
    (FIXTURE / "signals" / SID / "paper.json").write_text(json.dumps({
        "signal_id": SID, "computed_at": "2026-11-15T00:13:00+00:00", "suggestion": "hold",
        "suggestion_zh": "继续持有",
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # ⑦ 访问留痕（全局链）+ 大模型费用
    #    真实事件只有 role / role_name / denied / market —— 不能多写 allowed，
    #    否则读回时算不出同一个 hash（链会显示"被改过"）。
    acc = chain([
        {"at": "2026-11-15T00:14:00+00:00", "role": "hk", "role_name": "smk",
         "denied": "signals", "market": "fr"},
        {"at": "2026-11-15T00:15:00+00:00", "role": "hk", "role_name": "smk",
         "denied": "portfolio", "market": "uk"},
    ])
    (FIXTURE / "access" / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in acc), encoding="utf-8")
    (FIXTURE / "llm_costs.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        {"at": "2026-11-15T00:16:00+00:00", "task": "draft", "model": "smk-model",
         "prompt_tokens": 1000, "completion_tokens": 500, "usd": 0.00082},
        {"at": "2026-11-15T00:17:00+00:00", "task": "draft", "model": "smk-model",
         "prompt_tokens": 2000, "completion_tokens": 800, "usd": 0.00142},
    ]), encoding="utf-8")


def cleanup() -> None:
    from factopia import db

    cleanup_rows()
    db.close_pool()


def run_migrate(extra: list[str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "FACTOPIA_RUNTIME": str(FIXTURE), "FACTOPIA_STORAGE": "files"}
    cmd = [sys.executable, str(ROOT / "tools" / "migrate_files.py"), "--domain", "all"]
    return subprocess.run(cmd + (extra or []), capture_output=True, text=True, env=env)


def main() -> int:
    os.environ["FACTOPIA_RUNTIME"] = str(FIXTURE)
    os.environ.setdefault("FACTOPIA_STORAGE", "files")
    os.environ["FACTOPIA_PG_POOL_MAX"] = "4"

    from factopia import db
    from factopia import signals as signals_mod
    from factopia.repo import approval as repo_approval
    from factopia.repo import chain_hash
    from factopia.repo import ideas as repo_ideas
    from factopia.repo import ops as repo_ops
    from factopia.repo import paper as repo_paper
    from factopia.repo import portfolio as repo_portfolio
    from factopia.repo import signals as repo_signals
    from factopia.repo import agents as repo_agents

    health = db.health()
    if not health.get("ok"):
        print(f"数据库不可用：{health.get('error')}")
        return 2
    print(f"目标库 {health['database']}（{health['size']}）\n")

    build_fixture()
    try:
        print("① 七域回填（文件 → 库）")
        cleanup_rows()
        run1 = run_migrate()
        check("回填执行成功", run1.returncode == 0,
              (run1.stderr or "").strip()[:160] or run1.stdout.strip().splitlines()[-1])
        expect = {SUB: 3, IID: 1}
        check("审批事件入库", len(repo_approval.events(SUB)) == expect[SUB],
              f"{len(repo_approval.events(SUB))} 条")
        check("信号事件入库", len(repo_signals.events()) == 2, f"{len(repo_signals.events())} 条")
        check("线索事件入库", len(repo_ideas.events(IID)) == 1, f"{len(repo_ideas.events(IID))} 条")
        check("访问留痕入库", len(repo_ops.access_events()) == 2, f"{len(repo_ops.access_events())} 条")
        check("Agent 入库", repo_agents.load(AID)["state"] == "running")
        check("组合入库", repo_portfolio.load(RID, 1) is not None)
        check("模拟盘入库", repo_paper.load_enrollment(SID) is not None)

        run2 = run_migrate()
        skipped = all("书签命中" in ln for ln in run2.stdout.splitlines() if "[" in ln)
        check("二次执行幂等（全部书签命中）", run2.returncode == 0 and skipped, "没有重复写入")

        print("\n② 对账")
        ver = run_migrate(["--verify"])
        if "全部一致" not in ver.stdout:
            for line in ver.stdout.splitlines():
                if "DIFF" in line or "校验：" in line:
                    print(f"      {line.strip()}")
        check("文件与库一致", "全部一致" in ver.stdout,
              ver.stdout.strip().splitlines()[-1] if ver.stdout else (ver.stderr or "")[:120])

        print("\n③ 哈希链逐条等值（搬进库后重算必须一模一样）")
        for label, evs in (("approval", repo_approval.events(SUB)),
                           ("signals", repo_signals.events()),
                           ("ideas", repo_ideas.events(IID)),
                           ("access", repo_ops.access_events())):
            ok = all(chain_hash(e) == e["hash"] for e in evs)
            prev_ok = evs[0]["prev"] == ZERO if evs else False
            check(f"{label} 重算哈希一致", ok and prev_ok, f"{len(evs)} 条")

        print("\n④ 只追加表拒绝改写")
        for table, where in (("approval_event", "submission_id = %s"),
                             ("signal_event", "signal_id = %s"),
                             ("idea_event", "idea_id = %s")):
            key = {"approval_event": SUB, "signal_event": SID, "idea_event": IID}[table]
            blocked_u = blocked_d = False
            try:
                db.execute(f"UPDATE factopia.{table} SET note = 'x' WHERE {where}", (key,))
            except Exception:
                blocked_u = True
            try:
                db.execute(f"DELETE FROM factopia.{table} WHERE {where}", (key,))
            except Exception:
                blocked_d = True
            check(f"{table} 拒绝 UPDATE / DELETE", blocked_u and blocked_d,
                  f"UPDATE={'拒' if blocked_u else '通'} DELETE={'拒' if blocked_d else '通'}")

        print("\n⑤ db 模式续链（prev 必须接住上一条 hash）")
        os.environ["FACTOPIA_STORAGE"] = "db"
        head = repo_signals.events()[-1]["hash"]
        n_before = len(repo_signals.events())
        ev = signals_mod.append({"signal_id": SID, "market": "hk", "action": "pause",
                                 "role": "投资组合经理", "by": "smk", "note": "库里续链"})
        check("信号链续得上", ev["prev"] == head and len(repo_signals.events()) == n_before + 1,
              f"prev={ev['prev'][:8]}… head={head[:8]}…")
        check("库内链自洽", repo_signals.verify_chain()["ok"])

        head_acc = repo_ops.access_events()[-1]["hash"]
        ev_acc = repo_ops.append_access({"role": "hk", "role_name": "smk",
                                         "denied": "signals", "market": "fr"})
        check("访问迹链续得上", ev_acc["prev"] == head_acc, f"prev={ev_acc['prev'][:8]}…")

        print("\n⑥ 来源唯一键：并发只落一条")
        dup_err: list[Exception] = []
        winners: list[str] = []

        def writer(i: int) -> None:
            body = {"id": f"idea_smk_c{i}", "channel": "web", "title": "并发", "body": "x",
                    "language": "zh", "markets": ["hk"], "submitted_by": "smk", "role": "研究员",
                    "status": "new", "created_at": "2026-11-15T00:20:00+00:00"}
            try:
                repo_ideas.save(body, source_key="smk|concurrent")
                winners.append(f"idea_smk_c{i}")
            except Exception as exc:                      # DuplicateIdea
                dup_err.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("只有一个写入者成功", len(winners) == 1 and len(dup_err) == 1,
              f"成功 {winners} / 冲突 {[type(e).__name__ for e in dup_err]}")
        check("冲突方能拿到先到者编号",
              bool(dup_err) and getattr(dup_err[0], "idea_id", "") == winners[0],
              getattr(dup_err[0], "idea_id", "") if dup_err else "")

        print("\n⑦ 其余域在 db 模式的读写")
        state = repo_paper.order_event(SID, {"event": "placed", "order_id": "smk_o2", "symbol": "5",
                                             "side": "buy", "qty": 200, "placed_on": "2026-11-15",
                                             "mode": "shadow"}) or repo_paper.load_state(SID)
        check("挂单出现在 pending", any(p["order_id"] == "smk_o2" for p in state["pending"]),
              f"{len(state['pending'])} 条挂单")
        check("净值天数一致", len(repo_paper.nav(SID)) == 1, f"{len(repo_paper.nav(SID))} 天")
        repo_agents.write_round(AID, 2, "evaluation", {"score": 0.8})
        check("轮次可读写", repo_agents.read_round(AID, 2, "evaluation")["score"] == 0.8)
        check("报告可读写", (repo_agents.report(AID) or "").startswith("# 冒烟迭代报告"))
        dd = repo_portfolio.daily(RID, 1)
        check("逐日曲线列齐", {"date", "port", "bench"} <= set(dd.columns), f"{list(dd.columns)}")
        check("权重按市场归一", abs(repo_portfolio.weights(RID, 1)["weight_in_market"].sum() - 1) < 1e-6)
        check("费用汇总可读", repo_ops.cost_summary(3650)["rows"][0]["calls"] == 2,
              f"{repo_ops.cost_summary(3650)['rows']}")

    finally:
        os.environ.pop("FACTOPIA_STORAGE", None)
        try:
            cleanup()
        except Exception as exc:
            print(f"  清理异常：{exc}")
        print("\n" + "-" * 60)
        bad = [r for r in results if not r[1]]
        print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    return 0 if all(r[1] for r in results) else 1


def force_delete(table: str, where: str, params: tuple) -> None:
    """验收脚本专用清理：临时摘掉只追加触发器再删。

    生产路径永远不该这么干——留痕表只能追加纠正事件。这里是测试夹具，必须能回滚干净，
    所以显式关掉再打开，并且只作用于 `factopia.<table>` 这一张表。
    """
    from factopia import db

    trigs = [r[0] for r in db.query(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal",
        (f"factopia.{table}",))]
    for name in trigs:
        db.execute(f"ALTER TABLE factopia.{table} DISABLE TRIGGER {name}")
    try:
        db.execute(f"DELETE FROM factopia.{table} WHERE {where}", params)
    finally:
        for name in trigs:
            db.execute(f"ALTER TABLE factopia.{table} ENABLE TRIGGER {name}")


def cleanup_rows() -> None:
    """回填前清掉上一轮的同名样本，保证从空表开始。

    只删**本脚本造的行**（按编号前缀），绝不能整表清空——库里还有别的迁移批次的数据。
    """
    from factopia import db

    db.execute("DELETE FROM factopia.migration_file WHERE path LIKE %s", (f"{FIXTURE.as_posix()}%",))
    force_delete("approval_event", "submission_id = %s", (SUB,))
    force_delete("idea_event", "idea_id LIKE %s", ("idea_smk%",))
    force_delete("signal_event", "signal_id = %s", (SID,))
    db.execute("DELETE FROM factopia.approval_submission WHERE id = %s", (SUB,))
    db.execute("DELETE FROM factopia.signal_monitor WHERE signal_id = %s", (SID,))
    db.execute("DELETE FROM factopia.idea WHERE idea_id LIKE %s", ("idea_smk%",))
    db.execute("DELETE FROM factopia.agent WHERE agent_id = %s", (AID,))
    db.execute("DELETE FROM factopia.portfolio WHERE research_id = %s", (RID,))
    db.execute("DELETE FROM factopia.paper_enrollment WHERE signal_id = %s", (SID,))
    db.execute("DELETE FROM factopia.access_event WHERE actor = 'smk'")
    db.execute("DELETE FROM factopia.llm_cost WHERE model = 'smk-model'")


if __name__ == "__main__":
    sys.exit(main())
