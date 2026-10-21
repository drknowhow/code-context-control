"""Approval requests reach the feed: ref_id-scoped dedup and a locked store.

The desk and the phone learn of an override request only from its line in
``.c3/notifications.jsonl``. Two things kept that line from existing:

- dedup keyed on agent+title: a second request on the same file inside the
  cooldown after the first was acknowledged wrote nothing at all;
- unlocked read-modify-write from several processes: a rewrite from a stale
  or truncated read dropped lines another process had just appended.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import override_requests as orq  # noqa: E402
from services.notifications import NotificationStore  # noqa: E402

_TITLE = "Approve Edit write on SKILL.md?"


def _lines(project: Path) -> list[dict]:
    path = project / ".c3" / "notifications.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _request_row(request_id: str, status: str = "pending") -> dict:
    return {"id": request_id, "tool": "Edit", "op": "write",
            "path": "skills/demo/SKILL.md", "rule": ".claude/**",
            "rule_class": "access_confirm", "status": status}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".c3").mkdir(parents=True)

    def tearDown(self):
        for _ in range(20):
            try:
                self._tmp.cleanup()
                break
            except OSError:
                time.sleep(0.05)


class TestRefScopedDedup(_Base):
    def test_acknowledged_request_does_not_suppress_the_next_on_the_same_file(self):
        store = NotificationStore(str(self.project))
        first = store.add("override", "critical", _TITLE, "approve ovr_a",
                          kind="override", ref_id="ovr_a")
        store.acknowledge(first["id"])

        second = store.add("override", "critical", _TITLE, "approve ovr_b",
                           kind="override", ref_id="ovr_b")

        self.assertIsNotNone(second)
        self.assertEqual({e.get("ref_id") for e in _lines(self.project)},
                         {"ovr_a", "ovr_b"})

    def test_pending_requests_sharing_a_title_survive_the_collapse(self):
        store = NotificationStore(str(self.project))
        store.add("override", "critical", _TITLE, "approve ovr_a",
                  kind="override", ref_id="ovr_a")
        store.add("override", "critical", _TITLE, "approve ovr_b",
                  kind="override", ref_id="ovr_b")

        removed = NotificationStore(str(self.project)).collapse_duplicates()

        self.assertEqual(removed, 0)
        self.assertEqual(len(_lines(self.project)), 2)

    def test_the_same_ref_id_still_collapses_into_one_record(self):
        store = NotificationStore(str(self.project))
        store.add("ci", "warning", "CI failed", "run 7", kind="ci", ref_id="run_7")
        again = store.add("ci", "warning", "CI failed", "run 7",
                          kind="ci", ref_id="run_7")

        self.assertIsNone(again)
        (only,) = _lines(self.project)
        self.assertEqual(only["count"], 2)

    def test_replace_if_unacked_still_retargets_by_title(self):
        store = NotificationStore(str(self.project))
        store.add("agent", "warning", "Index stale", "one", ref_id="r1")
        store.add("agent", "warning", "Index stale", "two", ref_id="r2",
                  replace_if_unacked=True)

        (only,) = _lines(self.project)
        self.assertEqual((only["ref_id"], only["message"]), ("r2", "two"))


class TestOverrideLines(_Base):
    def test_second_request_on_a_file_is_notified_after_the_first_was_read(self):
        policy = SimpleNamespace(notify_severity="critical")
        orq._notify(str(self.project), _request_row("ovr_a"), policy)
        store = NotificationStore(str(self.project))
        store.acknowledge(_lines(self.project)[0]["id"])

        orq._notify(str(self.project), _request_row("ovr_b"), policy)

        refs = [e.get("ref_id") for e in _lines(self.project)
                if e.get("kind") == "override"]
        self.assertEqual(refs, ["ovr_a", "ovr_b"])

    def test_two_decisions_on_one_file_are_two_lines(self):
        orq._notify_decision(str(self.project), _request_row("ovr_a", "approved"))
        store = NotificationStore(str(self.project))
        store.acknowledge(_lines(self.project)[0]["id"])

        orq._notify_decision(str(self.project), _request_row("ovr_b", "approved"))

        decisions = _lines(self.project)
        self.assertEqual([e.get("ref_id") for e in decisions], ["ovr_a", "ovr_b"])
        self.assertTrue(all(not e.get("kind") for e in decisions))


_WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    from services.notifications import NotificationStore
    project, tag, count = sys.argv[2], sys.argv[3], int(sys.argv[4])
    for i in range(count):
        NotificationStore(project).add(
            "override", "critical", f"req {tag}-{i}", "m",
            kind="override", ref_id=f"{tag}-{i}")
""")

_REWRITER = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from services.notifications import NotificationStore
    project, stop = sys.argv[2], sys.argv[3]
    while not os.path.exists(stop):
        store = NotificationStore(project)
        store.get_pending_summary()
        store.acknowledge_all()
""")


class TestConcurrentWriters(_Base):
    def test_lines_appended_by_one_process_survive_rewrites_by_others(self):
        writers, per_writer = 3, 30
        stop = Path(self._tmp.name) / "stop"
        args = [str(REPO_ROOT), str(self.project)]
        rewriters = [subprocess.Popen([sys.executable, "-c", _REWRITER, *args, str(stop)])
                     for _ in range(2)]
        try:
            procs = [subprocess.Popen([sys.executable, "-c", _WRITER, *args,
                                       f"w{n}", str(per_writer)])
                     for n in range(writers)]
            for proc in procs:
                self.assertEqual(proc.wait(timeout=120), 0)
        finally:
            stop.touch()
            for proc in rewriters:
                proc.wait(timeout=60)

        refs = [e.get("ref_id") for e in _lines(self.project)]
        self.assertEqual(len(refs), writers * per_writer)
        self.assertEqual(len(set(refs)), writers * per_writer)


if __name__ == "__main__":
    unittest.main()
