"""Edit-ledger hook regressions.

Covers two correctness fixes in cli/hook_edit_ledger.py + cli/_hook_utils.py:
  #3  NotebookEdit path (`notebook_path`) is extracted, so notebook edits get logged.
  #4  Generated edit ids carry a random suffix to avoid same-second collisions
      between the hook process and the server (services/edit_ledger.py).
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

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


if __name__ == "__main__":
    unittest.main()
