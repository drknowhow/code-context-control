"""Tests for ChatEngine's tool-calling loop (native + legacy protocols).

First coverage of the Oracle chat orchestrator. A scripted FakeBridge stands
in for Ollama so every path is exercised without a network: native structured
tool calls, the legacy <tool_call> text protocol (stripper, trust-answer,
malformed JSON), the unknown-capability mid-turn fallback on HTTP 400, the
thinking-only visible-retry, round caps, and the delegate sub-agent loop.
"""
from __future__ import annotations

import io
import queue
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from oracle.services import chat_engine as ce  # noqa: E402
from oracle.services.chat_engine import ChatEngine, _ToolCallStripper  # noqa: E402

# ── Fakes ─────────────────────────────────────────────────────────────


class _FakeBridge:
    """Scripted stream_chat: each call consumes the next script entry.

    A script entry is either a list of (kind, payload) tuples to yield, or an
    Exception instance to raise at call time.
    """

    def __init__(self, scripts, supports=True, model="fake-model"):
        self.scripts = list(scripts)
        self.supports = supports
        self.model = model
        self.calls: list[dict] = []
        self.support_overrides: list[tuple] = []

    def supports_tools(self, model=None):
        return self.supports

    def set_tools_support(self, model, value):
        self.support_overrides.append((model, value))
        self.supports = value

    def stream_chat(self, messages, model=None, think=True, tools=None, **kw):
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "model": model, "think": think, "tools": tools,
        })
        if not self.scripts:
            return iter(())
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return iter(script)


class _FakeStore:
    def __init__(self, state=None):
        self.state = {"depth": "normal", "focused_projects": [], **(state or {})}
        self.history: list[dict] = []
        self.persisted: list[dict] = []

    def create_conversation(self):
        return "conv-test"

    def get_state(self, conv_id):
        return dict(self.state)

    def append_message(self, conv_id, msg):
        self.history.append(msg)

    def append_messages(self, conv_id, msgs):
        self.persisted.extend(msgs)
        self.history.extend(msgs)

    def get_conversation(self, conv_id):
        return list(self.history)


class _CountingScanner:
    def __init__(self):
        self.discover_calls = 0

    def discover(self, force=False):
        self.discover_calls += 1
        return [{"path": "C:/proj", "name": "proj", "has_c3": True, "facts_count": 2}]


def _engine(bridge, store=None, scanner=None):
    return ChatEngine(
        bridge=bridge,
        reader=mock.Mock(),
        writer=mock.Mock(),
        cross_memory=mock.Mock(),
        health_checker=mock.Mock(),
        insight_engine=mock.Mock(),
        scanner=scanner or _CountingScanner(),
        store=store or _FakeStore(),
    )


def _types(events):
    return [e["type"] for e in events]


def _text(events):
    return "".join(e["content"] for e in events if e["type"] == "text")


def _http400(body: bytes = b'{"error":"model does not support tools"}'):
    return urllib.error.HTTPError("http://x/api/chat", 400, "Bad Request",
                                  None, io.BytesIO(body))


_NO_AGENTS = {"agents": []}
TOOLCALL = '<tool_call>{"name": "list_projects", "args": {}}</tool_call>'


class _EngineTestBase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ce, "load_config", return_value=dict(_NO_AGENTS))
        patcher.start()
        self.addCleanup(patcher.stop)


# ── Native protocol ───────────────────────────────────────────────────


