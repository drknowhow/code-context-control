"""c3_edit refuses an edit whose target changed since this session read it."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools.edit import handle_edit  # noqa: E402
from cli.tools.read import handle_read  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import read_stamps  # noqa: E402
from services.file_memory import FileMemoryStore  # noqa: E402

STALE = "[c3_edit:stale]"


def _finalize(name, args, resp, summ="", **kw):
    return resp


def _lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


class StaleGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._home = patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.svc = SimpleNamespace(
            project_path=str(self.proj), file_memory=FileMemoryStore(str(self.proj)),
            edit_ledger=None, activity_log=None, session_mgr=None, memory=None,
            hybrid_config={})
        self.f = self.proj / "a.txt"
        self.f.write_text(_lines(40), encoding="utf-8")
        read_stamps._stamps.clear()
        read_stamps._total_chars = 0

    def tearDown(self):
        self._home.stop()
        read_stamps._stamps.clear()
        read_stamps._total_chars = 0
        self._tmp.cleanup()

    def _read(self):
        return handle_read(str(self.f), None, [1, 5], True, self.svc, _finalize)

    def _edit(self, old, new, edits=""):
        return handle_edit(str(self.f), old, new, "", "", False, self.svc,
                           _finalize, edits)

    def _external(self, old, new):
        text = self.f.read_text(encoding="utf-8")
        self.f.write_text(text.replace(old, new), encoding="utf-8")

    def _set_mode(self, value):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"edit": {"stale_guard": value}}), encoding="utf-8")

    def test_crlf_old_string_is_still_checked(self):
        self.f.write_bytes(_lines(40).replace("\n", "\r\n").encode("utf-8"))
        self._read()
        self.f.write_bytes(self.f.read_bytes().replace(b"line 11\r\n", b"line 11 X\r\n"))
        out = self._edit("line 10\r\n", "line ten\r\n")
        self.assertIn(STALE, out)

    def test_overlapping_external_change_refuses_with_current_region(self):
        self._read()
        self._external("line 11\n", "line 11 CHANGED\n")
        out = self._edit("line 10\n", "line ten\n")
        self.assertIn(STALE, out)
        self.assertIn("L11", out)
        self.assertIn("line 11 CHANGED", out)
        self.assertIn("line 10\n", self.f.read_text(encoding="utf-8"))

    def test_retry_after_refusal_succeeds(self):
        self._read()
        self._external("line 11\n", "line 11 CHANGED\n")
        self.assertIn(STALE, self._edit("line 10\n", "line ten\n"))
        out = self._edit("line 10\n", "line ten\n")
        self.assertNotIn("stale", out)
        self.assertIn("line ten\nline 11 CHANGED\n", self.f.read_text(encoding="utf-8"))

    def test_distant_external_change_applies_with_note(self):
        self._read()
        self._external("line 35\n", "line 35 CHANGED\n")
        out = self._edit("line 5\n", "line five\n")
        self.assertNotIn(STALE, out)
        self.assertIn("file changed outside this session", out)
        self.assertIn("L35", out)
        self.assertIn("region untouched", out)
        self.assertIn("line five\n", self.f.read_text(encoding="utf-8"))

    def test_change_just_past_near_window_is_not_overlap(self):
        self._read()
        self._external("line 14\n", "line 14 CHANGED\n")
        self.assertNotIn(STALE, self._edit("line 10\n", "line ten\n"))

    def test_change_at_near_window_edge_is_overlap(self):
        self._read()
        self._external("line 13\n", "line 13 CHANGED\n")
        self.assertIn(STALE, self._edit("line 10\n", "line ten\n"))

    def test_own_consecutive_edits_never_trip(self):
        self._read()
        for i in range(10, 16):
            out = self._edit(f"line {i}\n", f"line {i} x\n")
            self.assertNotIn("stale", out)
            self.assertNotIn("changed outside", out)

    def test_no_prior_read_means_no_check(self):
        self._external("line 11\n", "line 11 CHANGED\n")
        out = self._edit("line 10\n", "line ten\n")
        self.assertNotIn("stale", out)
        self.assertNotIn("changed outside", out)

    def test_batch_mode_refuses_when_any_patch_overlaps(self):
        self._read()
        self._external("line 30\n", "line 30 CHANGED\n")
        edits = json.dumps([{"old_string": "line 2\n", "new_string": "two\n"},
                            {"old_string": "line 29\n", "new_string": "x\n"}])
        out = self._edit("", "", edits)
        self.assertIn(STALE, out)
        self.assertIn("line 2\n", self.f.read_text(encoding="utf-8"))

    def test_warn_mode_applies_overlapping_edit_with_warning(self):
        self._set_mode("warn")
        self._read()
        self._external("line 11\n", "line 11 CHANGED\n")
        out = self._edit("line 10\n", "line ten\n")
        self.assertIn(STALE, out)
        self.assertIn("edit applied", out)
        self.assertIn("line ten\n", self.f.read_text(encoding="utf-8"))

    def test_off_mode_skips_the_check(self):
        self._set_mode("off")
        self._read()
        self._external("line 11\n", "line 11 CHANGED\n")
        out = self._edit("line 10\n", "line ten\n")
        self.assertNotIn("stale", out)
        self.assertNotIn("changed outside", out)

    def test_oversize_file_is_hash_only_and_warns(self):
        with patch.object(read_stamps, "MAX_FILE_CHARS", 10):
            self._read()
            self.assertIsNone(read_stamps.get(self.f).text)
            self._external("line 11\n", "line 11 CHANGED\n")
            out = self._edit("line 10\n", "line ten\n")
        self.assertIn("too large to localize", out)
        self.assertIn("line ten\n", self.f.read_text(encoding="utf-8"))

    def test_failed_edit_does_not_show_change_note(self):
        self._read()
        self._external("line 35\n", "line 35 CHANGED\n")
        out = self._edit("no such text\n", "x\n")
        self.assertNotIn("changed outside", out)

    def test_path_spelling_case_shares_stamp_on_windows(self):
        read_stamps.record(self.f)
        other = Path(str(self.f).upper()) if sys.platform == "win32" else self.f
        self.assertIsNotNone(read_stamps.get(other))


class LruTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        read_stamps._stamps.clear()
        read_stamps._total_chars = 0

    def tearDown(self):
        read_stamps._stamps.clear()
        read_stamps._total_chars = 0
        self._tmp.cleanup()

    def test_evicts_least_recent_past_file_limit(self):
        paths = []
        with patch.object(read_stamps, "MAX_FILES", 3):
            for i in range(4):
                p = self.dir / f"f{i}.txt"
                p.write_text("x\n", encoding="utf-8")
                read_stamps.record(p)
                paths.append(p)
        self.assertIsNone(read_stamps.get(paths[0]))
        self.assertIsNotNone(read_stamps.get(paths[3]))

    def test_evicts_past_total_size_limit(self):
        with patch.object(read_stamps, "MAX_TOTAL_CHARS", 25):
            for i in range(3):
                p = self.dir / f"f{i}.txt"
                p.write_text("y" * 10, encoding="utf-8")
                read_stamps.record(p)
        self.assertLessEqual(read_stamps._total_chars, 25)
        self.assertEqual(len(read_stamps._stamps), 2)


class ChangedRangesTests(unittest.TestCase):
    def test_reports_ranges_in_current_coordinates(self):
        before = "a\nb\nc\nd\n"
        after = "a\nNEW\nNEW2\nb\nc\nd\n"
        self.assertEqual(read_stamps.changed_ranges(before, after), [(2, 3)])

    def test_pure_deletion_names_lines_either_side(self):
        self.assertEqual(read_stamps.changed_ranges("a\nb\nc\n", "a\nc\n"), [(1, 2)])


if __name__ == "__main__":
    unittest.main()
