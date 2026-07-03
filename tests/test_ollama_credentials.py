import os
import sys
import types
import unittest
from unittest.mock import patch


class FakeKeyring(types.ModuleType):
    def __init__(self):
        super().__init__("keyring")
        self.store = {}

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        del self.store[(service, account)]


class TestOllamaCredentials(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKeyring()
        self._patch = patch.dict(sys.modules, {"keyring": self.fake})
        self._patch.start()
        # Re-import cleanly so the lazy _keyring_module picks up the fake.
        sys.modules.pop("services.ollama_credentials", None)
        import services.ollama_credentials as oc
        self.oc = oc

    def tearDown(self):
        self._patch.stop()
        sys.modules.pop("services.ollama_credentials", None)

    def test_save_load_roundtrip_default_account(self):
        self.oc.save_api_key("sk-test-1")
        self.assertEqual(self.oc.load_api_key(), "sk-test-1")
        self.assertEqual(
            self.fake.store[("c3-ollama", "https://ollama.com")], "sk-test-1")

    def test_account_keyed_by_base_url(self):
        self.oc.save_api_key("key-a", "https://ollama.com/")
        self.oc.save_api_key("key-b", "http://localhost:11434")
        self.assertEqual(self.oc.load_api_key("https://ollama.com"), "key-a")
        self.assertEqual(self.oc.load_api_key("http://localhost:11434"), "key-b")

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            self.oc.save_api_key("   ")

    def test_delete_returns_whether_key_existed(self):
        self.assertFalse(self.oc.delete_api_key())
        self.oc.save_api_key("sk-test-2")
        self.assertTrue(self.oc.delete_api_key())
        self.assertIsNone(self.oc.load_api_key())

    def test_api_key_available_resolution_chain(self):
        env_var = "C3_TEST_OLLAMA_KEY"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_var, None)
            # nothing anywhere
            self.assertFalse(self.oc.api_key_available(api_key_env=env_var))
            # config key wins without touching keyring/env
            self.assertTrue(self.oc.api_key_available(
                api_key_env=env_var, config_key="explicit"))
            # env var
            os.environ[env_var] = "from-env"
            self.assertTrue(self.oc.api_key_available(api_key_env=env_var))
            os.environ.pop(env_var, None)
            # keyring fallback
            self.oc.save_api_key("from-ring")
            self.assertTrue(self.oc.api_key_available(api_key_env=env_var))


class TestMemoryLlmConfigLoader(unittest.TestCase):
    def test_overrides_merge_over_defaults(self):
        import json
        import tempfile
        from pathlib import Path

        from core.config import MEMORY_LLM_DEFAULTS, load_memory_llm_config
        with tempfile.TemporaryDirectory() as tmp:
            # no config file → pure defaults
            cfg = load_memory_llm_config(tmp)
            self.assertEqual(cfg, MEMORY_LLM_DEFAULTS)
            self.assertFalse(cfg["cloud_enabled"])  # privacy default
            # partial override merges, other keys keep defaults
            c3 = Path(tmp) / ".c3"
            c3.mkdir()
            (c3 / "config.json").write_text(json.dumps({
                "memory_llm": {"cloud_enabled": True, "local_model": "llama3.2:3b"}}),
                encoding="utf-8")
            cfg = load_memory_llm_config(tmp)
            self.assertTrue(cfg["cloud_enabled"])
            self.assertEqual(cfg["local_model"], "llama3.2:3b")
            self.assertEqual(cfg["cloud_model"], MEMORY_LLM_DEFAULTS["cloud_model"])


if __name__ == "__main__":
    unittest.main()
