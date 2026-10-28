"""Edit-ledger hook regressions.

Covers two correctness fixes in cli/hook_edit_ledger.py + cli/_hook_utils.py:
  #3  NotebookEdit path (`notebook_path`) is extracted, so notebook edits get logged.
  #4  Generated edit ids carry a random suffix to avoid same-second collisions
      between the hook process and the server (services/edit_ledger.py).
and the ledger scans behind each entry's id and version: the tail is read
backwards in chunks, and a tail miss parses only the lines naming the file.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import hook_edit_ledger as hook  # noqa: E402
from cli._hook_utils import get_tool_input_path  # noqa: E402


def _run_ledger_hook(project: Path, tool_name: str, tool_input: dict) -> str:
    """Spawn the real ledger hook against a temp project; return ledger text."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli" / "hook_edit_ledger.py")],
        input=payload, capture_output=True, text=True,
        encoding="utf-8", cwd=str(project), timeout=20,
    )
    ledger = project / ".c3" / "edit_ledger.jsonl"
    return ledger.read_text(encoding="utf-8") if ledger.exists() else ""


class TestGetToolInputPath(unittest.TestCase):
    def test_notebook_path_extracted(self):
        # Fix #3: NotebookEdit uses notebook_path, not file_path.
        path = get_tool_input_path({"tool_input": {"notebook_path": "nb.ipynb"}})
        self.assertEqual(path, "nb.ipynb")

    def test_file_path_still_wins(self):
        path = get_tool_input_path(
            {"tool_input": {"file_path": "a.py", "notebook_path": "b.ipynb"}}
        )
        self.assertEqual(path, "a.py")


class TestEditLedgerHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notebook_edit_is_logged(self):
        # Fix #3: a notebook edit must produce a ledger entry.
        (self.tmp / "nb.ipynb").write_text("{}", encoding="utf-8")
        text = _run_ledger_hook(
            self.tmp, "NotebookEdit",
            {"notebook_path": str(self.tmp / "nb.ipynb")},
        )
        self.assertTrue(text.strip(), "NotebookEdit produced no ledger entry")
        entry = json.loads(text.strip().splitlines()[-1])
        self.assertEqual(entry["file"], "nb.ipynb")

    def test_edit_id_has_random_suffix(self):
        # Fix #4: ids look like edit_<ts>_<seq>_<4hex> and are unique even when
        # two entries land in the same second.
        (self.tmp / "foo.py").write_text("x = 1\n", encoding="utf-8")
        ids = []
        for _ in range(3):
            text = _run_ledger_hook(
                self.tmp, "Edit",
                {"file_path": str(self.tmp / "foo.py"),
                 "old_string": "x", "new_string": "y"},
            )
            ids.append(json.loads(text.strip().splitlines()[-1])["id"])
        for eid in ids:
            parts = eid.split("_")
            # edit, YYYYMMDD, HHMMSS, seq, hex4
            self.assertEqual(len(parts), 5, f"unexpected id shape: {eid}")
            self.assertEqual(len(parts[-1]), 4, f"missing 4-hex suffix: {eid}")
            int(parts[-1], 16)  # suffix is valid hex
        self.assertEqual(len(set(ids)), len(ids), "edit ids collided")


class TestLedgerScans(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = self.tmp / "edit_ledger.jsonl"
        # Enough later entries that the tail scan cannot see the first ones.
        self.filler = [{"file": f"f{i}.py", "version": "v1"}
                       for i in range(hook._TAIL_SCAN + 10)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, lines):
        with open(self.ledger, "w", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")

    def test_tail_lines_matches_a_full_read(self):
        # Blank lines, CRLF endings, a line longer than the chunk, no final newline.
        data = (b'{"a": 1}\r\n\r\n{"b": 2}\n{"long": "' + b"x" * 40 + b'"}\n'
                b'\n{"c": 3}\r\n{"d": 4}')
        self.ledger.write_bytes(data)
        expected = [ln.decode() for ln in data.split(b"\n") if ln.strip()]
        for chunk in (1, 3, 7, 64, 1 << 16):
            with mock.patch.object(hook, "_TAIL_CHUNK", chunk):
                for n in range(len(expected) + 2):
                    self.assertEqual(hook._tail_lines(self.ledger, n),
                                     expected[-n:] if n else [], f"chunk={chunk} n={n}")

    def test_tail_lines_of_a_missing_ledger(self):
        self.assertEqual(hook._tail_lines(self.tmp / "absent.jsonl", 5), [])

    def test_version_found_beyond_the_tail(self):
        self._write([{"file": "old.py", "version": "v3"}] + self.filler)
        self.assertEqual(hook._next_version(self.ledger, "old.py"), "v4")
        self.assertEqual(hook._next_version(self.ledger, "never.py"), "v1")
        self.assertEqual(hook._next_version(self.tmp / "absent.jsonl", "old.py"), "v1")

    def test_version_scan_counts_only_the_file_field(self):
        self._write([
            {"file": "pkg/foo.py", "version": "v9"},
            {"file": "bar.py", "version": "v8", "renamed_from": "foo.py"},
            '["foo.py", 2]',
            'not json "foo.py"',
            {"file": "foo.py", "version": "v2"},
        ] + self.filler)
        self.assertEqual(hook._next_version(self.ledger, "foo.py"), "v3")

    def test_version_scan_finds_both_path_encodings(self):
        name = "docs/übersicht.md"
        escaped = {"file": name, "version": "v6"}
        raw = json.dumps({"file": name, "version": "v4"}, ensure_ascii=False)
        self._write([escaped, raw] + self.filler)
        self.assertEqual(hook._next_version(self.ledger, name), "v7")
        escaped["version"] = "v4"
        raw = json.dumps({"file": name, "version": "v6"}, ensure_ascii=False)
        self._write([escaped, raw] + self.filler)
        self.assertEqual(hook._next_version(self.ledger, name), "v7")


if __name__ == "__main__":
    unittest.main()