class TestNativeMode(_EngineTestBase):
    def test_plain_answer_event_sequence(self):
        bridge = _FakeBridge([[
            ("thinking", "pondering"),
            ("text", "Hello "),
            ("text", "world"),
            ("stats", {"eval_count": 5, "eval_duration": 1_000_000_000}),
        ]], supports=True)
        events = list(_engine(bridge).chat(None, "hi"))
        kinds = _types(events)
        self.assertEqual(kinds[0], "meta")
        self.assertIn("thinking", kinds)
        self.assertEqual(_text(events), "Hello world")
        self.assertEqual(kinds[-1], "done")
        # Native mode: tools declared via API, no <tool_call> catalog in prompt.
        call = bridge.calls[0]
        self.assertTrue(call["tools"])
        self.assertNotIn("<tool_call>", call["messages"][0]["content"])
        self.assertEqual(events[-1]["stats"]["eval_tokens"], 5)

    def test_tool_round_executes_and_feeds_role_tool(self):
        bridge = _FakeBridge([
            [("tool_call", {"name": "list_projects", "arguments": {}})],
            [("text", "Final answer")],
        ], supports=True)
        scanner = _CountingScanner()
        store = _FakeStore()
        events = list(_engine(bridge, store=store, scanner=scanner).chat(None, "list"))
        kinds = _types(events)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertEqual(_text(events), "Final answer")
        self.assertEqual(scanner.discover_calls, 1)
        # Second round context: assistant echo with tool_calls + role:tool msg.
        msgs = bridge.calls[1]["messages"]
        assistant = next(m for m in msgs if m.get("tool_calls"))
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "list_projects")
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        self.assertEqual(tool_msg["tool_name"], "list_projects")
        # Native mode must NOT inject the legacy finalize nudge.
        self.assertFalse(any(
            m["role"] == "user" and "Do NOT output any <tool_call>" in m["content"]
            for m in msgs
        ))
        # Persisted round messages keep the legacy role vocabulary.
        roles = [m["role"] for m in store.persisted]
        self.assertIn("tool_call", roles)
        self.assertIn("tool_result", roles)

    def test_round_cap_yields_fallback(self):
        # depth=brief → 2 rounds, both spent on tool calls → for-else fallback.
        bridge = _FakeBridge([
            [("tool_call", {"name": "list_projects", "arguments": {}})],
            [("tool_call", {"name": "list_projects", "arguments": {}})],
        ], supports=True)
        store = _FakeStore(state={"depth": "brief"})
        events = list(_engine(bridge, store=store).chat(None, "loop"))
        self.assertIn("2-round limit", _text(events))
        self.assertEqual(_types(events)[-1], "done")

    def test_thinking_only_triggers_retry_with_think_false(self):
        bridge = _FakeBridge([
            [("thinking", "only thoughts")],
            [("text", "recovered")],
        ], supports=True)
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertTrue(any(
            e["type"] == "status" and e["message"] == "Retrying visible response"
            for e in events
        ))
        self.assertEqual(_text(events), "recovered")
        self.assertIs(bridge.calls[1]["think"], False)


# ── Unknown capability → mid-turn fallback ────────────────────────────


class TestMidTurnFallback(_EngineTestBase):
    def test_400_with_tools_falls_back_and_negative_caches(self):
        bridge = _FakeBridge([
            _http400(),
            [("text", "legacy answer")],
        ], supports=None)  # unknown → attempt native
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertTrue(any(
            e["type"] == "status" and "Falling back" in e["message"] for e in events
        ))
        self.assertEqual(_text(events), "legacy answer")
        self.assertEqual(bridge.support_overrides, [("fake-model", False)])
        # Rerun request: no tools, legacy catalog restored in system prompt.
        retry = bridge.calls[1]
        self.assertIsNone(retry["tools"])
        self.assertIn("<tool_call>", retry["messages"][0]["content"])

    def test_unrelated_error_does_not_fall_back(self):
        bridge = _FakeBridge([RuntimeError("boom")], supports=True)
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertTrue(any(e["type"] == "error" for e in events))
        self.assertEqual(len(bridge.calls), 1)  # no legacy rerun
        self.assertEqual(bridge.support_overrides, [])

    def test_400_without_tool_mention_falls_back_without_caching(self):
        bridge = _FakeBridge([
            _http400(body=b'{"error":"invalid request"}'),
            [("text", "ok")],
        ], supports=None)
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertEqual(_text(events), "ok")
        self.assertEqual(bridge.support_overrides, [])  # capability not demoted


# ── Legacy protocol (preserved verbatim) ──────────────────────────────


class TestLegacyMode(_EngineTestBase):
    def test_tool_round_via_text_protocol(self):
        bridge = _FakeBridge([
            [("text", TOOLCALL)],
            [("text", "legacy synthesis")],
        ], supports=False)
        scanner = _CountingScanner()
        events = list(_engine(bridge, scanner=scanner).chat(None, "list"))
        kinds = _types(events)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertEqual(scanner.discover_calls, 1)
        # The <tool_call> block never leaks into text events.
        self.assertNotIn("tool_call", _text(events))
        # Legacy round 2: <tool_result> user message + finalize nudge present.
        msgs = bridge.calls[1]["messages"]
        self.assertTrue(any(
            m["role"] == "user" and m["content"].startswith("<tool_result")
            for m in msgs
        ))
        self.assertTrue(any(
            m["role"] == "user" and "Do NOT output any <tool_call>" in m["content"]
            for m in msgs
        ))
        # System prompt carries the legacy catalog.
        self.assertIn("<tool_call>", bridge.calls[0]["messages"][0]["content"])
        self.assertIsNone(bridge.calls[0]["tools"])

    def test_trust_answer_skips_speculative_tool_calls(self):
        long_answer = "A" * 130
        bridge = _FakeBridge([[("text", long_answer + TOOLCALL)]], supports=False)
        scanner = _CountingScanner()
        events = list(_engine(bridge, scanner=scanner).chat(None, "q"))
        self.assertTrue(any(
            e["type"] == "status" and e["message"] == "Answer finalized"
            for e in events
        ))
        self.assertEqual(scanner.discover_calls, 0)  # tool never executed
        self.assertEqual(len(bridge.calls), 1)

    def test_malformed_tool_json_finalizes_answer(self):
        bridge = _FakeBridge([[
            ("text", '<tool_call>{not json}</tool_call>fallback text'),
        ]], supports=False)
        scanner = _CountingScanner()
        events = list(_engine(bridge, scanner=scanner).chat(None, "q"))
        self.assertEqual(scanner.discover_calls, 0)
        self.assertEqual(_types(events)[-1], "done")
        self.assertEqual(_text(events), "fallback text")


