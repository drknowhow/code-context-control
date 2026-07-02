"""Notification dedup + scoped ArtifactScan warnings (Stream C/P4).

Regression: the (retired) KeyFileVersionAgent re-notified every cycle without
checking for an existing unread duplicate — a live store accumulated 13
identical warning lines with no detail about which file changed. This locks in:

  1. Store-level dedup: identical (agent, title, message) unacked adds
     collapse into one record with count/last_seen.
  2. Retro-cleanup: pre-existing unacked duplicate backlogs (same agent+title)
     self-heal lazily on first store access.
  3. ArtifactScanAgent (its successor) stays silent on the baseline cycle,
     notifies ONLY for out-of-band (source=scan) changes to security-relevant
     classes, emits per-artifact detail, and updates its pending notice in
     place instead of appending duplicates.
  4. The c3_status notifications view renders collapsed records as one line
     with '(xN, last HH:MM)' and caps the list with a '+N more' tail.
"""
import json
import tempfile
import unittest
from pathlib import Path

from cli.tools.status import _notifications_view
from services.agents import ArtifactScanAgent
from services.notifications import NotificationStore


def _passthrough_finalize(tool, args, text, tag):
    return text


class _SvcStub:
    def __init__(self, store):
        self.notifications = store


class _ArtifactStoreStub:
    """Minimal ArtifactStore stand-in: canned pending/scan results."""

    def __init__(self):
        self.pending = {"consumed": 0, "events": []}
        self.result = {"added": [], "modified": [], "deleted": [],
                       "unchanged": 0, "events": []}

    def consume_pending(self):
        return self.pending

    def scan(self, paths=None, attribution=None):
        return self.result


def _ev(aid, cls, event="modified", source="scan", version=4):
    return {"artifact_id": aid, "class": cls, "event": event,
            "source": source, "version": version}


class _StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir()
        self.store = NotificationStore(str(self.project))
        self.jsonl = self.project / ".c3" / "notifications.jsonl"

    def tearDown(self):
        self.tmp.cleanup()


# ── 1. Store-level dedup upsert ───────────────────────────────────────────
class TestStoreDedup(_StoreTestCase):
    def test_identical_add_collapses_with_count(self):
        first = self.store.add("KeyFileVersion", "warning", "drift", "same msg")
        self.assertIsNotNone(first)
        self.assertEqual(first["count"], 1)
        for _ in range(2):
            self.assertIsNone(
                self.store.add("KeyFileVersion", "warning", "drift", "same msg"))

        pending = self.store.get_unacknowledged()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["count"], 3)
        self.assertGreaterEqual(pending[0]["last_seen"], pending[0]["timestamp"])

    def test_distinct_messages_still_append(self):
        self.store.add("AgentA", "warning", "title", "message one")
        self.store.add("AgentA", "warning", "title", "message two")
        # Within one live store instance a different message is new info.
        self.assertEqual(len(self.store.get_unacknowledged()), 2)

    def test_replace_if_unacked_updates_in_place_and_bumps_count(self):
        self.store.add("KeyFileVersion", "info", "drift", "old detail")
        updated = self.store.add("KeyFileVersion", "warning", "drift", "new detail",
                                 replace_if_unacked=True)
        self.assertIsNotNone(updated)
        pending = self.store.get_unacknowledged(severities=())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["message"], "new detail")
        self.assertEqual(pending[0]["severity"], "warning")
        self.assertEqual(pending[0]["count"], 2)

    def test_pending_summary_shows_repeat_count(self):
        self.store.add("KeyFileVersion", "warning", "drift", "same msg")
        self.store.add("KeyFileVersion", "warning", "drift", "same msg")
        summary = self.store.get_pending_summary()
        self.assertIn("(x2)", summary)
        self.assertIn("KeyFileVersion: drift — same msg", summary)


