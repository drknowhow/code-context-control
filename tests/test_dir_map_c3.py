"""C3 of the c3_compress remediation: c3_read maps directories too.

The one thing c3_read could not do that c3_compress could was a batch,
and neither could map a directory. Now `c3_read('<dir>')` renders one
line per file under a token budget, recently edited files first, then the
ones with the most structure. These tests pin the shape, the ranking, the
budget and the traversal bounds (docs/file-map.md § Directories), plus the
instruction surfaces that now teach the c3_read(file_path) form.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools.read import handle_read  # noqa: E402
from core import count_tokens  # noqa: E402
from services import dir_map  # noqa: E402
from services.file_memory import FileMemoryStore  # noqa: E402
from services.session_manager import SessionManager  # noqa: E402


def _py(n_funcs: int, prefix: str) -> str:
    parts = ["import os\n\n"]
    if n_funcs:
        parts.append(f"class {prefix.title()}Thing:\n    def run(self):\n        return 1\n\n")
    for i in range(n_funcs):
        parts.append(f"def {prefix}_{i}(x):\n    return x + {i}\n\n")
    return "".join(parts)


class _Ledger:
    def __init__(self, files):
        self._files = files

    def get_history(self, limit=50):
        return [{"file": f} for f in self._files]


def _svc(tmp, ledger=None, session=False):
    sm = None
    if session:
        sm = SessionManager(tmp)
        sm.start_session("t")
    return SimpleNamespace(project_path=tmp, file_memory=FileMemoryStore(tmp),
                           edit_ledger=ledger, session_mgr=sm, hybrid_config={})


def _fin(sm):
    def finalize(name, args, resp, summ, **kw):
        sm.log_tool_call(name, args, summ)
        sm.track_response(name, resp, kw.get("response_tokens", 0))
        return resp
    return finalize


class TestDirectoryMap(unittest.TestCase):
    def _tree(self, tmp):
        root = Path(tmp)
        (root / "pkg").mkdir()
        (root / "pkg" / "big.py").write_text(_py(8, "big"), encoding="utf-8")
        (root / "pkg" / "small.py").write_text(_py(1, "small"), encoding="utf-8")
        (root / "pkg" / "notes.md").write_text("# Notes\n\n## Later\n\ntext\n", encoding="utf-8")
        (root / "pkg" / "data.txt").write_text("a\nb\nc\n", encoding="utf-8")
        (root / "pkg" / "node_modules").mkdir()
        (root / "pkg" / "node_modules" / "dep.js").write_text("module.exports = 1;\n", encoding="utf-8")
        return root

    def test_shape_and_ranking_by_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            out = handle_read("pkg", None, None, True, _svc(tmp), None)
            lines = out.splitlines()
            self.assertEqual(lines[0], "# pkg/ (4 files, 47L)")
            self.assertTrue(lines[1].startswith("big.py (30L python) — BigThing; big_0; big_1; big_2; big_3; big_4"))
            self.assertIn("small.py (9L python) — SmallThing; small_0", lines)
            self.assertIn("notes.md (5L markdown) — Notes; Later", lines)
            self.assertIn("data.txt (3L txt)", lines)
            self.assertNotIn("dep.js", out)
            self.assertTrue(lines[-1].startswith("[dir_map] recently edited first"))

    def test_recent_edits_rank_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            svc = _svc(tmp, ledger=_Ledger(["pkg/small.py", "pkg/data.txt"]))
            lines = handle_read("pkg", None, None, True, svc, None).splitlines()
            self.assertTrue(lines[1].startswith("small.py"))
            self.assertTrue(lines[2].startswith("data.txt"))
            self.assertTrue(lines[3].startswith("big.py"))

    def test_budget_drops_from_the_end_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            out = handle_read("pkg", None, 30, True, _svc(tmp), None)
            self.assertLessEqual(count_tokens("\n".join(out.splitlines()[:-1])),
                                 30 + count_tokens("… 3 more files"))
            self.assertIn("… ", out)
            self.assertIn("more files", out)

    def test_traversal_cap_and_no_symlink_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            link = root / "pkg" / "loop"
            try:
                link.symlink_to(root / "pkg", target_is_directory=True)
                linked = True
            except (OSError, NotImplementedError):
                linked = False
            old = dir_map.MAX_FILES
            dir_map.MAX_FILES = 2
            try:
                out = handle_read("pkg", None, None, True, _svc(tmp), None)
            finally:
                dir_map.MAX_FILES = old
            self.assertIn("# pkg/ (2+ files", out)
            self.assertIn("traversal capped at 2 files", out)
            if linked:
                self.assertNotIn("loop/", out)

    def test_root_directory_and_telemetry_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            svc = _svc(tmp, session=True)
            out = handle_read(".", None, None, True, svc, _fin(svc.session_mgr))
            self.assertTrue(out.startswith("# ./ ("))
            self.assertIn("pkg/big.py (30L python)", out)
            rows = [json.loads(line) for line in
                    (Path(tmp) / ".c3" / "tool_telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
            detail = rows[-1]["detail"]
            self.assertEqual(detail["backend"], "directory")
            self.assertEqual(detail["files"], 4)
            self.assertEqual(detail["listed"], 4)
            self.assertGreaterEqual(detail["extracted"], 2)
            self.assertFalse(detail["capped"])

    def test_extraction_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "many").mkdir()
            for i in range(6):
                (root / "many" / f"m{i}.py").write_text(_py(1, f"m{i}"), encoding="utf-8")
            old = dir_map.MAX_EXTRACT_FILES
            dir_map.MAX_EXTRACT_FILES = 2
            try:
                text, detail = dir_map.render_directory_map(_svc(tmp), "many")
            finally:
                dir_map.MAX_EXTRACT_FILES = old
            self.assertEqual(detail["extracted"], 2)
            self.assertEqual(detail["files"], 6)
            # Unparsed files still show a line count.
            self.assertEqual(sum(1 for line in text.splitlines() if "(9L python)" in line), 6)


class TestInstructionSurfaces(unittest.TestCase):
    def test_managed_block_teaches_read_first(self):
        from services import claude_md
        text = claude_md.C3_WORKFLOW if hasattr(claude_md, "C3_WORKFLOW") else ""
        src = Path(claude_md.__file__).read_text(encoding="utf-8")
        self.assertIn("`c3_read(file_path)` with no symbols/lines returns the file map", src)
        self.assertNotIn("`c3_compress(mode='map')` then `c3_read(symbols=...|lines=...)`", src)
        self.assertIn("c3_read(file_path) maps a file (or directory)", src)
        del text

    def test_hook_redirect_names_c3_read(self):
        from cli import hook_pretool_enforce as hook
        self.assertIn("c3_read(file_path='...') to map the file first", hook._REDIRECTS["Read"])
        self.assertNotIn("c3_compress", hook._REDIRECTS["Read"])


if __name__ == "__main__":
    unittest.main()
