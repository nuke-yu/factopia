"""研究项目 AI 搜索的单元测试（不依赖网络）。"""
import unittest
from unittest import mock

from factopia.research import semantic


ITEMS = [
    {"id": "volume_reversal", "title": "量价背离反转", "hypothesis": "成交量放大后的价格背离预测短期反转。"},
    {"id": "momentum", "title": "动量延续", "hypothesis": "过去一个月的强势股在下一周继续跑赢。"},
]


class ProjectSearchTests(unittest.TestCase):
    def test_embedding_ranks_by_cosine(self):
        vectors = [[1.0, 0.0], [0.0, 1.0]]                     # 两个项目；查询单独编码
        with mock.patch.object(semantic, "embed", return_value=vectors), \
             mock.patch.object(semantic, "embed_query", return_value=[0.9, 0.1]), \
             mock.patch.object(semantic, "embedding_config", return_value={"model": "test-embed"}):
            r = semantic.search_projects("动量", ITEMS)
        self.assertEqual(r["method"], "embedding")
        self.assertEqual(r["model"], "test-embed")
        self.assertIsNone(r["error"])
        self.assertEqual([x["id"] for x in r["results"]], ["volume_reversal", "momentum"])
        self.assertAlmostEqual(r["results"][0]["score"], 0.9939, places=3)

    def test_keyword_fallback(self):
        with mock.patch.object(semantic, "embed", side_effect=RuntimeError("no vector endpoint")):
            r = semantic.search_projects("动量", ITEMS)
        self.assertEqual(r["method"], "keyword")
        self.assertIn("no vector endpoint", r["error"])
        self.assertEqual(r["results"][0]["id"], "momentum")

    def test_empty_query(self):
        r = semantic.search_projects("", ITEMS)
        self.assertEqual(r["method"], "none")
        self.assertEqual(r["results"], [])


if __name__ == "__main__":
    unittest.main()
