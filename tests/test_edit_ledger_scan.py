"""The enricher's pending scan parses the ledger once, then only what was appended.

EditLedgerEnricherAgent calls enrich_pending() and validate_pending() every
cycle. Each used to parse the whole ledger to find the few entries still
waiting (26,640 lines, 8.3 MB on the largest project here), and
validate_pending() kept retrying entries its cache never checks: five
Markdown edits after a Python edit filled every batch with answers of None,
so the Python edit was never checked.

These tests pin the incremental scan. Appended lines are folded in, an
unchanged file is not read again, a file replaced or rewritten in place is
read from the start, and a half-written line waits for its newline. Entries
the cache never checks, entries whose file is gone and entries older than a
day leave the queue instead of being retried; a busy checker is retried.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services.edit_ledger import EditLedger


def _iso(**ago) -> str:
    return (datetime.now(timezone.utc) - timedelta(**ago)).isoformat()


class _Cache:
    """Checks the given extensions; ``answers`` queues results per file."""

    def __init__(self, exts=(".py",), answers=None):
        self.exts = exts
        self.answers = answers or {}
        self.calls = []

    def eligible(self, rel):
        return Path(rel).suffix in self.exts

    def validate_file(self, rel):
        self.calls.append(rel)
        queue = self.answers.get(rel)
        if not queue:
            return {"valid": True, "errors": []}
        return queue.pop(0) if len(queue) > 1 else queue[0]


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = EditLedger(str(self.root))
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, rel, *, git_pending=False, ts=None, pad=0, newline=True):
        """Append a base entry for *rel*, create the file, return the entry id."""
        self.n += 1
        eid = f"edit_{self.n:04d}"
        entry = {"id": eid, "timestamp": ts or _iso(), "file": rel,
                 "change_type": "edit", "summary": "x" * pad, "version": "v1"}
        if git_pending:
            entry["git_pending"] = True
        self.append(json.dumps(entry) + ("\n" if newline else ""))
        (self.root / rel).write_text("x = 1\n", encoding="utf-8")
        return eid

    def append(self, text):
        with open(self.ledger.ledger_file, "a", encoding="utf-8") as fh:
            fh.write(text)

    def lines(self):
        text = self.ledger.ledger_file.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def patched(self, key):
        return [e["target_id"] for e in self.lines() if "target_id" in e and key in e]

    def scan(self):
        with self.ledger._scan_lock:
            self.ledger._scan()


class TestIncrementalScan(_LedgerCase):
    def test_only_appended_bytes_are_parsed(self):
        first = self.add("a.py")
        with mock.patch.object(self.ledger, "_fold", wraps=self.ledger._fold) as fold:
            self.scan()
            self.scan()  # unchanged file: one stat, nothing read
            self.assertEqual(fold.call_count, 1)
            second = self.add("b.py")
            self.scan()
        self.assertEqual(fold.call_count, 2)
        chunk = fold.call_args[0][0]
        self.assertIn(second.encode(), chunk)
        self.assertNotIn(first.encode(), chunk)
        self.assertEqual(set(self.ledger._unvalidated), {first, second})

    def test_a_half_written_line_waits_for_its_newline(self):
        first = self.add("a.py")
        second = self.add("b.py", newline=False)
        self.scan()
        self.assertEqual(set(self.ledger._unvalidated), {first})
        self.append("\n")
        self.scan()
        self.assertEqual(set(self.ledger._unvalidated), {first, second})

    def test_a_file_rewritten_in_place_is_read_again(self):
        self.add("a.py")
        self.add("b.py")
        self.scan()
        entry = {"id": "edit_new", "timestamp": _iso(), "file": "c.py",
                 "summary": "longer than both earlier lines " * 20}
        self.ledger.ledger_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        self.scan()
        self.assertEqual(set(self.ledger._unvalidated), {"edit_new"})

    def test_a_rewrite_past_the_head_is_noticed(self):
        """Same first bytes, other bytes before the offset: the tail check sees it."""
        self.add("a.py", pad=800)  # the compared head lies inside this line
        second = self.add("b.py")
        self.scan()
        text = self.ledger.ledger_file.read_text(encoding="utf-8")
        self.ledger.ledger_file.write_text(text.replace(second, "edit_xxxx") + "\n",
                                           encoding="utf-8")
        self.scan()
        self.assertIn("edit_xxxx", self.ledger._unvalidated)
        self.assertNotIn(second, self.ledger._unvalidated)

    def test_a_replaced_file_is_read_again(self):
        self.add("a.py")
        self.scan()
        swap = self.ledger.ledger_file.with_name("swap.tmp")
        swap.write_text(json.dumps({"id": "edit_swapped", "timestamp": _iso(),
                                    "file": "z.py"}) + "\n", encoding="utf-8")
        os.replace(swap, self.ledger.ledger_file)
        self.scan()
        self.assertEqual(set(self.ledger._unvalidated), {"edit_swapped"})

    def test_rotation_starts_the_scan_over(self):
        self.add("old.py", ts=_iso(days=30))
        fresh = self.add("new.py")
        self.scan()
        summary = self.ledger.rotate_if_needed(1, self.root / ".c3" / "archive", keep_days=14)
        self.assertIsNotNone(summary)
        self.assertEqual(self.ledger._scan_offset, 0)
        self.scan()
        self.assertEqual(set(self.ledger._unvalidated), {fresh})

    def test_unreadable_lines_are_skipped(self):
        self.append("{not json\n[1, 2]\n")
        eid = self.add("a.py")
        self.scan()
        self.assertEqual(set(self.ledger._unvalidated), {eid})


class TestValidatePending(_LedgerCase):
    def test_checks_newest_first_in_batches(self):
        ids = [self.add(f"m{i}.py") for i in range(7)]
        cache = _Cache()
        first = self.ledger.validate_pending(batch=5, validation_cache=cache)
        self.assertEqual([r["id"] for r in first], list(reversed(ids[2:])))
        second = self.ledger.validate_pending(batch=5, validation_cache=cache)
        self.assertEqual([r["id"] for r in second], [ids[1], ids[0]])
        self.assertEqual(self.ledger.validate_pending(batch=5, validation_cache=cache), [])
        self.assertEqual(len(cache.calls), 7)
        self.assertEqual(sorted(self.patched("valid")), ids)

    def test_a_file_the_cache_never_checks_leaves_the_queue(self):
        py = self.add("a.py")
        for i in range(5):
            self.add(f"doc{i}.md")
        cache = _Cache()
        results = self.ledger.validate_pending(batch=5, validation_cache=cache)
        self.assertEqual([r["id"] for r in results], [py])
        self.assertEqual(self.ledger.validate_pending(batch=5, validation_cache=cache), [])
        self.assertEqual(cache.calls, ["a.py"])
        self.assertEqual(self.ledger._unvalidated, {})

    def test_a_deleted_file_is_not_retried(self):
        eid = self.add("gone.py")
        (self.root / "gone.py").unlink()
        cache = _Cache(answers={"gone.py": [None]})
        self.assertEqual(self.ledger.validate_pending(validation_cache=cache), [])
        self.assertEqual(self.ledger.validate_pending(validation_cache=cache), [])
        self.assertEqual(cache.calls, ["gone.py"])
        self.assertNotIn(eid, self.ledger._unvalidated)

    def test_a_busy_checker_is_retried(self):
        eid = self.add("busy.py")
        cache = _Cache(answers={"busy.py": [None, {"valid": False, "errors": ["E1"]}]})
        self.assertEqual(self.ledger.validate_pending(validation_cache=cache), [])
        results = self.ledger.validate_pending(validation_cache=cache)
        self.assertEqual([(r["id"], r["valid"], r["errors"]) for r in results],
                         [(eid, False, ["E1"])])

    def test_edits_older_than_a_day_are_not_checked(self):
        """The checker reads the file as it is now, not as that edit left it."""
        old = self.add("old.py", ts=_iso(days=2))
        new = self.add("new.py")
        cache = _Cache()
        self.assertEqual([r["id"] for r in self.ledger.validate_pending(validation_cache=cache)],
                         [new])
        self.assertEqual(cache.calls, ["new.py"])
        self.assertNotIn(old, self.ledger._unvalidated)

    def test_max_age_none_checks_every_age(self):
        old = self.add("old.py", ts=_iso(days=30))
        results = self.ledger.validate_pending(validation_cache=_Cache(), max_age_s=None)
        self.assertEqual([r["id"] for r in results], [old])

    def test_an_unparseable_timestamp_is_still_checked(self):
        eid = self.add("odd.py", ts="sometime")
        results = self.ledger.validate_pending(validation_cache=_Cache())
        self.assertEqual([r["id"] for r in results], [eid])

    def test_another_cache_rebuilds_the_queue(self):
        """An entry one cache never checks comes back for a cache that does."""
        md = self.add("notes.md")
        self.assertEqual(self.ledger.validate_pending(validation_cache=_Cache()), [])
        results = self.ledger.validate_pending(validation_cache=_Cache(exts=(".md",)))
        self.assertEqual([r["id"] for r in results], [md])

    def test_a_cache_without_eligible_is_asked_about_everything(self):
        eid = self.add("notes.md")
        cache = mock.Mock()
        del cache.eligible
        cache.validate_file.return_value = {"valid": True, "errors": []}
        results = self.ledger.validate_pending(validation_cache=cache)
        self.assertEqual([r["id"] for r in results], [eid])

    def test_a_patch_from_another_writer_settles_its_target(self):
        done = self.add("a.py")
        other = self.add("b.py")
        self.append(json.dumps({"target_id": done, "valid": True, "errors": []}) + "\n")
        results = self.ledger.validate_pending(validation_cache=_Cache())
        self.assertEqual([r["id"] for r in results], [other])

    def test_a_later_patch_settles_an_entry_already_queued(self):
        eid = self.add("a.py")
        cache = _Cache(answers={"a.py": [None]})  # busy every time
        self.ledger.validate_pending(validation_cache=cache)
        self.assertIn(eid, self.ledger._unvalidated)
        self.append(json.dumps({"target_id": eid, "valid": True, "errors": []}) + "\n")
        self.assertEqual(self.ledger.validate_pending(validation_cache=cache), [])
        self.assertEqual(cache.calls, ["a.py"])


class TestEnrichPending(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.ledger._git_root = str(self.root)
        patcher = mock.patch.object(self.ledger, "_git_combined",
                                    return_value=({"commit": "abc"}, "1 file changed"))
        self.combined = patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_entry_is_enriched_once(self):
        first = self.add("a.py", git_pending=True)
        self.add("b.py")  # logged with its git context already
        self.assertEqual(self.ledger.enrich_pending(), 1)
        self.assertEqual(self.ledger.enrich_pending(), 0)
        later = self.add("c.py", git_pending=True)
        self.assertEqual(self.ledger.enrich_pending(), 1)
        self.assertEqual([c.args[0] for c in self.combined.call_args_list], ["a.py", "c.py"])
        self.assertEqual(self.patched("git"), [first, later])

    def test_a_batch_leaves_the_rest_for_the_next_cycle(self):
        ids = [self.add(f"f{i}.py", git_pending=True) for i in range(3)]
        self.assertEqual(self.ledger.enrich_pending(batch=2), 2)
        self.assertEqual(self.ledger.enrich_pending(batch=2), 1)
        self.assertEqual(self.ledger.enrich_pending(batch=2), 0)
        self.assertEqual(self.patched("git"), ids)

    def test_a_failed_write_keeps_the_entries(self):
        self.add("a.py", git_pending=True)
        real_open = open
        ledger_file = self.ledger.ledger_file

        def refuse_appends(path, mode="r", *args, **kwargs):
            if "a" in mode and Path(path) == ledger_file:
                raise OSError("disk full")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", refuse_appends):
            self.assertEqual(self.ledger.enrich_pending(), 0)
        self.assertEqual(self.ledger.enrich_pending(), 1)

    def test_no_git_root_enriches_nothing(self):
        self.ledger._git_root = None
        self.add("a.py", git_pending=True)
        self.assertEqual(self.ledger.enrich_pending(), 0)
        self.combined.assert_not_called()


if __name__ == "__main__":
    unittest.main()
