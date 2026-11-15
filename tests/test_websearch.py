"""联网搜索（可选）的单元测试：不依赖网络，验证配置提示、日期过滤与结果归一化。"""
import os
import unittest
from unittest import mock

from factopia.research import websearch


class WebSearchTests(unittest.TestCase):
    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"FACTOPIA_SEARCH_PROVIDER": "bocha", "FACTOPIA_SEARCH_API_KEY": ""}, clear=False):
            r = websearch.search("volume reversal")
        self.assertFalse(r["configured"])
        self.assertIn("FACTOPIA_SEARCH_API_KEY", r["error"])
        self.assertEqual(r["results"], [])

    def test_bailian_reuses_llm_key(self):
        env = {"FACTOPIA_SEARCH_PROVIDER": "bailian", "FACTOPIA_LLM_API_KEY": "bailian-key",
               "FACTOPIA_LLM_BASE_URL": "http://example.test", "FACTOPIA_SEARCH_MODEL": "qwen-plus"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = websearch.config()
        self.assertEqual(cfg["provider"], "bailian")
        self.assertEqual(cfg["api_key"], "bailian-key")
        self.assertEqual(cfg["model"], "qwen-plus")
        rows = websearch._normalize_bailian([
            {"title": "Source without URL", "url": "", "published": "2020-01-01", "snippet": "finding"},
            {"title": "", "url": "https://example.com", "published": "2020-01-01", "snippet": "no title"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "")
        self.assertEqual(rows[0]["source"], "百炼内置搜索")

    def test_filter_by_date_rejects_unknown_and_future(self):
        rows = [{"published": "2020-01-01"}, {"published": "2022-01-01"}, {"published": None}]
        kept, excluded = websearch._filter_by_date(rows, "2021-12-31")
        self.assertEqual(kept, [{"published": "2020-01-01"}])
        self.assertEqual(excluded, 2)

    def test_bocha_normalization(self):
        rows = websearch._normalize_bocha([
            {"name": "Volume and reversal", "url": "https://example.com/a",
             "snippet": "A study before the test period.", "datePublished": "2020-05-01T00:00:00+08:00",
             "siteName": "Example"},
            {"name": "", "url": "https://example.com/b", "snippet": "no title"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Volume and reversal")
        self.assertEqual(rows[0]["published"], "2020-05-01")
        self.assertEqual(rows[0]["source"], "Example")

    def test_tavily_normalization(self):
        rows = websearch._normalize_tavily([
            {"title": "Momentum", "url": "https://example.org/m", "content": "snippet",
             "published_date": "2019-02-03", "score": 0.8},
        ])
        self.assertEqual(rows[0]["published"], "2019-02-03")
        self.assertEqual(rows[0]["source"], "example.org")
        self.assertEqual(rows[0]["score"], 0.8)

    def test_search_filters_and_reports_excluded(self):
        env = {"FACTOPIA_SEARCH_PROVIDER": "bocha", "FACTOPIA_SEARCH_API_KEY": "test-key",
               "FACTOPIA_SEARCH_BEFORE": "2021-12-31", "FACTOPIA_SEARCH_ALLOW_UNKNOWN": "false"}
        provider_rows = [
            {"title": "A", "url": "https://example.com/a", "snippet": "", "published": "2020-01-01", "source": "x"},
            {"title": "B", "url": "https://example.com/b", "snippet": "", "published": "2022-01-01", "source": "x"},
            {"title": "C", "url": "https://example.com/c", "snippet": "", "published": None, "source": "x"},
        ]
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(websearch, "_bocha", return_value=provider_rows):
            r = websearch.search("unique-test-query")
        self.assertTrue(r["configured"])
        self.assertEqual([x["title"] for x in r["results"]], ["A"])
        self.assertEqual(r["raw_count"], 3)
        self.assertEqual(r["excluded"], 2)


if __name__ == "__main__":
    unittest.main()
