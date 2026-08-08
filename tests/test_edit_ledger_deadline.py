"""#74 — a stalled ledger write must not hold the file write hostage.

`c3_edit` writes the file, *then* records it. That ordering is what made the
logged incident expensive: one call hung until the harness aborted it at 1800s
while the file on disk had been correct the whole time. Half an hour bought a
report of work that finished in milliseconds.

The host-side cause is not C3's (see the issue — commit-charge exhaustion on the
box, in short unpredictable windows), and nothing here prevents it. What these
tests pin is that C3 no longer *pays 1800s to find out*: the bookkeeping runs on
a daemon thread, the caller stops waiting at the deadline, and the response says
plainly that the write landed and the record may not have.

The two assertions that matter are opposites, and both have to hold:

- a stalled ledger must NOT suppress the success line — the write happened;
- a stalled ledger must NOT be silent — a degraded record that renders like a
  clean one is the failure class this subsystem exists to remove.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from cli.tools import edit as edit_mod
from cli.tools.edit import handle_edit


class _StallingLedger:
    """A ledger whose write never returns until the test releases it."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()

    def log_edit(self, **kw):
        self.entered.set()
        self.release.wait(30)
        return {"id": "edit_late"}


class _FastLedger:
    def __init__(self):
        self.calls = []

    def log_edit(self, **kw):
        self.calls.append(kw)
        return {"id": "edit_1"}


class _Svc:
    def __init__(self, project_path, ledger):
        self.project_path = str(project_path)
        self.edit_ledger = ledger
        self.activity_log = None
        self.session_mgr = None
        self.artifact_store = None


def _finalize(name, args, resp, summ, **kw):
    return resp


class DeadlineBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.file = self.root / "mod.py"
        self.file.write_text("def f():\n    return 1\n", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        # Real deadline is 10s; these tests must not spend it.
        self._real = edit_mod._LEDGER_DEADLINE_S
        edit_mod._LEDGER_DEADLINE_S = 0.2
        self.addCleanup(self._restore)

    def _restore(self):
        edit_mod._LEDGER_DEADLINE_S = self._real


class TestAStalledLedgerDoesNotBlockTheEdit(DeadlineBase):
    def setUp(self):
        super().setUp()
        self.ledger = _StallingLedger()
        self.addCleanup(self.ledger.release.set)

    def test_the_call_returns_and_reports_success(self):
        out = handle_edit(str(self.file), "return 1", "return 2", "", "",
                          False, _Svc(self.root, self.ledger), _finalize)
        self.assertIn("✓", out)

    def test_the_response_says_the_record_may_be_missing(self):
        out = handle_edit(str(self.file), "return 1", "return 2", "", "",
                          False, _Svc(self.root, self.ledger), _finalize)
        self.assertIn("[c3:ledger-deferred]", out)
        self.assertIn("FILE WRITE SUCCEEDED", out)
        self.assertIn("Do not re-apply", out)

    def test_the_file_really_was_written(self):
        """The whole point: the work is done, only the paperwork is late."""
        handle_edit(str(self.file), "return 1", "return 2", "", "", False,
                    _Svc(self.root, self.ledger), _finalize)
        self.assertIn("return 2", self.file.read_text(encoding="utf-8"))

    def test_the_bookkeeping_thread_was_actually_started(self):
        """Deferred means left running, not skipped — it may still land."""
        handle_edit(str(self.file), "return 1", "return 2", "", "", False,
                    _Svc(self.root, self.ledger), _finalize)
        self.assertTrue(self.ledger.entered.wait(5))

    def test_batch_mode_reports_it_too(self):
        edits = json.dumps([{"old_string": "return 1", "new_string": "return 2"}])
        out = handle_edit(str(self.file), "", "", "", "", False,
                          _Svc(self.root, self.ledger), _finalize, edits)
        self.assertIn("1/1 patches applied", out)
        self.assertIn("[c3:ledger-deferred]", out)

    def test_create_mode_reports_it_too(self):
        new = self.root / "created.py"
        out = handle_edit(str(new), "", "hello\n", "", "", False,
                          _Svc(self.root, self.ledger), _finalize)
        self.assertIn("created", out)
        self.assertIn("[c3:ledger-deferred]", out)
        self.assertEqual(new.read_text(encoding="utf-8"), "hello\n")


class TestTheNormalPathIsUnchanged(DeadlineBase):
    def test_a_fast_ledger_adds_no_note(self):
        ledger = _FastLedger()
        out = handle_edit(str(self.file), "return 1", "return 2", "", "",
                          False, _Svc(self.root, ledger), _finalize)
        self.assertIn("✓", out)
        self.assertNotIn("ledger-deferred", out)
        self.assertEqual(len(ledger.calls), 1)

    def test_a_raising_ledger_adds_no_note_either(self):
        """A failed record is already swallowed by design; only a STALL is news."""
        class _Boom:
            def log_edit(self, **kw):
                raise RuntimeError("ledger exploded")

        out = handle_edit(str(self.file), "return 1", "return 2", "", "",
                          False, _Svc(self.root, _Boom()), _finalize)
        self.assertIn("✓", out)
        self.assertNotIn("ledger-deferred", out)

    def test_no_ledger_at_all_adds_no_note(self):
        out = handle_edit(str(self.file), "return 1", "return 2", "", "",
                          False, _Svc(self.root, None), _finalize)
        self.assertIn("✓", out)
        self.assertNotIn("ledger-deferred", out)


class TestFailedEditsAreUnaffected(DeadlineBase):
    def test_a_miss_still_reports_not_found_without_a_deferred_note(self):
        """Nothing was written, so there is no record to be late about."""
        out = handle_edit(str(self.file), "no such text", "x", "", "", False,
                          _Svc(self.root, _StallingLedger()), _finalize)
        self.assertIn("not found", out)
        self.assertNotIn("ledger-deferred", out)


if __name__ == "__main__":
    unittest.main()
