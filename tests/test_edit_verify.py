"""#74 — `c3_edits(action='verify')`: did that edit land?

The three verdicts have to stay apart, and the tests are organized around that
rather than around the functions. Collapsing INCONCLUSIVE into either neighbour
is the failure mode: told APPLIED when it cannot tell, a caller loses work;
told NOT_APPLIED when it cannot tell, a caller applies an edit twice.

The double-apply case is not hypothetical, so it gets its own test — an edit
whose ``new_string`` contains its ``old_string`` is what you write every time
you append a line, and it is the shape a blind retry corrupts.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cli.tools.edit_verify import (
    VERDICT_APPLIED,
    VERDICT_INCONCLUSIVE,
    VERDICT_NOT_APPLIED,
    verify,
)


class _Ledger:
    """A ledger holding whatever entries a test hands it."""

    def __init__(self, entries=None):
        self.entries = entries or []

    def get_history(self, file=None, limit=50, since=None, branch=None):
        return [e for e in self.entries if file is None or e.get("file") == file]


class _Broken:
    """A ledger that raises. verify must still answer from the file."""

    def get_history(self, *a, **kw):
        raise RuntimeError("ledger unreadable")


class _Svc:
    def __init__(self, project_path, ledger=None):
        self.project_path = str(project_path)
        self.edit_ledger = ledger


def _entry(rel, old, new, eid="edit_1"):
    return {"id": eid, "timestamp": "2026-08-08T12:00:00+00:00", "file": rel,
            "detail": {"old_string": old, "new_string": new}}


def _batch_entry(rel, pairs, eid="edit_b"):
    return {"id": eid, "timestamp": "2026-08-08T12:00:00+00:00", "file": rel,
            "detail": {"patches": [{"old_string": o, "new_string": n}
                                   for o, n in pairs]}}


class VerifyBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.rel = "src/mod.py"
        self.file = self.root / "src" / "mod.py"
        self.file.parent.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, newline="\n"):
        self.file.write_bytes(text.replace("\n", newline).encode("utf-8"))

    def run_verify(self, old, new, ledger=None, edits=""):
        return verify(str(self.file), old, new, edits,
                      _Svc(self.root, ledger))


class TestTheThreeVerdicts(VerifyBase):
    def test_new_string_absent_is_not_applied(self):
        """Definitive: the edit's own replacement text would be there."""
        self.write("alpha\nbeta\n")
        body, summ = self.run_verify("alpha", "ALPHA", _Ledger())
        self.assertIn(VERDICT_NOT_APPLIED, body)
        self.assertIn("safe to re-send", body)
        self.assertIn(VERDICT_NOT_APPLIED, summ)

    def test_new_string_present_with_a_matching_ledger_entry_is_applied(self):
        self.write("ALPHA\nbeta\n")
        led = _Ledger([_entry(self.rel, "alpha", "ALPHA")])
        body, _ = self.run_verify("alpha", "ALPHA", led)
        self.assertIn(VERDICT_APPLIED, body)
        self.assertIn("do NOT retry", body)
        self.assertIn("edit_1", body)

    def test_new_string_present_without_a_ledger_entry_is_inconclusive(self):
        """The text may have been there all along. Do not guess either way."""
        self.write("ALPHA\nbeta\n")
        body, _ = self.run_verify("alpha", "ALPHA", _Ledger())
        self.assertIn(VERDICT_INCONCLUSIVE, body)
        self.assertNotIn(VERDICT_APPLIED, body)
        self.assertIn("read the file", body)

    def test_a_ledger_entry_for_a_different_edit_does_not_corroborate(self):
        self.write("ALPHA\nbeta\n")
        led = _Ledger([_entry(self.rel, "gamma", "GAMMA")])
        body, _ = self.run_verify("alpha", "ALPHA", led)
        self.assertIn(VERDICT_INCONCLUSIVE, body)


class TestTheDoubleApplyCase(VerifyBase):
    """new_string contains old_string — what a blind retry corrupts.

    Both strings are present in the file after a successful apply, so a checker
    keying on "is old_string still there" would call this NOT_APPLIED and invite
    the second application.
    """

    OLD = "def f():\n    return 1\n"
    NEW = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"

    def test_after_applying_it_reads_as_applied_not_retryable(self):
        self.write(self.NEW)
        led = _Ledger([_entry(self.rel, self.OLD, self.NEW)])
        body, _ = self.run_verify(self.OLD, self.NEW, led)
        self.assertIn(VERDICT_APPLIED, body)
        self.assertNotIn(VERDICT_NOT_APPLIED, body)

    def test_before_applying_it_reads_as_not_applied(self):
        self.write(self.OLD)
        body, _ = self.run_verify(self.OLD, self.NEW, _Ledger())
        self.assertIn(VERDICT_NOT_APPLIED, body)