# ── 2. Retro-cleanup of pre-existing duplicate backlogs ──────────────────
class TestRetroCollapse(_StoreTestCase):
    def _seed_backlog(self):
        """13 legacy unacked same-title warnings (distinct messages, no
        count/last_seen fields), 1 unrelated warning, 2 acked entries."""
        rows = []
        for i in range(13):
            rows.append({
                "id": f"dup{i:02d}", "agent": "KeyFileVersion",
                "severity": "warning", "title": "Key file versions changed",
                "message": f"drift snapshot {i}", "message_hash": f"h{i:02d}",
                "timestamp": f"2026-07-01T10:{i:02d}:00+00:00",
                "acknowledged": False, "ai_enhanced": False,
            })
        rows.append({
            "id": "other01", "agent": "IndexStaleness", "severity": "warning",
            "title": "Index is stale", "message": "rebuild", "message_hash": "hx",
            "timestamp": "2026-07-01T09:00:00+00:00",
            "acknowledged": False, "ai_enhanced": False,
        })
        for i in range(2):
            rows.append({
                "id": f"ack{i:02d}", "agent": "KeyFileVersion",
                "severity": "warning", "title": "Key file versions changed",
                "message": f"old acked {i}", "message_hash": f"ha{i}",
                "timestamp": f"2026-06-30T10:{i:02d}:00+00:00",
                "acknowledged": True, "ai_enhanced": False,
            })
        with open(self.jsonl, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_lazy_collapse_on_first_access(self):
        self._seed_backlog()
        store = NotificationStore(str(self.project))  # fresh instance
        pending = store.get_unacknowledged(limit=50)
        self.assertEqual(len(pending), 2)  # collapsed drift + unrelated warning

        drift = next(e for e in pending if e["agent"] == "KeyFileVersion")
        self.assertEqual(drift["count"], 13)
        self.assertEqual(drift["message"], "drift snapshot 12")  # newest wins
        self.assertEqual(drift["last_seen"], "2026-07-01T10:12:00+00:00")
        self.assertEqual(drift["severity"], "warning")

        # Acknowledged history is never touched.
        history = store.get_history(limit=100)
        acked = [e for e in history if e.get("acknowledged")]
        self.assertEqual(len(acked), 2)

    def test_explicit_collapse_returns_removed_count(self):
        self._seed_backlog()
        store = NotificationStore(str(self.project))
        self.assertEqual(store.collapse_duplicates(), 12)
        self.assertEqual(store.collapse_duplicates(), 0)  # idempotent

    def test_collapse_runs_once_per_instance(self):
        self._seed_backlog()
        store = NotificationStore(str(self.project))
        store.get_pending_count()
        # Post-collapse, a distinct-message add appends normally and the
        # lazy pass does not re-merge it within the same instance.
        store.add("KeyFileVersion", "warning", "Key file versions changed",
                  "brand new drift")
        self.assertEqual(len(store.get_unacknowledged(limit=50)), 3)


# ── 3. ArtifactScanAgent: primed baseline, scoped, non-duplicating ───────
class TestArtifactScanAgent(_StoreTestCase):
    def _agent(self, store_stub, **kw):
        return ArtifactScanAgent(store_stub, self.store, enabled=False, **kw)

    def test_baseline_cycle_is_silent(self):
        stub = _ArtifactStoreStub()
        stub.result["events"] = [_ev("settings:.claude/settings.local.json",
                                     "settings")]
        agent = self._agent(stub)
        agent.check()  # priming pass — re-syncs state, never notifies
        self.assertEqual(self.store.get_unacknowledged(severities=()), [])

    def test_out_of_band_settings_change_notifies_in_place(self):
        stub = _ArtifactStoreStub()
        agent = self._agent(stub)
        agent.check()  # prime
        stub.result["events"] = [
            _ev("settings:.claude/settings.local.json", "settings"),
            _ev("mcp:.mcp.json", "mcp", event="deleted"),
        ]
        agent.check()
        pending = self.store.get_unacknowledged(severities=())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["severity"], "warning")
        msg = pending[0]["message"]
        self.assertIn("settings:.claude/settings.local.json (modified v4)", msg)
        self.assertIn("mcp:.mcp.json (deleted)", msg)

        agent.check()  # same drift next cycle — updated in place, no duplicate
        pending = self.store.get_unacknowledged(severities=())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["count"], 2)

    def test_attributed_sources_and_other_classes_stay_quiet(self):
        stub = _ArtifactStoreStub()
        agent = self._agent(stub)
        agent.check()  # prime
        stub.pending["events"] = [
            _ev("instructions:CLAUDE.md", "instructions", source="c3_edit"),
            _ev("settings:.claude/settings.local.json", "settings",
                source="install_mcp"),
        ]
        stub.result["events"] = [
            _ev("skill:browcontrol", "skill"),                 # class not watched
            _ev("mcp:.mcp.json", "mcp", event="created"),      # created ≠ drift
        ]
        agent.check()
        self.assertEqual(self.store.get_unacknowledged(severities=()), [])

    def test_overflow_tail_when_many_artifacts_change(self):
        stub = _ArtifactStoreStub()
        agent = self._agent(stub)
        agent.check()  # prime
        stub.result["events"] = [
            _ev(f"settings:.claude/s{i}.json", "settings") for i in range(6)
        ]
        agent.check()
        msg = self.store.get_unacknowledged(severities=())[0]["message"]
        self.assertIn("(+2 more)", msg)
        self.assertIn("s0.json", msg)
        self.assertNotIn("s5.json (", msg)


# ── 4. Status view rendering ──────────────────────────────────────────────
class TestStatusNotificationsView(_StoreTestCase):
    def _render(self):
        return _notifications_view(_SvcStub(self.store), _passthrough_finalize)

    def test_collapsed_record_renders_count_and_last_seen(self):
        for _ in range(3):
            self.store.add("KeyFileVersion", "warning",
                           "Key file versions changed",
                           "cli/mcp_server.py (3f2a1bc->9d4e2aa). Git dirty: 1.")
        out = self._render()
        self.assertIn("# Actionable (1)", out)
        self.assertIn("[warning] KeyFileVersion: Key file versions changed "
                      "— cli/mcp_server.py (3f2a1bc->9d4e2aa). Git dirty: 1. "
                      "(x3, last ", out)
        # One rendered line per record — no 3x pile-up.
        self.assertEqual(
            sum("KeyFileVersion" in line for line in out.splitlines()), 1)

    def test_list_capped_with_more_tail(self):
        for i in range(12):
            self.store.add(f"Agent{i}", "warning", f"warning {i}", f"detail {i}")
        out = self._render()
        self.assertIn("# Actionable (12)", out)
        self.assertIn("... +2 more", out)
        rendered = [ln for ln in out.splitlines() if ln.startswith("[warning]")]
        self.assertEqual(len(rendered), 10)

    def test_long_message_truncated(self):
        self.store.add("AgentX", "critical", "big", "y" * 400)
        out = self._render()
        self.assertIn("...", out)
        self.assertNotIn("y" * 200, out)

    def test_no_pending_message_unchanged(self):
        self.store.add("FileMemory", "info", "maps updated", "5 files")
        out = self._render()
        self.assertIn("No actionable notifications.", out)
        self.assertIn("(1 info events archived)", out)


if __name__ == "__main__":
    unittest.main()
