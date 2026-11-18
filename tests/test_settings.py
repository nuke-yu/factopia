"""设置菜单（模型选择）的单元测试：默认值、保存、校验、AI 研发前缀与费用。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from factopia import settings
from factopia.research import draft

MODELS = {
    "roles": [
        {"key": "ai_rd", "env": "CHAT_MODEL", "prefix": "openai/"},
        {"key": "pro", "env": "FACTOPIA_LLM_MODEL"},
        {"key": "lite", "env": "FACTOPIA_LLM_LITE_MODEL"},
        {"key": "critic", "env": "FACTOPIA_LLM_CRITIC_MODEL"},
    ],
    "options": ["qwen3.8-max-0902", "qwen3.8-flash", "deepseek-v4.1-flash", "deepseek-v4-pro", "qwen-plus"],
    "prices": {"qwen3.8-max-0902": {"in": 0.27, "out": 1.10}},
}
ENV = {
    "CHAT_MODEL": "openai/deepseek-v4.1-flash",
    "CHAT_MODEL_MAP": '{"feedback":{"model":"openai/deepseek-v4.1-flash"}}',
    "FACTOPIA_LLM_MODEL": "qwen3.8-max-0902",
    "FACTOPIA_LLM_LITE_MODEL": "deepseek-v4.1-flash",
    "FACTOPIA_LLM_CRITIC_MODEL": "qwen-plus",
    "FACTOPIA_LLM_PRICE_IN": "0.27",
    "FACTOPIA_LLM_PRICE_OUT": "1.10",
    "FACTOPIA_LLM_BASE_URL": "http://example.test",
    "FACTOPIA_LLM_API_KEY": "key",
}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.configs = self.root / "configs"
        self.configs.mkdir()
        (self.configs / "models.yaml").write_text(yaml.safe_dump(MODELS, allow_unicode=True), encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.patches = [
            mock.patch.object(settings, "CONFIGS", self.configs),
            mock.patch.object(settings, "SETTINGS_DIR", self.runtime / "settings"),
            mock.patch.object(settings, "SETTINGS_FILE", self.runtime / "settings" / "models.json"),
            mock.patch.dict(os.environ, ENV, clear=False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_defaults_strip_provider_prefix(self):
        cur = settings.current_models()
        self.assertEqual(cur["ai_rd"], "deepseek-v4.1-flash")
        self.assertEqual(cur["pro"], "qwen3.8-max-0902")
        self.assertEqual(cur["critic"], "qwen-plus")
        self.assertEqual(settings.model_overrides(), {})

    def test_save_and_override_llm_config(self):
        state = settings.save_models({"pro": "qwen3.8-flash"})
        pro = next(r for r in state["roles"] if r["key"] == "pro")
        self.assertEqual(pro["model"], "qwen3.8-flash")
        self.assertEqual(settings.model_overrides(), {"pro": "qwen3.8-flash"})
        cfg = draft.llm_config()
        self.assertEqual(cfg["model"], "qwen3.8-flash")

    def test_rejects_unknown_model_and_role(self):
        with self.assertRaises(settings.SettingsError):
            settings.save_models({"pro": "not-a-model"})
        with self.assertRaises(settings.SettingsError):
            settings.save_models({"unknown": "qwen3.8-max-0902"})
        with self.assertRaises(settings.SettingsError):
            settings.save_models({})

    def test_clear_selection_falls_back_to_env(self):
        settings.save_models({"pro": "qwen3.8-flash", "lite": "deepseek-v4-pro"})
        self.assertEqual(settings.model_overrides(), {"pro": "qwen3.8-flash", "lite": "deepseek-v4-pro"})
        settings.save_models({"pro": ""})                     # 选回「默认」→ 删除该角色的选择
        self.assertEqual(settings.model_overrides(), {"lite": "deepseek-v4-pro"})
        self.assertEqual(settings.current_models()["pro"], "qwen3.8-max-0902")
        self.assertEqual(draft.llm_config()["model"], "qwen3.8-max-0902")

    def test_state_reports_env_default(self):
        role = next(r for r in settings.state()["roles"] if r["key"] == "ai_rd")
        self.assertEqual(role["default"], "deepseek-v4.1-flash")

    def test_ai_rd_subprocess_env_uses_openai_prefix(self):
        settings.save_models({"ai_rd": "deepseek-v4-pro"})
        env = settings.subprocess_env()
        self.assertEqual(env["CHAT_MODEL"], "openai/deepseek-v4-pro")
        chat_map = json.loads(env["CHAT_MODEL_MAP"])
        self.assertEqual(chat_map["feedback"]["model"], "openai/deepseek-v4-pro")

    def test_subprocess_env_empty_without_override(self):
        self.assertEqual(settings.subprocess_env(), {})

    def test_prices(self):
        state = settings.state()
        by_key = {r["key"]: r for r in state["roles"]}
        self.assertEqual(by_key["pro"]["price"], {"in": 0.27, "out": 1.10})   # 配置显式价格
        self.assertEqual(by_key["critic"]["price"], {"in": 0.27, "out": 1.10})  # 默认模型回退 env 单价
        settings.save_models({"pro": "qwen3.8-flash"})
        state = settings.state()
        pro = next(r for r in state["roles"] if r["key"] == "pro")
        self.assertIsNone(pro["price"])                                       # 未配置价格显示「—」

    def test_state_updated_at(self):
        self.assertIsNone(settings.state()["updated_at"])
        settings.save_models({"lite": "deepseek-v4-pro"})
        self.assertTrue(settings.state()["updated_at"])


if __name__ == "__main__":
    unittest.main()