class TestEvidenceSources(VerifyBase):
    def test_a_missing_file_is_not_applied(self):
        body, _ = self.run_verify("a", "A", _Ledger())
        self.assertIn(VERDICT_NOT_APPLIED, body)

    def test_a_broken_ledger_still_yields_the_file_verdict(self):
        """The file is the primary evidence; losing the ledger must not blind us."""
        self.write("alpha\n")
        body, _ = self.run_verify("alpha", "ALPHA", _Broken())
        self.assertIn(VERDICT_NOT_APPLIED, body)

    def test_a_broken_ledger_downgrades_applied_to_inconclusive(self):
        self.write("ALPHA\n")
        body, _ = self.run_verify("alpha", "ALPHA", _Broken())
        self.assertIn(VERDICT_INCONCLUSIVE, body)

    def test_no_ledger_at_all_is_not_an_error(self):
        """verify runs before the ledger-availability gate on purpose."""
        self.write("alpha\n")
        body, _ = verify(str(self.file), "alpha", "ALPHA", "",
                         _Svc(self.root, None))
        self.assertIn(VERDICT_NOT_APPLIED, body)

    def test_a_crlf_file_verifies_against_normalized_content(self):
        """c3_edit matches on LF-normalized text, so verify must read it the same.

        Without this the verdict for every CRLF file with a multi-line edit is
        NOT_APPLIED — which is the answer that invites a retry.
        """
        self.write("x\nALPHA\nBETA\ny\n", newline="\r\n")
        led = _Ledger([_entry(self.rel, "alpha\nbeta", "ALPHA\nBETA")])
        body, _ = self.run_verify("alpha\nbeta", "ALPHA\nBETA", led)
        self.assertIn(VERDICT_APPLIED, body)


class TestPathSpelling(VerifyBase):
    def test_an_absolute_path_matches_the_ledgers_relative_spelling(self):
        """The caller pastes the path they gave c3_edit, which is often absolute."""
        self.write("ALPHA\n")
        led = _Ledger([_entry(self.rel, "alpha", "ALPHA")])
        body, _ = verify(str(self.file), "alpha", "ALPHA", "",
                         _Svc(self.root, led))
        self.assertIn(VERDICT_APPLIED, body)

    def test_a_relative_path_works_too(self):
        self.write("ALPHA\n")
        led = _Ledger([_entry(self.rel, "alpha", "ALPHA")])
        body, _ = verify(self.rel, "alpha", "ALPHA", "", _Svc(self.root, led))
        self.assertIn(VERDICT_APPLIED, body)


class TestBatchVerify(VerifyBase):
    EDITS = ('[{"old_string": "a", "new_string": "A"},'
             ' {"old_string": "b", "new_string": "B"}]')

    def test_each_patch_is_reported_separately(self):
        self.write("A\nb\n")
        led = _Ledger([_batch_entry(self.rel, [("a", "A"), ("b", "B")])])
        body, summ = self.run_verify("", "", led, edits=self.EDITS)
        self.assertIn("patch[0]: " + VERDICT_APPLIED, body)
        self.assertIn("patch[1]: " + VERDICT_NOT_APPLIED, body)
        self.assertIn("applied", summ)

    def test_a_mixed_result_is_labelled_as_missed_hunks_not_a_torn_write(self):
        self.write("A\nb\n")
        led = _Ledger([_batch_entry(self.rel, [("a", "A"), ("b", "B")])])
        body, _ = self.run_verify("", "", led, edits=self.EDITS)
        self.assertIn("not a partial write", body)

    def test_malformed_edits_json_is_refused(self):
        self.write("x\n")
        body, summ = self.run_verify("", "", _Ledger(), edits="{nope")
        self.assertIn("valid JSON list", body)
        self.assertEqual(summ, "bad edits param")


class TestArgumentGuards(VerifyBase):
    def test_no_file_is_refused(self):
        body, summ = verify("", "a", "A", "", _Svc(self.root, _Ledger()))
        self.assertEqual(summ, "missing file")

    def test_no_edit_at_all_is_refused(self):
        """verify answers 'did THIS edit land', which needs the edit."""
        self.write("x\n")
        body, summ = self.run_verify("", "", _Ledger())
        self.assertEqual(summ, "missing params")


