"""OllamaClient shares its /api/tags answers across instances in a process."""
import json
import unittest
from unittest import mock

from services import ollama_client
from services.ollama_client import OllamaClient, clear_probe_cache


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tags(*names):
    return _Resp({"models": [{"name": n} for n in names]})


class TestProbeCache(unittest.TestCase):
    def setUp(self):
        clear_probe_cache()
        self.addCleanup(clear_probe_cache)

    def _patch_urlopen(self, **kwargs):
        patcher = mock.patch.object(ollama_client.urllib.request, "urlopen", **kwargs)
        urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        return urlopen

    def test_list_models_is_fetched_once_for_two_clients(self):
        urlopen = self._patch_urlopen(return_value=_tags("a:latest", "b:latest"))
        first = OllamaClient("http://ollama.test:11434").list_models()
        second = OllamaClient("http://ollama.test:11434/").list_models()
        self.assertEqual(first, ["a:latest", "b:latest"])
        self.assertEqual(second, first)
        self.assertEqual(urlopen.call_count, 1)

    def test_a_model_list_answers_is_available(self):
        urlopen = self._patch_urlopen(return_value=_tags("a:latest"))
        client = OllamaClient("http://ollama.test:11434")
        client.list_models()
        self.assertTrue(client.is_available())
        self.assertTrue(client.has_model("a"))
        self.assertEqual(urlopen.call_count, 1)

    def test_an_unreachable_server_is_not_probed_again_within_the_ttl(self):
        urlopen = self._patch_urlopen(side_effect=OSError("refused"))
        self.assertFalse(OllamaClient("http://ollama.test:11434").is_available(timeout=1))
        self.assertFalse(OllamaClient("http://ollama.test:11434").is_available(timeout=1))
        self.assertIsNone(OllamaClient("http://ollama.test:11434").list_models())
        self.assertIsNone(OllamaClient("http://ollama.test:11434").list_models())
        self.assertEqual(urlopen.call_count, 2)

    def test_answers_expire_after_the_ttl(self):
        urlopen = self._patch_urlopen(return_value=_tags("a:latest"))
        client = OllamaClient("http://ollama.test:11434")
        client.list_models()
        with mock.patch.object(ollama_client, "_PROBE_TTL_SECONDS", 0.0):
            client.list_models()
        self.assertEqual(urlopen.call_count, 2)

    def test_base_urls_are_cached_separately(self):
        urlopen = self._patch_urlopen(return_value=_tags("a:latest"))
        OllamaClient("http://one.test:11434").list_models()
        OllamaClient("http://two.test:11434").list_models()
        self.assertEqual(urlopen.call_count, 2)

    def test_a_caller_mutating_its_list_does_not_change_the_cache(self):
        self._patch_urlopen(return_value=_tags("a:latest"))
        client = OllamaClient("http://ollama.test:11434")
        client.list_models().append("bogus")
        self.assertEqual(client.list_models(), ["a:latest"])


if __name__ == "__main__":
    unittest.main()
