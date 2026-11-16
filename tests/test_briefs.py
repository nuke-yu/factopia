"""AI 调研（WF-03）单元测试（不依赖网络 / 真实回测）。

运行：python -m unittest tests.test_briefs -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from factopia.agents import brief
from factopia.agents import policy as policy_mod
from factopia.research import draft as draft_mod


POLICY = {
    "max_rounds": 3, "budget_usd": 0.5, "mandate": "demo_crossborder_hk", "horizon": 20,
    "tools": ["search_internal", "search_external", "evidence", "draft", "give_up"],
    "prompts": {"planner": "brief-planner-v1", "conclusion": "brief-conclusion-v1"},
}

IDEA = {
    "id": "idea_20261117000000_0001", "channel": "hypothesis", "channel_zh": "投资假设",
    "title": "Volume-price divergence predicts negative returns",
    "body": "When volume spikes on down days, the stock tends to reverse.",
    "markets": ["hk"], "ai_hypothesis": {"hypothesis": "Volume-price divergence predicts negative returns.",
                                          "direction": "volume-price divergence"},
}

USAGE = {"prompt_tokens": 10, "completion_tokens": 10, "usd": 0.0001}


def plan_json(action: str, args: dict | None = None, cites: list[str] | None = None,
              reason: str = "test") -> str:
    cites = cites if cites is not None else ["I1"]
    return json.dumps({"action": action, "rationale": {"text": reason, "cites": cites},
                       "args": args or {}})


def conclusion_json(reason: str = "no data") -> str:
    return json.dumps({"reason": reason, "evidence_gaps": ["fundamentals not available"],
                       "suggestion": "find a price-volume angle"})


class ScriptedChat:
    """按顺序返回剧本化的大模型输出；plans 是已编码的 JSON 字符串，逐个消费，结论用 conclusion。"""

    def __init__(self, plans: list[str], conclusion: dict | None = None):
        self.plans = list(plans)
        self.calls: list[dict] = []
        self.conclusion = conclusion or {"reason": "no data", "evidence_gaps": [], "suggestion": ""}
        self.usage = dict(USAGE)
        self.on_call = None

    def __call__(self, prompt, task, lite=False, role="main"):
        self.calls.append({"prompt": prompt, "task": task, "lite": lite})
        if self.on_call:
            self.on_call()
        if task == "AI 调研规划":
            if not self.plans:
                raise AssertionError(f"计划剧本耗尽（第 {len(self.calls)} 次调用）")
            return self.plans.pop(0), dict(self.usage), "test-pro"
        return json.dumps(self.conclusion), dict(self.usage), "test-lite"


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "jobs").mkdir(parents=True)
        config = self.root / "configs" / "agents"
        config.mkdir(parents=True)
        (config / "brief.yaml").write_text(yaml.safe_dump(POLICY, allow_unicode=True), encoding="utf-8")
        self.patches = [
            mock.patch.object(brief, "BRIEFS_DIR", self.runtime / "briefs"),
            mock.patch.object(policy_mod, "CONFIGS", self.root / "configs"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def create(self, idea: dict | None = None, policy: dict | None = None) -> dict:
        return brief.create("idea_20261117000000_0001", idea or IDEA, policy or POLICY)

    def mock_draft(self) -> mock.MagicMock:
        return mock.patch.object(draft_mod, "make_draft", return_value={
            "draft": {"id": "test_brief", "hypothesis": "H"}, "meta": {"model": "test-pro",
                                                                        "prompt_tokens": 5,
                                                                        "completion_tokens": 5},
            "evidence": {"alpha158": [], "past_runs": [], "internal": []}})

    # ── 主路径 ──────────────────────────────────────────────

    def test_full_loop_draft(self):
        """内部检索 → 证据包 → 起草：事实累积、草稿产出、终态 done。"""
        rec = self.create()
        chat = ScriptedChat([
            plan_json("search_internal", {"query": "volume-price divergence"}),
            plan_json("evidence", {}, cites=["F1"]),
            plan_json("draft", {}, cites=["F1"]),
        ])
        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[
                {"kind": "factor", "kind_zh": "因子库", "id": "x", "title": "CNTV", "score": 0.9,
                 "prompt": "volume factor", "ref": "lib"}]), \
                mock.patch("factopia.research.evidence.alpha158_findings", return_value=[]), \
                mock.patch("factopia.research.evidence.past_runs", return_value=[]), self.mock_draft():
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["outcome"], "draft")
        self.assertEqual(len(rec["facts"]), 1)          # 证据包在测试中被置空
        self.assertEqual(rec["facts"][0]["source"], "internal")
        self.assertIsNotNone(rec["draft"])
        self.assertEqual(int(rec["round"]), 3)
        self.assertEqual(len(chat.calls), 3)            # 3 次 Pro 规划 + make_draft（被 mock）

    def test_give_up_with_conclusion(self):
        """Planner 决定放弃：Lite 写结论，终态 not_researchable。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("give_up", {"reason": "fundamentals unavailable", "suggestion": "try price-volume"})],
                            conclusion={"reason": "Fundamentals are not connected.", "evidence_gaps": ["fundamentals"],
                                        "suggestion": "Try a price-volume hypothesis."})
        brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "not_researchable")
        self.assertEqual(rec["outcome"], "not_researchable")
        self.assertIn("Fundamentals", rec["conclusion"]["reason"])
        self.assertTrue(any(c["lite"] for c in chat.calls))    # 结论用 Lite

    def test_max_rounds_then_close(self):
        """max_rounds=2 且 Planner 一直检索：满轮强制收尾起草，不再有第 3 次规划。"""
        rec = self.create(policy={**POLICY, "max_rounds": 2})
        chat = ScriptedChat([
            plan_json("search_internal", {"query": "q1"}),
            plan_json("search_internal", {"query": "q2"}, cites=["F1"]),
            plan_json("draft", {}, cites=["F1"]),       # 剧本不应被消费
        ])
        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[
                {"kind": "factor", "kind_zh": "因子库", "id": "x", "title": "T", "score": 0.9,
                 "prompt": "snippet", "ref": "lib"}]), self.mock_draft():
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual(int(rec["round"]), 2)
        plans = [c for c in chat.calls if c["task"] == "AI 调研规划"]
        self.assertEqual(len(plans), 2)

    def test_external_facts_reach_draft(self):
        """联网检索的事实并入 make_draft 的 external_evidence。"""
        rec = self.create()
        chat = ScriptedChat([
            plan_json("search_external", {"query": "divergence literature"}),
            plan_json("draft", {}, cites=["F1"]),
        ])
        search_out = {"configured": True, "provider": "bailian", "results": [
            {"title": "Test Paper", "published": "2021-05-01", "url": "https://x/1",
             "snippet": "divergence predicts reversal"}]}
        with mock.patch("factopia.research.websearch.search", return_value=search_out), self.mock_draft() as md:
            brief.advance(rec, chat=chat)
        inputs = md.call_args[0][0]
        self.assertEqual(inputs["external_evidence"][0]["title"], "Test Paper")
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["facts"][0]["published"], "2021-05-01")

    # ── 红队 ──────────────────────────────────────────────

    def test_rejects_unknown_tool(self):
        """白名单外工具两次被拒 → 收尾为 not_researchable（无事实）。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("run_rdagent", {"loops": 3}),
                             plan_json("run_rdagent", {"loops": 3})])
        brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "not_researchable")
        plan = brief.read_round(rec["brief_id"], 1, "plan")
        self.assertIn("不在白名单", plan["error"])
        plans = [c for c in chat.calls if c["task"] == "AI 调研规划"]
        self.assertEqual(len(plans), 2)                 # 两次尝试后不再规划

    def test_draft_requires_fact_cite(self):
        """只引用 [I1] 就要求起草 → 被拒；两次后收尾。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("draft", {}, cites=["I1"]),
                             plan_json("draft", {}, cites=["I1"])])
        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[]):
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "not_researchable")
        self.assertIn("至少一条有效事实", brief.read_round(rec["brief_id"], 1, "plan")["error"])

    def test_plan_requires_cite(self):
        """检索动作不引用 [I1]/[F#] → 被拒。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("search_internal", {"query": "q"}, cites=[]),
                             plan_json("search_internal", {"query": "q"}, cites=[])])
        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[]):
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "not_researchable")

    def test_query_year_cleaned(self):
        """查询里的 2022+ 年份被清洗（MINE-11）。"""
        rec = self.create()
        chat = ScriptedChat([
            plan_json("search_internal", {"query": "factor 2023 earnings reversal"}),
            plan_json("give_up", {"reason": "no"}),
        ])
        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[]):
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        plan = brief.read_round(rec["brief_id"], 1, "plan")
        self.assertNotIn("2023", plan["args"]["query"])

    def test_idempotent_create(self):
        """同一线索不能有两个进行中的调研。"""
        self.create()
        with self.assertRaises(brief.BriefError):
            self.create()

    def test_budget_stops_without_llm(self):
        """预算 0：不进规划、不做结论 LLM 调用，直接 not_researchable。"""
        rec = self.create(policy={**POLICY, "budget_usd": 0.0})
        chat = ScriptedChat([plan_json("search_internal", {"query": "q"})])
        brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "not_researchable")
        self.assertIn("预算", rec["conclusion"]["reason"])
        self.assertEqual(chat.calls, [])                # 一分钱也没花

    def test_budget_stops_mid_loop(self):
        """预算在运行中耗尽：满额后不再规划，收尾。"""
        rec = self.create(policy={**POLICY, "budget_usd": 0.5})
        chat = ScriptedChat([
            plan_json("search_internal", {"query": "q"}),
            plan_json("search_internal", {"query": "q2"}, cites=["F1"]),
        ])
        chat.usage = {"prompt_tokens": 10, "completion_tokens": 10, "usd": 0.5}
        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[
                {"kind": "factor", "kind_zh": "因子库", "id": "x", "title": "T", "score": 0.9,
                 "prompt": "snippet", "ref": "lib"}]):
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "not_researchable")
        self.assertIn("预算", rec["conclusion"]["reason"])
        plans = [c for c in chat.calls if c["task"] == "AI 调研规划"]
        self.assertEqual(len(plans), 1)                 # 第二次规划因预算未执行

    def test_cancel_stops(self):
        """取消请求在下一步前生效：终态 stopped，不产生新调用。"""
        rec = self.create()
        chat = ScriptedChat([
            plan_json("search_internal", {"query": "q"}),
            plan_json("search_internal", {"query": "q2"}, cites=["F1"]),
        ])
        cancel = {"on": False}

        def flip():
            cancel["on"] = True

        chat.on_call = flip                               # 第一次规划后请求取消

        def should_stop():
            return cancel["on"]

        with mock.patch.object(draft_mod, "internal_knowledge", return_value=[]):
            brief.advance(rec, chat=chat, should_stop=should_stop)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "stopped")
        plans = [c for c in chat.calls if c["task"] == "AI 调研规划"]
        self.assertEqual(len(plans), 1)                  # 第二次规划被取消挡下

    def test_evidence_facts_added(self):
        """证据包工具产出事实，且不调用大模型检索。"""
        rec = self.create()
        chat = ScriptedChat([
            plan_json("evidence", {"markets": ["hk"], "horizon": 20}),
            plan_json("draft", {}, cites=["F1"]),
        ])
        alpha = [{"id": "a", "market": "hk", "kind": "alpha158", "source": "src",
                  "period": {"start": "2019-01-01", "end": "2021-12-31"}, "significant": 1,
                  "factors": [{"name": "CNTV", "desc": "volume", "t": 3.5, "ric": 0.02, "label": "x"}]}]
        with mock.patch("factopia.research.evidence.alpha158_findings", return_value=alpha), \
                mock.patch("factopia.research.evidence.past_runs", return_value=[]), self.mock_draft():
            brief.advance(rec, chat=chat)
        rec = brief.load(rec["brief_id"])
        self.assertEqual(rec["state"], "done")
        self.assertTrue(any(f["source"] == "evidence" for f in rec["facts"]))

    def test_default_markets_when_idea_has_none(self):
        """线索没选市场时默认覆盖全部三个市场。"""
        rec = self.create(idea={**IDEA, "markets": []})
        self.assertEqual(rec["idea"]["markets"], ["hk", "uk", "fr"])


if __name__ == "__main__":
    unittest.main()
