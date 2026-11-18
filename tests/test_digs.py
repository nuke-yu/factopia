"""公告深挖（WF-04）单元测试（不依赖网络 / 真实回测）。

运行：python -m unittest tests.test_digs -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from factopia.agents import dig
from factopia.agents import policy as policy_mod
from factopia.text import common as tcommon

POLICY = {
    "max_rounds": 4, "budget_usd": 1.5, "max_hits": 20,
    "tools": ["query", "finish", "give_up"],
    "prompts": {"planner": "dig-planner-v1", "rewrite": "dig-rewrite-v1",
                "extract": "dig-extract-v1", "conclusion": "dig-conclusion-v1"},
}

CONCEPTS = {"risk_increase": "风险增加", "demand_weakening": "需求转弱",
            "cost_pressure": "成本压力", "regulatory_uncertainty": "监管不确定性"}
GLOSSARY = {"version": "test",
            "concepts": {c: {"zh_name": v, "en_name": v, "fr_name": v} for c, v in CONCEPTS.items()}}

USAGE = {"prompt_tokens": 10, "completion_tokens": 10, "usd": 0.0001}


def plan_json(action: str, args: dict | None = None, cites: list[str] | None = None,
              reason: str = "test") -> str:
    cites = cites if cites is not None else ["S1"]
    return json.dumps({"action": action, "rationale": {"text": reason, "cites": cites},
                       "args": args or {}})


def rewrite_json(q: str = "demand weakening", terms=None, concepts=None) -> dict:
    return {"query": q, "terms": terms or ["demand weakening", "需求转弱", "affaiblissement"],
            "concepts": concepts or ["demand_weakening"]}


def fact(statement: str, concept: str = "demand_weakening", cites: list[int] | None = None,
         evidence: str = "Demand weakened materially in H2.", direction: int = 1,
         date: str | None = None) -> dict:
    out = {"statement": statement, "concept": concept, "direction": direction,
           "cites": cites if cites is not None else [1], "evidence": evidence}
    if date:
        out["date"] = date
    return out


def extract_json(facts: list[dict] | None = None) -> dict:
    return {"facts": facts if facts is not None else [
        fact("H2 demand weakened materially.")]}


def conclusion_json() -> dict:
    return {"summary": "Demand weakened in H2.", "signal_note": "matches the platform signal.",
            "web_note": "Public sources report weakening demand.", "follow_ups": ["watch Q3 guidance"],
            "groups": [{"concept": "demand_weakening", "summary": "Demand weakened in H2.", "cites": ["F1"]}],
            "timeline": [{"date": "2024-09-15", "statement": "Demand weakened.", "cites": ["F1"]}]}


def web_result(title: str = "Portal reports demand weakness", url: str = "https://portal.example/1",
               snippet: str = "Demand weakening reported.", published: str = "2026-11-01",
               source: str = "bloomberg.com") -> dict:
    return {"title": title, "url": url, "snippet": snippet, "published": published, "source": source}


def give_up_json(reason: str = "Only one announcement available.") -> dict:
    return {"give_up": {"reason": reason, "evidence_gaps": ["more filings"], "suggestion": "widen scope"}}


def hit(para: int = 0, doc_id: str = "doc1", instrument: str = "FR001234",
        text: str = "Demand weakened materially in H2.", published: str = "2024-09-15T07:00:00Z",
        concepts=None, review: str = "done") -> dict:
    return {"dataset": "fr", "simulated": False, "source": "AMF", "licence": "x", "doc_id": doc_id,
            "paragraph": para, "company": "ACME", "instrument": instrument, "title": "H1 report",
            "url": None, "language": "en", "language_zh": "英文", "business_date": "2024-09-15",
            "published_at": published, "available_at": "2024-09-15T08:00:00Z", "effective_date": "2024-09-16",
            "original": text, "translation": {"zh": "下半年需求显著转弱。"}, "translation_confidence": 0.9,
            "concepts": concepts if concepts is not None else [
                {"concept": "demand_weakening", "name": "需求转弱", "direction": 1, "confidence": 0.9,
                 "negated": False, "evidence": "weakened"}],
            "matched": ["需求转弱"], "review_status": review, "model": "test", "glossary_version": "v1"}


class ScriptedChat:
    """按任务类型返回剧本化的大模型输出；plans / extracts 逐个消费，其余任务固定回答。"""

    def __init__(self, plans: list[str] | None = None, extracts: list[dict] | None = None,
                 rewrite: dict | None = None, conclusion: dict | None = None,
                 conclusion_lite: dict | None = None):
        self.plans = list(plans or [])
        self.extracts = list(extracts or [])
        self.rewrite = rewrite or rewrite_json()
        self.conclusion = conclusion or conclusion_json()
        self.conclusion_lite = conclusion_lite or give_up_json()
        self.calls: list[dict] = []
        self.usage = dict(USAGE)
        self.on_call = None

    def __call__(self, prompt, task, lite=False, role="main"):
        self.calls.append({"prompt": prompt, "task": task, "lite": lite})
        if self.on_call:
            self.on_call()
        if task == "公告深挖规划":
            if not self.plans:
                raise AssertionError(f"计划剧本耗尽（第 {len(self.calls)} 次调用）")
            return self.plans.pop(0), dict(self.usage), "test-pro"
        if task == "公告深挖查询改写":
            return json.dumps(self.rewrite), dict(self.usage), "test-lite"
        if task == "公告深挖提取":
            out = self.extracts.pop(0) if self.extracts else extract_json()   # 剧本耗尽回退默认事实
            return json.dumps(out), dict(self.usage), "test-pro"
        if task == "公告深挖结论":
            return json.dumps(self.conclusion_lite if lite else self.conclusion), dict(self.usage), \
                "test-lite" if lite else "test-pro"
        raise AssertionError(f"意外的大模型任务：{task}")


class DigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "jobs").mkdir(parents=True)
        config = self.root / "configs" / "agents"
        config.mkdir(parents=True)
        (config / "dig.yaml").write_text(yaml.safe_dump(POLICY, allow_unicode=True), encoding="utf-8")
        (self.root / "configs" / "text").mkdir(parents=True)
        (self.root / "configs" / "text" / "glossary.yaml").write_text(
            yaml.safe_dump({**GLOSSARY, "negation": {"zh": [], "en": [], "fr": []},
                            "regulatory_wording": {"zh": [], "en": [], "fr": []},
                            "key_financial_terms": {"zh": [], "en": [], "fr": []}},
                           allow_unicode=True), encoding="utf-8")
        fr = self.root / "runtime" / "text" / "fr"
        fr.mkdir(parents=True)
        (fr / "aligned.parquet").touch()
        self.search_out: list[dict] = []
        # 注意：patch.start() 返回的是 mock 对象，patch 对象本身要单独保存，tearDown 才能正确还原
        self.search_patch = mock.patch("factopia.text.search.search", side_effect=self._search)
        self.search_mock = self.search_patch.start()
        self.summarize_patch = mock.patch("factopia.text.search.summarize", return_value={
            "sentences": [{"text": "Demand weakened.", "cites": [1]}], "dropped": 0,
            "model": "test-pro", "usage": dict(USAGE)})
        self.summarize_mock = self.summarize_patch.start()
        self.llm_label_patch = mock.patch("factopia.text.understand.llm_label")
        self.llm_label_mock = self.llm_label_patch.start()
        self.web_configured_patch = mock.patch("factopia.research.websearch.configured", return_value=True)
        self.web_configured_mock = self.web_configured_patch.start()
        self.web_search_patch = mock.patch("factopia.research.websearch.search")
        self.web_search_mock = self.web_search_patch.start()
        self.patches = [
            mock.patch.object(dig, "DIGS_DIR", self.runtime / "digs"),
            mock.patch.object(policy_mod, "CONFIGS", self.root / "configs"),
            mock.patch.object(tcommon, "CONFIGS", self.root / "configs"),
            mock.patch.object(dig, "ds_dir", side_effect=lambda ds: self.root / "runtime" / "text" / ds),
            mock.patch.object(dig, "is_table", return_value=False),
            mock.patch.object(dig, "concept_names", return_value=dict(CONCEPTS)),
            mock.patch.object(dig, "glossary", return_value=GLOSSARY),
            mock.patch("factopia.research.draft.llm_config", return_value={"model": "test-pro"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches + [self.search_patch, self.summarize_patch, self.llm_label_patch,
                                          self.web_configured_patch, self.web_search_patch]):
            p.stop()
        self.tmp.cleanup()

    def _search(self, *a, **k):
        return self.search_out.pop(0) if self.search_out else {"query": a[0] if a else "", "results": []}

    def create(self, dataset: str = "fr", instrument: str = "FR001234", filing: dict | None = None,
               policy: dict | None = None, web_search: bool = False) -> dict:
        return dig.create(dataset, "ACME", instrument, filing, policy or POLICY, web_search=web_search)

    # ── 主路径 ──────────────────────────────────────────────

    def test_full_loop_finish(self):
        """检索 → 提取 → 检索 → 提取 → 无命中轮 → 收尾：风险画像产出，终态 done。"""
        rec = self.create()
        chat = ScriptedChat(
            plans=[plan_json("query", {"query": "demand weakening"}),
                   plan_json("query", {"query": "cost pressure"}, cites=["F1"]),
                   plan_json("query", {"query": "regulatory"}, cites=["F1", "F2"]),
                   plan_json("finish", {}, cites=["F1"])],
            extracts=[extract_json([fact("H2 demand weakened materially.")]),
                      extract_json([fact("Input costs rose sharply.", concept="cost_pressure",
                                         evidence="Input costs rose sharply in 2024.")])])
        self.search_out = [
            {"query": "demand", "results": [hit()]},
            {"query": "cost", "results": [hit(para=1, text="Input costs rose sharply in 2024.")]},
            {"query": "regulatory", "results": []},
        ]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["outcome"], "done")
        self.assertEqual(int(rec["round"]), 4)
        self.assertEqual(len(rec["facts"]), 2)
        self.assertEqual(rec["facts"][0]["date"], "2024-09-15")
        self.assertEqual(rec["conclusion"]["groups"][0]["concept"], "demand_weakening")
        self.assertEqual(rec["conclusion"]["web_note"], "Public sources report weakening demand.")
        tasks = [c["task"] for c in chat.calls]
        self.assertEqual(tasks.count("公告深挖规划"), 4)
        self.assertEqual(tasks.count("公告深挖查询改写"), 3)      # 无命中轮也改写；finish 轮不改写
        self.assertEqual(tasks.count("公告深挖提取"), 2)          # 无命中轮跳过提取
        self.assertEqual(tasks.count("公告深挖结论"), 1)
        self.assertTrue(any(c["lite"] for c in chat.calls if c["task"] == "公告深挖查询改写"))
        extract3 = dig.read_round(rec["dig_id"], 3, "extract")
        self.assertTrue(extract3["skipped"])

    def test_give_up_with_conclusion(self):
        """Planner 决定放弃：Lite 写结论，终态 insufficient。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("give_up", {"reason": "too few filings", "suggestion": "widen scope"})])
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "insufficient")
        self.assertEqual(rec["outcome"], "insufficient")
        self.assertEqual(rec["conclusion"]["give_up"]["reason"], "Only one announcement available.")
        self.assertTrue(any(c["lite"] and c["task"] == "公告深挖结论" for c in chat.calls))

    def test_max_rounds_then_close(self):
        """max_rounds=3 且 Planner 一直检索：满轮强制收尾出结论，不再有第 4 次规划。"""
        rec = self.create(policy={**POLICY, "max_rounds": 3})
        chat = ScriptedChat(
            plans=[plan_json("query", {"query": "q1"}),
                   plan_json("query", {"query": "q2"}, cites=["F1"]),
                   plan_json("query", {"query": "q3"}, cites=["F1"]),
                   plan_json("query", {"query": "q4"}, cites=["F1"])],    # 剧本不应被消费
            extracts=[extract_json()])
        self.search_out = [{"query": "q", "results": [hit()]} for _ in range(3)]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual(int(rec["round"]), 3)
        plans = [c for c in chat.calls if c["task"] == "公告深挖规划"]
        self.assertEqual(len(plans), 3)

    # ── 红队 ──────────────────────────────────────────────

    def test_rejects_unknown_action(self):
        """白名单外动作两次被拒 → 收尾为数据不足（无事实）。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("run_rdagent", {"loops": 3}),
                             plan_json("run_rdagent", {"loops": 3})])
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "insufficient")
        plan = dig.read_round(rec["dig_id"], 1, "plan")
        self.assertIn("不在白名单", plan["error"])
        plans = [c for c in chat.calls if c["task"] == "公告深挖规划"]
        self.assertEqual(len(plans), 2)

    def test_finish_requires_fact_cite(self):
        """只引用 [S1] 就要求收尾 → 被拒；两次后收尾为数据不足。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("finish", {}, cites=["S1"]),
                             plan_json("finish", {}, cites=["S1"])])
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "insufficient")
        self.assertIn("至少一条有效事实", dig.read_round(rec["dig_id"], 1, "plan")["error"])

    def test_plan_requires_cite(self):
        """检索动作不引用 [S1]/[F#] → 被拒。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("query", {"query": "q"}, cites=[]),
                             plan_json("query", {"query": "q"}, cites=[])])
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "insufficient")

    def test_query_markers_cleaned(self):
        """查询里的 [F#] 标记被清洗。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("query", {"query": "demand [F1] weakness"}),
                             plan_json("give_up", {"reason": "no"})])
        self.search_out = [{"query": "q", "results": []}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        plan = dig.read_round(rec["dig_id"], 1, "plan")
        self.assertEqual(plan["args"]["query"], "demand weakness")

    def test_search_company_scoped(self):
        """检索命中只保留种子公司公告，别家公司的段落被过滤。"""
        rec = self.create()
        chat = ScriptedChat([plan_json("query", {"query": "demand"}),
                             plan_json("finish", {}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit(), hit(para=1, instrument="OTHER")]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        search = dig.read_round(rec["dig_id"], 1, "search")
        self.assertEqual(len(search["hits"]), 1)
        self.assertEqual(search["scoped_out"], 1)

    def test_extract_cites_must_exist(self):
        """事实引用不存在的段落编号被丢弃。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("give_up", {"reason": "no"})],
                            extracts=[extract_json([fact("x", cites=[5])])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["facts"], [])
        extract = dig.read_round(rec["dig_id"], 1, "extract")
        self.assertEqual(extract["dropped"][0]["reason"], "no_valid_cites")

    def test_extract_date_from_hit(self):
        """事实日期取自引用公告的发布时间，模型编造的日期被忽略。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("give_up", {"reason": "no"})],
                            extracts=[extract_json([fact("x", date="2023-01-01")])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["facts"][0]["date"], "2024-09-15")

    def test_extract_evidence_verbatim(self):
        """证据不是原文 / 译文片段的事实被丢弃。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("give_up", {"reason": "no"})],
                            extracts=[extract_json([fact("x", evidence="Totally invented sentence.")])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["facts"], [])
        extract = dig.read_round(rec["dig_id"], 1, "extract")
        self.assertEqual(extract["dropped"][0]["reason"], "evidence_not_verbatim")

    def test_extract_unknown_concept(self):
        """概念不在术语表的事实被丢弃。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("give_up", {"reason": "no"})],
                            extracts=[extract_json([fact("x", concept="totally_new_concept")])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["facts"], [])
        extract = dig.read_round(rec["dig_id"], 1, "extract")
        self.assertEqual(extract["dropped"][0]["reason"], "unknown_concept")

    def test_extract_dedup(self):
        """两轮提取同一条事实只入列一次。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("query", {"query": "demand again"}, cites=["F1"]),
                                   plan_json("finish", {}, cites=["F1"])],
                            extracts=[extract_json(), extract_json()])
        self.search_out = [{"query": "q", "results": [hit()]},
                           {"query": "q", "results": [hit()]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(len(rec["facts"]), 1)

    def test_budget_stops_without_llm(self):
        """预算 0：不进规划、不做任何 LLM 调用，直接数据不足。"""
        rec = self.create()
        rec["policy"] = {**POLICY, "budget_usd": 0.0}
        dig.save(rec)
        chat = ScriptedChat([plan_json("query", {"query": "q"})])
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "insufficient")
        self.assertIn("预算", rec["conclusion"]["give_up"]["reason"])
        self.assertEqual(chat.calls, [])

    def test_cancel_stops(self):
        """取消请求在下一步前生效：终态 stopped，不产生新调用。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "q"}),
                                   plan_json("query", {"query": "q2"}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        cancel = {"on": False}

        def flip():
            cancel["on"] = True

        chat.on_call = flip

        def should_stop():
            return cancel["on"]

        dig.advance(rec, chat=chat, should_stop=should_stop)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "stopped")
        plans = [c for c in chat.calls if c["task"] == "公告深挖规划"]
        self.assertEqual(len(plans), 1)

    def test_idempotent_create(self):
        """同一公司不能有两个进行中的深挖。"""
        self.create()
        with self.assertRaises(dig.DigError):
            self.create()

    def test_requires_aligned_results(self):
        """数据集尚无理解结果（无 aligned.parquet）时创建失败。"""
        with self.assertRaises(dig.DigError) as ctx:
            self.create(dataset="demo_hk", instrument="HK0001")
        self.assertIn("尚未生成", str(ctx.exception))

    def test_summarize_used(self):
        """summarize_first：先复用摘要原语，再提取。"""
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand", "summarize_first": True}),
                                   plan_json("finish", {}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "done")
        self.summarize_mock.assert_called_once()
        summary = dig.read_round(rec["dig_id"], 1, "summarize")
        self.assertEqual(len(summary["sentences"]), 1)
        extract = dig.read_round(rec["dig_id"], 1, "extract")
        self.assertEqual(len(extract["added_facts"]), 1)

    def test_annotate_reuses_labels(self):
        """annotate_first：已由大模型标注的段落不重复标注。"""
        doc_dir = self.root / "runtime" / "text" / "fr" / "docs"
        doc_dir.mkdir(parents=True)
        (doc_dir / "doc1.json").write_text(json.dumps({"paragraphs": [
            {"i": 0, "text": "Demand weakened materially in H2.", "sha": "aaaa"}]}), encoding="utf-8")
        lab_dir = self.root / "runtime" / "text" / "fr" / "labels"
        lab_dir.mkdir(parents=True)
        (lab_dir / "doc1.json").write_text(json.dumps({"language": "en", "labels": [
            {"i": 0, "sha": "aaaa", "method": "llm", "concepts": [],
             "translation": {}, "translation_confidence": 0.9, "flags": []}]}), encoding="utf-8")
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand", "annotate_first": True}),
                                   plan_json("finish", {}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit(doc_id="doc1", para=0)]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "done")
        annotate = dig.read_round(rec["dig_id"], 1, "annotate")
        self.assertEqual(annotate["labelled"], 0)
        self.llm_label_mock.assert_not_called()

    def test_annotate_labels_new_paragraphs(self):
        """annotate_first：未标注段落补大模型标注并写入 labels 文件（进复核队列）。"""
        doc_dir = self.root / "runtime" / "text" / "fr" / "docs"
        doc_dir.mkdir(parents=True)
        (doc_dir / "doc1.json").write_text(json.dumps({"paragraphs": [
            {"i": 0, "text": "Demand weakened materially in H2.", "sha": "aaaa"}]}), encoding="utf-8")
        label = {"concepts": [{"concept": "demand_weakening", "direction": 1, "negated": False,
                               "confidence": 0.9, "evidence": "weakened"}],
                 "translation": {"zh": "需求显著转弱。"}, "translation_confidence": 0.9}
        self.llm_label_mock.return_value = ({0: label}, dict(USAGE))
        rec = self.create()
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand", "annotate_first": True}),
                                   plan_json("finish", {}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit(doc_id="doc1", para=0)]}]
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        annotate = dig.read_round(rec["dig_id"], 1, "annotate")
        self.assertEqual(annotate["labelled"], 1)
        self.llm_label_mock.assert_called_once()
        lab = json.loads((self.root / "runtime" / "text" / "fr" / "labels" / "doc1.json").read_text(encoding="utf-8"))
        self.assertEqual(lab["labels"][0]["method"], "llm")
        self.assertEqual(lab["labels"][0]["concepts"][0]["concept"], "demand_weakening")

    # ── 联网搜索（可选，勾选「联网搜索」时）──────────────────────

    def test_web_search_gathers(self):
        """勾选联网：启动时有界检索 3 个查询，结果去重后载入，写在第 0 轮检查点。"""
        rec = self.create(web_search=True)
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("finish", {}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        self.web_search_mock.side_effect = lambda q, before="", count=5: {
            "configured": True, "provider": "bailian", "model": "qwen-plus", "warning": "请人工核验",
            "results": [web_result()], "error": None}
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual(self.web_search_mock.call_count, 3)          # 公司新闻 / 风险 / 业绩展望
        self.assertEqual(len(rec["web"]["results"]), 1)               # 三个查询同一条结果，去重
        self.assertEqual(rec["web"]["provider"], "bailian")
        self.assertEqual(rec["web"]["results"][0]["title"], "Portal reports demand weakness")
        ws = dig.read_round(rec["dig_id"], 0, "websearch")
        self.assertEqual(ws["queries"][0]["n"], 1)

    def test_web_search_unconfigured(self):
        """勾选联网但未配置搜索：创建即被拒（422），不静默跳过。"""
        self.web_configured_mock.return_value = False
        with self.assertRaises(dig.DigError) as ctx:
            self.create(web_search=True)
        self.assertIn("未配置联网搜索", str(ctx.exception))

    def test_web_search_failure_continues(self):
        """联网检索失败：记录错误，不阻断深挖循环。"""
        rec = self.create(web_search=True)
        chat = ScriptedChat(plans=[plan_json("query", {"query": "demand"}),
                                   plan_json("finish", {}, cites=["F1"])])
        self.search_out = [{"query": "q", "results": [hit()]}]
        self.web_search_mock.side_effect = RuntimeError("network down")
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "done")
        self.assertIn("network down", rec["web"]["error"])

    def test_web_search_budget_skipped(self):
        """预算 0：跳过联网检索，直接收尾。"""
        rec = self.create(web_search=True, policy={**POLICY, "budget_usd": 0.0})
        chat = ScriptedChat([plan_json("query", {"query": "q"})])
        dig.advance(rec, chat=chat)
        rec = dig.load(rec["dig_id"])
        self.assertEqual(rec["state"], "insufficient")
        self.web_search_mock.assert_not_called()
        self.assertIn("预算", rec["web"]["error"])


if __name__ == "__main__":
    unittest.main()
