"""研究迭代 Agent P0 的单元测试（不依赖网络 / 大模型 / 真实运行目录）。

运行：python -m unittest tests.test_agents -v
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from factopia import runstore
from factopia.agents import loop, observe, policy as policy_mod, store as astore, tools
from factopia.research import store as rstore


SPEC = {
    "id": "test_study",
    "version": 1,
    "title_zh": "测试研究",
    "hypothesis": "Volatility-adjusted momentum predicts future returns.",
    "hypothesis_zh": "波动率调整后的动量预测未来收益。",
    "directions": ["volatility-adjusted momentum"],
    "exclusions": ["plain short-term reversal"],
    "falsification": "Hong Kong must show a positive rank IC; annual return with cost must be above the baseline; "
                     "max drawdown must not be worse than the baseline.",
    "markets": ["hk", "uk"],
    "horizon": 20,
    "mandate": "demo_crossborder_hk",
    "signal_source": "price_volume",
    "constraints": [],
    "ai_draft": {},
}

POLICY = {
    "max_rounds": 1, "budget_usd": 2.0, "no_improve_patience": 2,
    "human_gate": "none", "tools": ["new_version", "stop"],
    "costly_tools": ["new_version"],
    "prompts": {"critic": "iteration-critic-v1", "planner": "iteration-planner-v1",
                "evaluator": "iteration-evaluator-v1"},
}


class AgentP0Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.research = self.runtime / "research" / "test_study"
        self.research.mkdir(parents=True)
        (self.research / "v1.yaml").write_text(yaml.safe_dump(SPEC, allow_unicode=True), encoding="utf-8")
        (self.runtime / "jobs").mkdir(parents=True)

        run = self.runtime / "results" / "runs" / "hk_h20_20260101000000"
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({
            "run_id": "hk_h20_20260101000000", "market": "hk", "horizon": 20,
            "research": {"id": "test_study", "version": 1}, "status": "done",
            "cost_usd": 0.1, "context_leak": False, "rounds": 2, "adopted": 1,
            "settings": {"train_start": "2015-01-01", "train_end": "2020-12-31",
                         "valid_start": "2021-02-01", "valid_end": "2021-12-31",
                         "test_start": "2022-02-01", "test_end": "2023-12-31"},
            "baseline": {"metrics": {"annual_return_with_cost": 0.05, "rank_ic": 0.01}},
        }), encoding="utf-8")
        (run / "rounds.json").write_text(json.dumps([
            {"loop": 0, "hypothesis": "h0", "metrics": {"rank_ic": -0.02, "annual_return_with_cost": 0.06,
                                                         "max_drawdown_with_cost": -0.2},
             "decision": False, "factors": []},
            {"loop": 1, "hypothesis": "h1", "metrics": {"rank_ic": -0.01, "annual_return_with_cost": 0.12,
                                                         "max_drawdown_with_cost": -0.1},
             "decision": True, "factors": [{"name": "f1"}]},
        ]), encoding="utf-8")
        for sub in ("diagnostics", "validation", "quantile"):
            (run / sub).mkdir()
        (run / "diagnostics" / "1.json").write_text(json.dumps({
            "cost": {"gross_excess": 0.18, "net_excess": -0.02, "cost_drag": 0.2, "tax_share": 0.3,
                     "breakeven_cost_multiple": 0.9, "turnover_annual": 5.2, "holding_days": 20},
            "liquidity": {"capacity_base": 40_000_000, "low_liquidity_weight": 0.3},
            "fx": {"same_currency": True, "fx_contribution": 0.0},
            "exposure": {"max_sector_weight": 0.6},
            "signal": {"neutral_retained": 0.3, "year_sign_consistency": 0.4,
                       "redundancy": {"baseline_model": 0.8}},
            "economic": {"meaningful": False, "label": "扣成本后不再具有经济意义"},
            "backtest_note": "pre-fix",
        }), encoding="utf-8")
        (run / "validation" / "1.json").write_text(json.dumps({
            "status": "done",
            "ai": {"test": {"annual_return_with_cost_all": [0.10, 0.12], "ties": None},
                   "holdout": {"rank_ic": 0.01, "rank_ic_t": 2.1,
                               "annual_return_with_cost_all": [0.10, 0.11]}},
            "baseline": {"test": {"annual_return_with_cost_all": [0.05, 0.06]},
                         "holdout": {"rank_ic": 0.0, "rank_ic_t": 0.1,
                                     "annual_return_with_cost_all": [0.04, 0.05]}},
            "factors": [{"match_share": 0.99}],
            "placebo": {"n": 20, "share_ge_ai": 0.2, "median": 0.05},
        }), encoding="utf-8")
        (run / "quantile" / "1.json").write_text(json.dumps({
            "tie_share": 0.33, "spread_mean": -0.01, "spread_positive_share": 0.4,
            "groups": 5, "horizon": 20, "days": 100,
        }), encoding="utf-8")

        config = self.root / "configs" / "agents"
        config.mkdir(parents=True)
        (config / "iteration.yaml").write_text(yaml.safe_dump(POLICY, allow_unicode=True), encoding="utf-8")

        self.patches = [
            mock.patch.object(astore, "AGENTS_DIR", self.runtime / "agents"),
            mock.patch.object(runstore, "RUNS_DIR", self.runtime / "results" / "runs"),
            mock.patch.object(rstore, "RESEARCH_DIR", self.runtime / "research"),
            mock.patch.object(rstore, "JOBS_DIR", self.runtime / "jobs"),
            mock.patch.object(policy_mod, "CONFIGS", self.root / "configs"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_policy_load_and_tool_filter(self):
        policy = policy_mod.load()
        self.assertEqual(policy["max_rounds"], 1)
        self.assertEqual(policy_mod.available_tools(policy, tools.REGISTRY), ["new_version", "stop"])
        bad = self.root / "configs" / "agents" / "bad.yaml"
        bad.write_text("human_gate: sometimes\n", encoding="utf-8")
        with self.assertRaises(policy_mod.PolicyError):
            policy_mod.load(bad)

    def test_state_transitions(self):
        agent = astore.create("test_study", 1, POLICY, ["hk"], 20)
        loop._advance(agent, "observing")
        self.assertEqual(agent["state"], "observing")
        with self.assertRaises(loop.AgentStateError):
            loop._advance(agent, "done")
        loop._advance(agent, "diagnosing")
        loop._advance(agent, "planning")
        loop._advance(agent, "stopped")
        self.assertEqual(agent["state"], "stopped")

    def test_citation_filter(self):
        data = {
            "primary_cause": {"text": "Ranking is not the source.", "cites": ["F1", "F99"]},
            "market_findings": [{"market": "hk", "text": "dropped", "cites": ["F99"]}],
            "risks": [{"text": "Tie handling matters.", "cites": ["F2"]}],
            "confidence": "medium",
            "what_would_change_my_mind": "none",
        }
        out = loop._filter_diagnosis(data, {"F1", "F2"})
        self.assertEqual(out["primary_cause"]["cites"], ["F1"])
        self.assertEqual(out["market_findings"], [])
        self.assertEqual(out["risks"][0]["cites"], ["F2"])
        self.assertEqual(out["dropped"], 1)

    def test_observe_facts_are_redacted(self):
        facts = observe.build("test_study", 1)
        ids = [f["id"] for f in facts["facts"]]
        self.assertEqual(ids, [f"F{i}" for i in range(1, len(ids) + 1)])
        redacted = [f for f in facts["facts"] if f["redacted"]]
        self.assertTrue(redacted)
        for f in redacted:
            self.assertNotIn("value", f)
            self.assertIsNone(re.search(r"\d+\.\d+", f["text"]), f["text"])
            for forbidden in ("-0.01", "0.12", "0.33", "-0.1", "0.2"):
                self.assertNotIn(forbidden, f["text"])
        self.assertTrue(any(f["period"] == "test (status only)" for f in redacted))
        self.assertTrue(any(f["period"] == "test (qualitative)" for f in redacted))
        self.assertIn("label", facts["summary"]["overall"])

    def test_tool_allowlist(self):
        agent = astore.create("test_study", 1, POLICY, ["hk"], 20)
        ctx = {"agent_id": agent["agent_id"], "round": 1, "agent": agent, "spec": SPEC,
               "research_id": "test_study", "version": 1, "fact_ids": ["F1"]}
        with self.assertRaises(tools.ToolError):
            tools.execute("redundancy", {}, ctx)
        with self.assertRaises(tools.ToolError):
            tools.execute("new_version", {"changes": {}, "cites": ["F1"]}, ctx)
        out = tools.execute("stop", {"reason": "done"}, ctx)
        self.assertEqual(out["status"], "stopped")

    def test_dry_run_loop(self):
        responses = [
            json.dumps({
                "primary_cause": {"text": "Ranking skill is not the source of the return.", "cites": ["F1"]},
                "market_findings": [{"market": "hk", "text": "The adopted round failed the direction check.",
                                     "cites": ["F2"]}],
                "risks": [], "confidence": "medium",
                "what_would_change_my_mind": "A positive rank IC in a new version.",
            }),
            json.dumps({
                "action": "new_version",
                "rationale": {"text": "Refine the definition and re-test.", "cites": ["F1"]},
                "args": {"changes": {"hypothesis": "Revised hypothesis", "direction": "Revised direction",
                                     "markets": ["hk"], "horizon": 20}, "reason": "test"},
                "expected_outcome": "Higher validation rank IC",
                "success_criteria": {"metric": "validation.rank_ic", "direction": "increase",
                                     "min_delta": 0.005, "guardrails": {}},
                "alternatives_rejected": [],
                "stop_condition": "two rounds without improvement",
            }),
        ]

        def fake_chat(prompt, task):
            return responses.pop(0), {"prompt_tokens": 10, "completion_tokens": 5, "usd": 0.0001}, "fake-model"

        agent = loop.run("test_study", 1, rounds=1, dry_run=True, policy=POLICY, chat=fake_chat)
        self.assertEqual(agent["state"], "stopped")
        self.assertIn("dry-run", agent["stop_reason"])
        diagnosis = astore.read_round(agent["agent_id"], 1, "diagnosis")
        self.assertEqual(diagnosis["primary_cause"]["cites"], ["F1"])
        plan = astore.read_round(agent["agent_id"], 1, "plan")
        self.assertEqual(plan["action"], "new_version")
        self.assertTrue((astore.dir_path(agent["agent_id"]) / "report.md").exists())
        trace = (astore.dir_path(agent["agent_id"]) / "trace.jsonl").read_text(encoding="utf-8")
        self.assertIn('"role": "critic"', trace)
        self.assertIn('"role": "planner"', trace)


if __name__ == "__main__":
    unittest.main()