class TestToolCallStripper(unittest.TestCase):
    def test_block_split_across_chunks_never_leaks(self):
        s = _ToolCallStripper()
        out = s.feed("before <tool_")
        out += s.feed('call>{"name": "x"}</tool_')
        out += s.feed("call> after")
        out += s.flush()
        self.assertEqual(out, "before  after")

    def test_partial_open_tag_held_then_released_as_text(self):
        s = _ToolCallStripper()
        out = s.feed("hello <tool_")
        self.assertEqual(out, "hello ")  # partial tag held back
        out += s.feed("bar")  # not a tool_call after all
        out += s.flush()
        self.assertEqual(out, "hello <tool_bar")

    def test_unclosed_block_suppressed(self):
        s = _ToolCallStripper()
        out = s.feed('x<tool_call>{"name"')
        out += s.flush()
        self.assertEqual(out, "x")


# ── Delegate sub-agent loop ───────────────────────────────────────────


class TestDelegateTask(unittest.TestCase):
    AGENT = {
        "id": "architect", "name": "Architect", "active": True,
        "system_prompt": "You are the Architect.", "model": "agent-model",
    }

    def setUp(self):
        patcher = mock.patch.object(
            ce, "load_config", return_value={"agents": [dict(self.AGENT)]}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sink: queue.Queue = queue.Queue()
        ce._agent_tls.agent_sink = self.sink
        ce._agent_tls.parent_tool_id = "parent-1"
        self.addCleanup(lambda: setattr(ce._agent_tls, "agent_sink", None))
        self.addCleanup(lambda: setattr(ce._agent_tls, "parent_tool_id", None))

    def _drain_sink(self):
        events = []
        while True:
            try:
                events.append(self.sink.get_nowait())
            except queue.Empty:
                return events

    def test_native_agent_tool_round_and_lifecycle(self):
        bridge = _FakeBridge([
            [("tool_call", {"name": "list_projects", "arguments": {}})],
            [("text", "sub answer")],
        ], supports=True)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("architect", "do the thing")
        self.assertEqual(result, {"agent": "architect", "result": "sub answer"})
        kinds = [e["type"] for e in self._drain_sink()]
        for expected in ("agent_start", "agent_round", "agent_tool_call",
                         "agent_tool_result", "agent_done"):
            self.assertIn(expected, kinds)
        # Sub-agent ran on its own model with native tools.
        self.assertEqual(bridge.calls[0]["model"], "agent-model")
        self.assertTrue(bridge.calls[0]["tools"])
        # role:tool feeding in round 2.
        self.assertTrue(any(m["role"] == "tool" for m in bridge.calls[1]["messages"]))

    def test_legacy_agent_tool_round(self):
        bridge = _FakeBridge([
            [("text", TOOLCALL)],
            [("text", "legacy sub answer")],
        ], supports=False)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("architect", "task")
        self.assertEqual(result["result"], "legacy sub answer")
        self.assertIn("<tool_call>", bridge.calls[0]["messages"][0]["content"])

    def test_sub_agents_cannot_delegate(self):
        nested = '<tool_call>{"name": "delegate_task", "args": {"agent_id": "architect", "task": "x"}}</tool_call>'
        bridge = _FakeBridge([
            [("text", nested)],
            [("text", "done anyway")],
        ], supports=False)
        engine = _engine(bridge)
        engine._tool_delegate_task("architect", "task")
        results = [e for e in self._drain_sink() if e["type"] == "agent_tool_result"]
        self.assertIn("cannot delegate", str(results[0]["result"]))

    def test_agent_mid_turn_fallback(self):
        bridge = _FakeBridge([
            _http400(),
            [("text", "recovered legacy")],
        ], supports=None)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("architect", "task")
        self.assertEqual(result["result"], "recovered legacy")
        self.assertEqual(bridge.support_overrides, [("agent-model", False)])
        self.assertIsNone(bridge.calls[1]["tools"])

    def test_inactive_agent_errors(self):
        bridge = _FakeBridge([], supports=True)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("ghost", "task")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
