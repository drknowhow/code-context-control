"""Tests for ChatEngine's native tool-calling loop.

A scripted FakeBridge stands in for Ollama so every path is exercised
without a network: native structured tool calls, the tool-less degradation
paths (probe-negative models and the unknown-capability mid-turn rerun on
HTTP 400), the thinking-only visible-retry, round caps, and the delegate
sub-agent loop. The legacy <tool_call> text protocol was retired (issue #34).
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
from oracle.services.chat_engine import ChatEngine  # noqa: E402

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
    def test_400_with_tools_reruns_toolless_and_negative_caches(self):
        bridge = _FakeBridge([
            _http400(),
            [("text", "toolless answer")],
        ], supports=None)  # unknown → attempt native
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertTrue(any(
            e["type"] == "status" and "Continuing without tools" in e["message"]
            for e in events
        ))
        self.assertEqual(_text(events), "toolless answer")
        self.assertEqual(bridge.support_overrides, [("fake-model", False)])
        # Rerun request: no tools, and the prompt stops promising them.
        retry = bridge.calls[1]
        self.assertIsNone(retry["tools"])
        self.assertIn("No tools are available", retry["messages"][0]["content"])

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

    def test_400_naming_thinking_drops_think_and_keeps_tools(self):
        bridge = _FakeBridge([
            _http400(body=b'{"error":"\\"m\\" does not support thinking"}'),
            [("text", "thinkless answer")],
        ], supports=True)
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertEqual(_text(events), "thinkless answer")
        self.assertTrue(any(
            e["type"] == "status" and "without thinking" in e["message"]
            for e in events
        ))
        # Tools survive a thinking rejection; capability is not demoted.
        retry = bridge.calls[1]
        self.assertTrue(retry["tools"])
        self.assertIs(retry["think"], False)
        self.assertEqual(bridge.support_overrides, [])

    def test_400_thinking_then_tools_downgrades_both(self):
        bridge = _FakeBridge([
            _http400(body=b'{"error":"\\"m\\" does not support thinking"}'),
            _http400(),  # names tools
            [("text", "bare answer")],
        ], supports=None)
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertEqual(_text(events), "bare answer")
        self.assertEqual(bridge.support_overrides, [("fake-model", False)])
        final = bridge.calls[2]
        self.assertIsNone(final["tools"])
        self.assertIs(final["think"], False)
        self.assertIn("No tools are available", final["messages"][0]["content"])


# ── Tool-less mode (probe-negative models) ────────────────────────────


class TestNoToolsMode(_EngineTestBase):
    def test_probe_false_runs_without_tools(self):
        bridge = _FakeBridge([[("text", "plain answer")]], supports=False)
        events = list(_engine(bridge).chat(None, "hi"))
        self.assertEqual(_text(events), "plain answer")
        self.assertEqual(_types(events)[-1], "done")
        call = bridge.calls[0]
        self.assertIsNone(call["tools"])
        # The prompt must not promise tool capabilities the model lacks.
        self.assertIn("No tools are available", call["messages"][0]["content"])


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

    def test_toolless_agent_answers_directly(self):
        bridge = _FakeBridge([
            [("text", "direct sub answer")],
        ], supports=False)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("architect", "task")
        self.assertEqual(result["result"], "direct sub answer")
        call = bridge.calls[0]
        self.assertIsNone(call["tools"])
        self.assertIn("No tools are available", call["messages"][0]["content"])

    def test_sub_agents_cannot_delegate(self):
        bridge = _FakeBridge([
            [("tool_call", {"name": "delegate_task",
                            "arguments": {"agent_id": "architect", "task": "x"}})],
            [("text", "done anyway")],
        ], supports=True)
        engine = _engine(bridge)
        engine._tool_delegate_task("architect", "task")
        results = [e for e in self._drain_sink() if e["type"] == "agent_tool_result"]
        self.assertIn("cannot delegate", str(results[0]["result"]))

    def test_agent_mid_turn_fallback(self):
        bridge = _FakeBridge([
            _http400(),
            [("text", "recovered")],
        ], supports=None)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("architect", "task")
        self.assertEqual(result["result"], "recovered")
        self.assertEqual(bridge.support_overrides, [("agent-model", False)])
        self.assertIsNone(bridge.calls[1]["tools"])
        self.assertIn("No tools are available",
                      bridge.calls[1]["messages"][0]["content"])

    def test_inactive_agent_errors(self):
        bridge = _FakeBridge([], supports=True)
        engine = _engine(bridge)
        result = engine._tool_delegate_task("ghost", "task")
        self.assertIn("error", result)


class TestDelegateCliBackends(unittest.TestCase):
    """Wave 2: agents with codex/gemini/claude/auto backends route through
    cli.tools.delegate against a read-only shim of the target project."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = str((Path(self.tmp.name) / "proj").resolve())
        (Path(self.proj) / ".c3").mkdir(parents=True)

        self.agent_cfg = {
            "id": "codex_agent", "name": "Codex Agent", "active": True,
            "system_prompt": "You are precise.", "model": "m",
            "backend": "codex", "task_type": "ask",
        }
        patcher = mock.patch.object(
            ce, "load_config", return_value={"agents": [dict(self.agent_cfg)]}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.sink: queue.Queue = queue.Queue()
        ce._agent_tls.agent_sink = self.sink
        ce._agent_tls.parent_tool_id = "p1"
        self.addCleanup(lambda: setattr(ce._agent_tls, "agent_sink", None))
        self.addCleanup(lambda: setattr(ce._agent_tls, "parent_tool_id", None))
        self.addCleanup(lambda: setattr(ce._agent_tls, "conv_state", None))

        class _Scanner:
            def discover(inner, force=False):
                return [{"path": self.proj, "has_c3": True}]

        self.runtime = mock.Mock()
        self.runtime.delegate_config = {"enabled": True, "codex_memory_bridge": True,
                                        "codex_default_sandbox": "workspace-write"}
        bridge = mock.Mock()
        bridge.get_runtime = mock.Mock(return_value=self.runtime)

        self.engine = ChatEngine(
            bridge=_FakeBridge([], supports=False),
            reader=mock.Mock(), writer=mock.Mock(), cross_memory=mock.Mock(),
            health_checker=mock.Mock(), insight_engine=mock.Mock(),
            scanner=_Scanner(), store=_FakeStore(), c3_bridge=bridge,
        )

        self.delegate_calls: list[dict] = []

        def fake_handle_delegate(task, task_type, context, file_path, svc,
                                 finalize, backend="ollama"):
            self.delegate_calls.append({
                "task": task, "task_type": task_type, "context": context,
                "svc": svc, "backend": backend,
            })
            return f"[delegate:{backend}] done"

        import cli.tools.delegate as delegate_mod
        patcher = mock.patch.object(delegate_mod, "handle_delegate", fake_handle_delegate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_codex_backend_routes_through_shim(self):
        result = self.engine._tool_delegate_task("codex_agent", "do it",
                                                 project_path=self.proj)
        self.assertEqual(result["backend"], "codex")
        self.assertEqual(result["project"], self.proj)
        self.assertIn("done", result["result"])
        call = self.delegate_calls[0]
        self.assertEqual(call["backend"], "codex")
        self.assertEqual(call["context"], "You are precise.")
        # The shim is the write-suppression contract:
        shim = call["svc"]
        dcfg = shim.delegate_config
        self.assertIs(dcfg["codex_memory_bridge"], False)
        self.assertIs(dcfg["gemini_memory_bridge"], False)
        self.assertEqual(dcfg["codex_default_sandbox"], "read-only")
        self.assertIs(shim.notifications, None)
        self.assertIs(dcfg["enabled"], True)  # target's own keys preserved

    def test_missing_project_gives_instructive_error(self):
        result = self.engine._tool_delegate_task("codex_agent", "do it")
        self.assertIn("project_path", result["error"])
        self.assertEqual(self.delegate_calls, [])

    def test_focused_project_fallback(self):
        ce._agent_tls.conv_state = {"focused_projects": [{"path": self.proj}]}
        result = self.engine._tool_delegate_task("codex_agent", "do it")
        self.assertEqual(result["project"], self.proj)
        self.assertEqual(len(self.delegate_calls), 1)

    def test_unregistered_project_rejected(self):
        rogue = str((Path(self.tmp.name) / "rogue").resolve())
        (Path(rogue) / ".c3").mkdir(parents=True)
        result = self.engine._tool_delegate_task("codex_agent", "do it",
                                                 project_path=rogue)
        self.assertIn("error", result)
        self.assertEqual(self.delegate_calls, [])

    def test_unknown_backend_errors(self):
        self.agent_cfg["backend"] = "warp-drive"
        with mock.patch.object(ce, "load_config",
                               return_value={"agents": [dict(self.agent_cfg)]}):
            result = self.engine._tool_delegate_task("codex_agent", "x")
        self.assertIn("unknown backend", result["error"])

    def test_lifecycle_events_emitted(self):
        self.engine._tool_delegate_task("codex_agent", "do it", project_path=self.proj)
        kinds = []
        while True:
            try:
                kinds.append(self.sink.get_nowait()["type"])
            except queue.Empty:
                break
        self.assertIn("agent_start", kinds)
        self.assertIn("agent_done", kinds)


class TestOracleDelegateRuntimeShim(unittest.TestCase):
    def test_passthrough_and_overrides(self):
        from oracle.services.c3_bridge import _OracleDelegateRuntime

        class _Runtime:
            project_path = "C:/proj"
            delegate_config = {"enabled": True, "codex_timeout": 99}
            notifications = "REAL-STORE"
            ollama_client = "CLIENT"

        cb_msgs = []
        shim = _OracleDelegateRuntime(_Runtime(), progress_cb=cb_msgs.append)
        self.assertEqual(shim.project_path, "C:/proj")       # passthrough
        self.assertEqual(shim.ollama_client, "CLIENT")       # passthrough
        self.assertIsNone(shim.notifications)                # suppressed
        dcfg = shim.delegate_config
        self.assertIs(dcfg["codex_memory_bridge"], False)
        self.assertIs(dcfg["gemini_memory_bridge"], False)
        self.assertEqual(dcfg["codex_default_sandbox"], "read-only")
        self.assertEqual(dcfg["codex_timeout"], 99)          # preserved
        shim._agent_progress_cb("hello")
        self.assertEqual(cb_msgs, ["hello"])


if __name__ == "__main__":
    unittest.main()
