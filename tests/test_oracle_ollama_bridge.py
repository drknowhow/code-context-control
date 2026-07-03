"""Tests for OllamaBridge: capability probe, native-tools streaming, the
is_available 5xx fix, and the LLM disk cache TTL/size bounds.

urllib.request.urlopen is monkeypatched at the module level — no network.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import ollama_bridge as ob  # noqa: E402
from services.ollama_bridge import OllamaBridge, _Cache  # noqa: E402

_URLOPEN = "urllib.request.urlopen"


class _FakeResp:
    """Context-manager response: read() for JSON calls, iteration for streams."""

    def __init__(self, payload=None, lines=None):
        self._payload = payload
        self._lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload or {}).encode()

    def __iter__(self):
        return iter(json.dumps(line).encode() + b"\n" for line in self._lines)


def _http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError("http://x", code, "err", None, io.BytesIO(body))


class TestIsAvailable(unittest.TestCase):
    def setUp(self):
        self.bridge = OllamaBridge(base_url="http://fake", model="m")

    def test_head_500_means_unavailable(self):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:  # /api/tags
                raise urllib.error.URLError("nope")
            raise _http_error(500)

        with mock.patch(_URLOPEN, fake):
            self.assertFalse(self.bridge.is_available(timeout=1))

    def test_head_405_means_reachable(self):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("nope")
            raise _http_error(405)

        with mock.patch(_URLOPEN, fake):
            self.assertTrue(self.bridge.is_available(timeout=1))


class TestSupportsTools(unittest.TestCase):
    def setUp(self):
        self.bridge = OllamaBridge(base_url="http://fake", model="m")

    def test_capabilities_with_tools(self):
        with mock.patch(_URLOPEN, return_value=_FakeResp({"capabilities": ["completion", "tools"]})):
            self.assertIs(self.bridge.supports_tools("m"), True)

    def test_capabilities_without_tools(self):
        with mock.patch(_URLOPEN, return_value=_FakeResp({"capabilities": ["completion"]})):
            self.assertIs(self.bridge.supports_tools("m"), False)

    def test_probe_failure_is_unknown(self):
        with mock.patch(_URLOPEN, side_effect=urllib.error.URLError("down")):
            self.assertIsNone(self.bridge.supports_tools("m"))

    def test_missing_capabilities_key_is_unknown(self):
        with mock.patch(_URLOPEN, return_value=_FakeResp({"modelfile": "..."})):
            self.assertIsNone(self.bridge.supports_tools("m"))

    def test_result_cached_per_model(self):
        with mock.patch(_URLOPEN, return_value=_FakeResp({"capabilities": ["tools"]})) as m:
            self.bridge.supports_tools("m")
            self.bridge.supports_tools("m")
            self.assertEqual(m.call_count, 1)

    def test_set_tools_support_overrides_probe(self):
        with mock.patch(_URLOPEN, return_value=_FakeResp({"capabilities": ["tools"]})) as m:
            self.bridge.set_tools_support("m", False)
            self.assertIs(self.bridge.supports_tools("m"), False)
            self.assertEqual(m.call_count, 0)  # negative cache short-circuits


class TestStreamChat(unittest.TestCase):
    def setUp(self):
        self.bridge = OllamaBridge(base_url="http://fake", model="m")

    def _stream(self, lines, tools=None):
        captured = {}

        def fake(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(lines=lines)

        with mock.patch(_URLOPEN, fake):
            events = list(self.bridge.stream_chat(
                [{"role": "user", "content": "hi"}], tools=tools,
            ))
        return events, captured["body"]

    def test_parses_thinking_text_toolcalls_and_stats(self):
        lines = [
            {"message": {"thinking": "hmm"}},
            {"message": {"content": "Hello"}},
            {"message": {"tool_calls": [
                {"function": {"name": "list_projects", "arguments": {"a": 1}}},
            ]}},
            {"message": {}, "done": True, "eval_count": 7, "eval_duration": 123},
        ]
        events, body = self._stream(lines, tools=[{"type": "function"}])
        self.assertIn(("thinking", "hmm"), events)
        self.assertIn(("text", "Hello"), events)
        self.assertIn(("tool_call", {"name": "list_projects", "arguments": {"a": 1}}), events)
        stats = dict(events)[("stats")]
        self.assertEqual(stats["eval_count"], 7)
        self.assertEqual(body["tools"], [{"type": "function"}])

    def test_string_arguments_are_parsed(self):
        lines = [
            {"message": {"tool_calls": [
                {"function": {"name": "t", "arguments": '{"x": 2}'}},
            ]}, "done": True},
        ]
        events, _ = self._stream(lines, tools=[{"type": "function"}])
        self.assertIn(("tool_call", {"name": "t", "arguments": {"x": 2}}), events)

    def test_unparseable_string_arguments_wrapped_raw(self):
        lines = [
            {"message": {"tool_calls": [
                {"function": {"name": "t", "arguments": "not json"}},
            ]}, "done": True},
        ]
        events, _ = self._stream(lines, tools=[{"type": "function"}])
        self.assertIn(("tool_call", {"name": "t", "arguments": {"_raw": "not json"}}), events)

    def test_no_tools_param_omits_tools_from_body(self):
        _, body = self._stream([{"message": {"content": "x"}, "done": True}])
        self.assertNotIn("tools", body)


class TestDiskCache(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_ttl_expiry(self):
        cache = _Cache(cache_dir=self.dir, ttl_sec=60)
        cache.set("p", "m", "cached-answer")
        self.assertEqual(cache.get("p", "m"), "cached-answer")
        # Age the entry past the TTL via mtime.
        entry = next(self.dir.glob("*.json"))
        old = time.time() - 120
        os.utime(entry, (old, old))
        self.assertIsNone(cache.get("p", "m"))
        self.assertFalse(list(self.dir.glob("*.json")))  # expired entry removed

    def test_size_bound_evicts_oldest(self):
        cache = _Cache(cache_dir=self.dir, ttl_sec=3600, max_entries=3)
        base = time.time() - 100  # recent (within TTL), strictly increasing
        for i in range(5):
            cache.set(f"prompt-{i}", "m", f"resp-{i}")
            # Distinct, increasing mtimes so eviction order is deterministic.
            entry = cache._dir / f"{cache._key(f'prompt-{i}', 'm')}.json"
            os.utime(entry, (base + i, base + i))
        remaining = list(self.dir.glob("*.json"))
        self.assertLessEqual(len(remaining), 3)
        self.assertIsNone(cache.get("prompt-0", "m"))
        self.assertEqual(cache.get("prompt-4", "m"), "resp-4")


if __name__ == "__main__":
    unittest.main()
