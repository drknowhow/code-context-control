"""Tests for the poll-based mobile chat transport (oracle/services/chat_poll.py).

The engine is a stub in every test — a real ``ChatEngine.chat()`` would talk
to Ollama, and this module's job is transport, not generation. The stub is a
generator with the same shape (yields the raw event dicts, may raise, may run
slowly), which is exactly the surface chat_poll depends on.

The load-bearing assertions are the index ones: ``after``/``next`` must
partition the event list with no gap and no duplicate, because a gap is a
silently truncated answer on the phone and a duplicate is doubled text.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("C3_ORACLE_API_KEY", "chat-poll-key")

from oracle.services import api_auth, chat_poll  # noqa: E402

TOKEN = "chat-poll-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _StubStore:
    """Just the two methods the poll surface touches."""

    def __init__(self):
        self.conversations: dict[str, list] = {}
        self._n = 0

    def create_conversation(self, title=None):
        self._n += 1
        conv_id = f"conv{self._n}"
        self.conversations[conv_id] = []
        return conv_id

    def list_conversations(self, limit=50):
        return [{"id": c, "title": "t"} for c in list(self.conversations)[:limit]]

    def get_conversation(self, conv_id):
        return self.conversations.get(conv_id, [])


class _StubEngine:
    """Stands in for ChatEngine. ``script`` is what chat() yields.

    ``gate`` (when set) is waited on before each event, letting a test hold a
    turn open at a known index. ``raises`` makes the generator blow up
    partway, the case the error-capture test needs.
    """

    def __init__(self, script=None, gate=None, raises=None, per_event_delay=0.0):
        self.script = script if script is not None else [
            {"type": "meta", "conv_id": "conv1"},
            {"type": "text", "content": "hello"},
            {"type": "done", "conv_id": "conv1"},
        ]
        self.gate = gate
        self.raises = raises
        self.per_event_delay = per_event_delay
        self.calls: list[tuple] = []
        self.entered = threading.Event()

    def chat(self, conv_id, message):
        self.calls.append((conv_id, message))
        self.entered.set()
        for i, event in enumerate(self.script):
            if self.gate is not None:
                self.gate.wait(5)
            if self.per_event_delay:
                time.sleep(self.per_event_delay)
            if self.raises is not None and i == self.raises:
                raise RuntimeError("engine exploded")
            yield event

    def get_commands(self):
        return [{"name": "/help"}]

    def execute_command(self, conv_id, command):
        return {"ok": True, "command": command, "conversation_id": conv_id}


class _ChatPollBase(unittest.TestCase):
    """A Flask app carrying only the chat_poll blueprint."""

    def setUp(self):
        from flask import Flask

        chat_poll.reset_for_tests()
        self.store = _StubStore()
        self.engine = _StubEngine()
        chat_poll.configure(get_engine=lambda: self.engine,
                            get_store=lambda: self.store,
                            get_cfg=lambda: {"mobile_api_enabled": True})

        app = Flask(__name__)
        app.register_blueprint(chat_poll.bp)
        self.app = app
        self.client = app.test_client()

        patcher = mock.patch.object(api_auth, "verify",
                                    side_effect=lambda t: t == TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(chat_poll.reset_for_tests)

    def start_turn(self, message="hi", conversation_id=None):
        body = {"message": message}
        if conversation_id:
            body["conversation_id"] = conversation_id
        res = self.client.post("/api/mobile/chat/turn", json=body, headers=AUTH)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()

    def poll(self, run_id, after=0, wait=None):
        url = f"/api/mobile/chat/turn/{run_id}?after={after}"
        if wait is not None:
            url += f"&wait={wait}"
        return self.client.get(url, headers=AUTH)

    def drain(self, run_id, timeout=5.0):
        """Poll to completion. Returns (all_events, final_status, error)."""
        events, after, deadline = [], 0, time.time() + timeout
        while time.time() < deadline:
            body = self.poll(run_id, after).get_json()
            events.extend(body["events"])
            after = body["next"]
            if body["status"] != "running":
                return events, body["status"], body["error"]
            time.sleep(0.01)
        self.fail("run did not finish within timeout")

    def wait_until(self, run_id, predicate, timeout=5.0, what=""):
        """Poll until ``predicate(body)`` holds. Returns that body, or fails.

        Written for #78. A `time.sleep(0.1)` standing in for "by now the worker
        has produced an event" is a hope, and on a loaded CI runner the hope is
        occasionally wrong — the thread simply never got scheduled, the test
        asserted on a run that had collected nothing, and the failure looked
        like a bug in the code under test rather than in the test's own setup.

        The distinction that matters is between a *precondition* and an
        *assertion*. A precondition that has not been established yet should be
        waited for; one that never arrives should fail saying which condition
        never came true, not by tripping some later assertion with a number that
        means nothing to the reader.
        """
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            body = self.poll(run_id, after=0).get_json()
            if predicate(body):
                return body
            time.sleep(0.01)
        self.fail(
            f"timed out after {timeout}s waiting for "
            f"{what or 'the condition'}; last poll: "
            f"status={(body or {}).get('status')!r}, "
            f"events={len((body or {}).get('events') or [])}"
        )


class TestStartPollDone(_ChatPollBase):
    def test_start_returns_run_and_conversation_immediately(self):
        body = self.start_turn()
        self.assertTrue(body["run_id"])
        self.assertEqual(body["conversation_id"], "conv1")

    def test_start_reuses_supplied_conversation(self):
        conv = self.store.create_conversation()
        body = self.start_turn(conversation_id=conv)
        self.assertEqual(body["conversation_id"], conv)
        self.engine.entered.wait(5)
        self.assertEqual(self.engine.calls[0][0], conv)

    def test_poll_to_done_yields_every_event_in_order(self):
        run = self.start_turn()["run_id"]
        events, status, error = self.drain(run)
        self.assertEqual(status, "done")
        self.assertIsNone(error)
        self.assertEqual([e["type"] for e in events],
                         ["meta", "text", "done"])

    def test_events_are_the_raw_engine_dicts(self):
        """The wire carries the engine's dicts unmodified — the phone's
        renderer is shared with the SSE client, so any reshaping here would
        silently break one of the two."""
        self.engine.script = [{"type": "tool_call", "name": "read",
                               "args": {"p": 1}, "tool_id": "t1"},
                              {"type": "done"}]
        run = self.start_turn()["run_id"]
        events, _, _ = self.drain(run)
        self.assertEqual(events[0], {"type": "tool_call", "name": "read",
                                     "args": {"p": 1}, "tool_id": "t1"})

    def test_completed_run_is_replayable_from_zero(self):
        """The property SSE cannot offer: a backgrounded phone comes back and
        collects the whole turn."""
        run = self.start_turn()["run_id"]
        self.drain(run)
        body = self.poll(run, after=0).get_json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(len(body["events"]), 3)
        self.assertEqual(body["next"], 3)

    def test_unknown_run_is_404(self):
        self.assertEqual(self.poll("nope").status_code, 404)

    def test_message_is_required(self):
        res = self.client.post("/api/mobile/chat/turn",
                               json={"message": "  "}, headers=AUTH)
        self.assertEqual(res.status_code, 400)


class TestAfterSlicing(_ChatPollBase):
    def test_slices_partition_the_stream_without_gap_or_duplicate(self):
        self.engine.script = [{"type": "text", "content": str(i)}
                              for i in range(12)] + [{"type": "done"}]
        run = self.start_turn()["run_id"]
        events, status, _ = self.drain(run)
        self.assertEqual(status, "done")
        self.assertEqual([e.get("content") for e in events[:12]],
                         [str(i) for i in range(12)])
        self.assertEqual(len(events), 13)

    def test_next_equals_after_plus_returned_count(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        after = 1
        body = self.poll(run, after=after).get_json()
        self.assertEqual(body["next"], after + len(body["events"]))

    def test_after_past_the_end_returns_empty_not_an_error(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        body = self.poll(run, after=999).get_json()
        self.assertEqual(body["events"], [])
        self.assertEqual(body["next"], 3)

    def test_repolling_the_same_after_is_idempotent(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        first = self.poll(run, after=1).get_json()
        second = self.poll(run, after=1).get_json()
        self.assertEqual(first["events"], second["events"])

    def test_negative_and_garbage_after_are_clamped(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        for raw in ("-5", "abc"):
            body = self.client.get(
                f"/api/mobile/chat/turn/{run}?after={raw}",
                headers=AUTH).get_json()
            self.assertEqual(len(body["events"]), 3)


class TestLongPoll(_ChatPollBase):
    def test_wait_returns_early_when_an_event_arrives(self):
        gate = threading.Event()
        self.engine = _StubEngine(gate=gate)
        run = self.start_turn()["run_id"]
        self.engine.entered.wait(5)

        started = time.monotonic()
        threading.Timer(0.3, gate.set).start()
        body = self.poll(run, after=0, wait=10).get_json()
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(len(body["events"]), 1)
        self.assertLess(elapsed, 5, "long-poll did not return early")

    def test_wait_times_out_cleanly_with_zero_events(self):
        gate = threading.Event()  # never set — the turn produces nothing
        self.engine = _StubEngine(gate=gate)
        run = self.start_turn()["run_id"]
        self.engine.entered.wait(5)

        started = time.monotonic()
        body = self.poll(run, after=0, wait=1).get_json()
        elapsed = time.monotonic() - started

        self.assertEqual(body["events"], [])
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["next"], 0)
        self.assertGreaterEqual(elapsed, 0.9)
        gate.set()

    def test_wait_is_capped(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        started = time.monotonic()
        self.poll(run, after=99, wait=9999)
        # Terminal run: returns at once regardless. The cap matters only in
        # that MAX_WAIT_S bounds the parked thread.
        self.assertLess(time.monotonic() - started, chat_poll.MAX_WAIT_S)

    def test_finished_run_does_not_hold_the_connection(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        started = time.monotonic()
        body = self.poll(run, after=3, wait=5).get_json()
        self.assertEqual(body["status"], "done")
        self.assertLess(time.monotonic() - started, 2)


class TestAbort(_ChatPollBase):
    def test_abort_stops_the_run_and_reports_aborted(self):
        self.engine = _StubEngine(
            script=[{"type": "text", "content": str(i)} for i in range(50)],
            per_event_delay=0.02)
        run = self.start_turn()["run_id"]
        self.engine.entered.wait(5)

        res = self.client.delete(f"/api/mobile/chat/turn/{run}", headers=AUTH)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["aborted"])

        body = self.wait_until(run, lambda b: b["status"] != "running",
                               what="the run to leave 'running'")
        self.assertEqual(body["status"], "aborted")

    def test_aborted_run_keeps_what_it_collected(self):
        self.engine = _StubEngine(
            script=[{"type": "text", "content": str(i)} for i in range(50)],
            per_event_delay=0.02)
        run = self.start_turn()["run_id"]
        self.engine.entered.wait(5)

        # Establish the precondition rather than sleeping toward it (#78). The
        # claim under test is that an abort PRESERVES collected events, and that
        # claim says nothing at all until something has been collected — so
        # "some event exists" is setup, and waiting for it is not the same as
        # asserting it.
        before = self.wait_until(run, lambda b: len(b["events"]) > 0,
                                 what="the first event to be collected")
        collected = len(before["events"])

        self.client.delete(f"/api/mobile/chat/turn/{run}", headers=AUTH)
        body = self.wait_until(run, lambda b: b["status"] != "running",
                               what="the run to leave 'running'")

        self.assertEqual(body["status"], "aborted")
        # Retention, not mere non-emptiness. `> 0` would also pass if the abort
        # truncated five collected events down to one, which is the failure this
        # test exists to catch.
        self.assertGreaterEqual(len(body["events"]), collected)

    def test_abort_does_not_wedge_the_worker_thread(self):
        self.engine = _StubEngine(
            script=[{"type": "text", "content": str(i)} for i in range(50)],
            per_event_delay=0.01)
        run = self.start_turn()["run_id"]
        self.engine.entered.wait(5)
        with chat_poll._lock:
            thread = chat_poll._runs[run].thread
        self.client.delete(f"/api/mobile/chat/turn/{run}", headers=AUTH)
        thread.join(5)
        self.assertFalse(thread.is_alive(), "worker thread did not exit")

    def test_aborting_a_finished_run_reports_false(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        body = self.client.delete(f"/api/mobile/chat/turn/{run}",
                                  headers=AUTH).get_json()
        self.assertFalse(body["aborted"])

    def test_abort_unknown_run_is_404(self):
        res = self.client.delete("/api/mobile/chat/turn/nope", headers=AUTH)
        self.assertEqual(res.status_code, 404)


class TestErrorCapture(_ChatPollBase):
    def test_generator_exception_becomes_status_error(self):
        self.engine = _StubEngine(
            script=[{"type": "meta"}, {"type": "text", "content": "partial"},
                    {"type": "done"}],
            raises=2)
        run = self.start_turn()["run_id"]
        events, status, error = self.drain(run)
        self.assertEqual(status, "error")
        self.assertIn("engine exploded", error)
        # Everything produced before the blow-up is still delivered.
        self.assertEqual([e["type"] for e in events], ["meta", "text"])

    def test_error_run_is_terminal_and_does_not_long_poll(self):
        self.engine = _StubEngine(script=[{"type": "meta"}], raises=0)
        run = self.start_turn()["run_id"]
        self.drain(run)
        started = time.monotonic()
        body = self.poll(run, after=0, wait=5).get_json()
        self.assertEqual(body["status"], "error")
        self.assertLess(time.monotonic() - started, 2)

    def test_uninitialised_engine_is_captured_not_raised(self):
        chat_poll.configure(get_engine=lambda: None,
                            get_store=lambda: self.store,
                            get_cfg=lambda: {"mobile_api_enabled": True})
        res = self.client.post("/api/mobile/chat/turn",
                               json={"message": "hi"}, headers=AUTH)
        self.assertEqual(res.status_code, 500)


class TestRegistryBounds(_ChatPollBase):
    def test_cap_keeps_the_newest_runs_only(self):
        run_ids = [self.start_turn()["run_id"]
                   for _ in range(chat_poll.MAX_RUNS + 5)]
        # Only the survivors are drainable — the evicted ones already 404,
        # which is the behaviour under test.
        for r in run_ids[5:]:
            self.drain(r)
        with chat_poll._lock:
            self.assertLessEqual(len(chat_poll._runs), chat_poll.MAX_RUNS)
            surviving = set(chat_poll._runs)
        self.assertTrue(surviving.issubset(set(run_ids[5:])))
        self.assertEqual(self.poll(run_ids[0]).status_code, 404)

    def test_finished_runs_are_reaped_past_the_ttl(self):
        run = self.start_turn()["run_id"]
        self.drain(run)
        with chat_poll._lock:
            chat_poll._runs[run].finished = time.time() - chat_poll.RUN_TTL_S - 1
        self.start_turn()  # any insert triggers a reap
        self.assertEqual(self.poll(run).status_code, 404)

    def test_running_runs_are_never_reaped_on_age(self):
        gate = threading.Event()
        self.engine = _StubEngine(gate=gate)
        run = self.start_turn()["run_id"]
        with chat_poll._lock:
            chat_poll._runs[run].started = time.time() - chat_poll.RUN_TTL_S * 10
        self.start_turn(message="second")
        self.assertEqual(self.poll(run).status_code, 200)
        gate.set()

    def test_constants_are_bounded(self):
        self.assertGreater(chat_poll.MAX_RUNS, 0)
        self.assertGreater(chat_poll.RUN_TTL_S, 0)
        self.assertLessEqual(chat_poll.MAX_WAIT_S, 25)


class TestConcurrency(_ChatPollBase):
    def test_polls_during_a_turn_never_see_a_torn_list(self):
        """Every event index must be observed exactly once across concurrent
        readers advancing their own cursors."""
        self.engine = _StubEngine(
            script=[{"type": "text", "content": str(i)} for i in range(40)]
                   + [{"type": "done"}],
            per_event_delay=0.002)
        run = self.start_turn()["run_id"]
        collected: list[list] = []
        errors: list = []

        def reader():
            try:
                events, _, _ = self.drain(run, timeout=10)
                collected.append(events)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)

        self.assertEqual(errors, [])
        for events in collected:
            # Each reader's own slices must concatenate to a prefix of the
            # stream with no repeats — a torn read shows up as a bad count.
            texts = [e["content"] for e in events if e["type"] == "text"]
            self.assertEqual(texts, sorted(texts, key=int))
            self.assertEqual(len(texts), len(set(texts)))


class TestProxies(_ChatPollBase):
    def test_conversation_list(self):
        self.store.create_conversation()
        body = self.client.get("/api/mobile/chat/conversations",
                               headers=AUTH).get_json()
        self.assertEqual(len(body["conversations"]), 1)

    def test_conversation_create(self):
        res = self.client.post("/api/mobile/chat/conversations",
                               json={"title": "T"}, headers=AUTH)
        self.assertEqual(res.status_code, 201)
        self.assertIn("id", res.get_json())

    def test_conversation_history(self):
        conv = self.store.create_conversation()
        self.store.conversations[conv] = [{"role": "user", "content": "x"}]
        body = self.client.get(f"/api/mobile/chat/conversations/{conv}",
                               headers=AUTH).get_json()
        self.assertEqual(body["conversation_id"], conv)
        self.assertEqual(len(body["messages"]), 1)

    def test_commands_registry(self):
        body = self.client.get("/api/mobile/chat/commands",
                               headers=AUTH).get_json()
        self.assertEqual(body["commands"], [{"name": "/help"}])

    def test_command_execution(self):
        body = self.client.post("/api/mobile/chat/command",
                                json={"conversation_id": "c1",
                                      "command": "/help"},
                                headers=AUTH).get_json()
        self.assertTrue(body["ok"])

    def test_empty_command_rejected(self):
        res = self.client.post("/api/mobile/chat/command",
                               json={"command": ""}, headers=AUTH)
        self.assertEqual(res.status_code, 400)


class TestAuth(_ChatPollBase):
    def test_every_route_requires_a_bearer_token(self):
        for method, url in [
            ("post", "/api/mobile/chat/turn"),
            ("get", "/api/mobile/chat/turn/x"),
            ("delete", "/api/mobile/chat/turn/x"),
            ("get", "/api/mobile/chat/conversations"),
            ("post", "/api/mobile/chat/conversations"),
            ("get", "/api/mobile/chat/conversations/c1"),
            ("get", "/api/mobile/chat/commands"),
            ("post", "/api/mobile/chat/command"),
        ]:
            res = getattr(self.client, method)(url, json={})
            self.assertEqual(res.status_code, 401, f"{method} {url}")

    def test_wrong_token_is_401(self):
        res = self.client.get("/api/mobile/chat/commands",
                              headers={"Authorization": "Bearer wrong"})
        self.assertEqual(res.status_code, 401)

    def test_disabled_mobile_api_is_404(self):
        chat_poll.configure(get_engine=lambda: self.engine,
                            get_store=lambda: self.store,
                            get_cfg=lambda: {"mobile_api_enabled": False})
        res = self.client.get("/api/mobile/chat/commands", headers=AUTH)
        self.assertEqual(res.status_code, 404)


class TestCapability(unittest.TestCase):
    def test_capability_symbol_is_chat(self):
        """mobile_api wires this into CAPABILITIES; the name is the contract."""
        self.assertEqual(chat_poll.CAPABILITY, "chat")


class TestBlueprintRegistration(unittest.TestCase):
    def test_server_registers_the_blueprint_at_the_expected_prefix(self):
        import oracle.oracle_server as srv

        rules = {str(r) for r in srv.app.url_map.iter_rules()}
        self.assertIn("/api/mobile/chat/turn", rules)
        self.assertIn("/api/mobile/chat/turn/<run_id>", rules)
        self.assertIn("/api/mobile/chat/conversations", rules)
        self.assertIn("/api/mobile/chat/commands", rules)
        self.assertIn("/api/mobile/chat/command", rules)


if __name__ == "__main__":
    unittest.main()
