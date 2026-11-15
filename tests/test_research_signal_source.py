"""研究规格的信号来源分类：text 只是附加条件时不能把量价研究判成文本研究。"""
import unittest

from factopia.research import draft


def raw(categories: list[str]) -> dict:
    return {
        "id": "test_signal", "title_zh": "测试", "hypothesis": "h", "hypothesis_zh": "测试",
        "rationale": {"category": "other", "note": "n"}, "directions": ["d"], "exclusions": [],
        "falsification": "f",
        "data_needs": [{"category": c, "detail": "d"} for c in categories],
        "open_questions": [], "leak_flags": [], "mandate_interpretations": [],
    }


class SignalSourceTests(unittest.TestCase):
    def test_price_first_with_optional_text_stays_price_volume(self):
        out = draft._validate(raw(["price_volume", "fx", "benchmark", "text", "fundamentals"]),
                              {"markets": ["hk"], "_free_text": []})
        self.assertEqual(out["signal_source"], "price_volume")

    def test_fx_first_with_optional_text_stays_price_volume(self):
        out = draft._validate(raw(["fx", "text"]), {"markets": ["hk"], "_free_text": []})
        self.assertEqual(out["signal_source"], "price_volume")

    def test_text_first_is_text_research(self):
        out = draft._validate(raw(["text", "price_volume"]), {"markets": ["fr"], "_free_text": []})
        self.assertEqual(out["signal_source"], "text")


if __name__ == "__main__":
    unittest.main()