class TestRouting(VerifyBase):
    def test_handle_edits_routes_verify_without_a_ledger(self):
        from cli.tools.edits import handle_edits

        captured = {}

        def finalize(name, args, resp, summ, **kw):
            captured.update(name=name, resp=resp, summ=summ)
            return resp

        self.write("alpha\n")
        handle_edits("verify", str(self.file), "", "", "", "", 50, "", "", "",
                     _Svc(self.root, None), finalize, "", "alpha", "ALPHA", "")
        self.assertIn(VERDICT_NOT_APPLIED, captured["resp"])
        self.assertNotIn("Edit ledger not available", captured["resp"])

    def test_unknown_action_message_lists_verify(self):
        from cli.tools.edits import handle_edits

        out = {}

        def finalize(name, args, resp, summ, **kw):
            out["resp"] = resp
            return resp

        handle_edits("bogus", "", "", "", "", "", 50, "", "", "",
                     _Svc(self.root, _Ledger()), finalize)
        self.assertIn("verify", out["resp"])


class TestAccessGuard(VerifyBase):
    """A verdict about a denied file is still a read of it.

    verify returns "does the file contain this string", never the content — and
    that is precisely why it needs the guard. Answered one substring at a time,
    it is a content read with extra steps.
    """

    def test_a_denied_path_is_refused_before_the_file_is_touched(self):
        denied = self.root / ".env"
        denied.write_text("SECRET=hunter2\n", encoding="utf-8")
        body, summ = verify(str(denied), "SECRET=", "SECRET=x", "",
                            _Svc(self.root, _Ledger()))
        self.assertEqual(summ, "access-denied")
        self.assertNotIn(VERDICT_APPLIED, body)
        self.assertNotIn(VERDICT_NOT_APPLIED, body)

    def test_an_ordinary_project_file_is_not_refused(self):
        self.write("alpha\n")
        body, summ = self.run_verify("alpha", "ALPHA", _Ledger())
        self.assertNotEqual(summ, "access-denied")


class TestAgainstARealEdit(VerifyBase):
    """The wiring, not the logic — a real c3_edit followed by a real verify.

    Every test above pins the decision rules against a fake ledger, which proves
    the rules and nothing about whether the two halves agree in production. They
    only agree if three spellings line up: the path c3_edit stores (`rel`), the
    path verify looks up, and the shape of the `detail` dict. A fake ledger
    agrees with itself by construction, so a drift in any of the three would be
    invisible to every test but this one.
    """

    def _svc(self):
        from services.edit_ledger import EditLedger

        class Svc:
            pass

        svc = Svc()
        svc.project_path = str(self.root)
        svc.edit_ledger = EditLedger(str(self.root))
        svc.activity_log = None
        svc.session_mgr = None
        svc.artifact_store = None
        return svc

    @staticmethod
    def _finalize(name, args, resp, summ, **kw):
        return resp

    def test_a_real_single_edit_then_verify_reads_applied(self):
        from cli.tools.edit import handle_edit

        self.write("def f():\n    return 1\n")
        svc = self._svc()
        old, new = "return 1", "return 2"
        out = handle_edit(str(self.file), old, new, "", "", False,
                          svc, self._finalize)
        self.assertIn("✓", out)

        body, _ = verify(str(self.file), old, new, "", svc)
        self.assertIn(VERDICT_APPLIED, body)

    def test_verify_before_the_edit_reads_not_applied(self):
        self.write("def f():\n    return 1\n")
        body, _ = verify(str(self.file), "return 1", "return 2", "", self._svc())
        self.assertIn(VERDICT_NOT_APPLIED, body)

    def test_a_real_batch_edit_then_verify_reads_applied_per_patch(self):
        import json

        from cli.tools.edit import handle_edit

        self.write("a\nb\n")
        svc = self._svc()
        batch = json.dumps([
            {"old_string": "a", "new_string": "A"},
            {"old_string": "b", "new_string": "B"},
        ])
        out = handle_edit(str(self.file), "", "", "", "", False,
                          svc, self._finalize, batch)
        self.assertIn("2/2 patches applied", out)

        body, _ = verify(str(self.file), "", "", batch, svc)
        self.assertIn("patch[0]: " + VERDICT_APPLIED, body)
        self.assertIn("patch[1]: " + VERDICT_APPLIED, body)


if __name__ == "__main__":
    unittest.main()
