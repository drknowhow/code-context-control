"""c3_read outside the project root — served, not refused.

Before this, an outside path raised ValueError out of `relative_to` and the
whole tool call died with "is not in the subpath of". That was never a
security control: Access Guard already rules on the ABSOLUTE canonical path
(Rule.matches tests `canon` as well as `rel`), and the PreToolUse hook
deliberately stands down outside the root, so native Read went there
unimpeded. The crash only pushed the agent off the budgeted, guarded tool.

What the root still decides is INDEXING. file_memory is keyed by
project-relative path and feeds the text index, the map cache and
prune_stale, so an outside file is served from a transient record and must
never land in this project's store. These tests pin both halves.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli.tools._helpers import project_key  # noqa: E402
from cli.tools.compress import handle_compress  # noqa: E402
from cli.tools.read import handle_read  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services.file_memory import FileMemoryStore  # noqa: E402

BANNER = "[c3-read:external]"
DENIED = "[c3-access:denied]"


def _finalize(name, args, resp, summ="", **kw):
    return resp


def _py(n_funcs: int, prefix: str) -> str:
    parts = [f"class {prefix.title()}Thing:\n    def run(self):\n        return 1\n\n"]
    for i in range(n_funcs):
        parts.append(f"def {prefix}_{i}(x):\n    return x + {i}\n\n")
    return "".join(parts)


class _Base(unittest.TestCase):
    """A project and a sibling directory that is NOT inside it."""

    def setUp(self):
        self._proj_tmp = tempfile.TemporaryDirectory()
        self._out_tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._proj_tmp.name)
        self.outside = Path(self._out_tmp.name)
        (self.proj / ".c3").mkdir()
        # Isolate from the developer's real ~/.c3 rules — an outside path is
        # exactly what a personal global rule is most likely to cover.
        self._home = patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.svc = SimpleNamespace(
            project_path=str(self.proj), file_memory=FileMemoryStore(str(self.proj)),
            edit_ledger=None, session_mgr=None, memory=None, hybrid_config={})

    def tearDown(self):
        self._home.stop()
        self._proj_tmp.cleanup()
        self._out_tmp.cleanup()

    def _read(self, path, symbols=None, lines=None):
        return handle_read(str(path), symbols, lines, True, self.svc, _finalize)

    def _indexed(self):
        """Records actually persisted in the project's file_memory store."""
        store = self.proj / ".c3" / "file_memory"
        return sorted(p.name for p in store.glob("*.json")
                      if not p.name.startswith("_"))


class TestOutsideFileIsServed(_Base):
    def test_source_read_by_lines(self):
        target = self.outside / "lib.py"
        target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
        out = self._read(target, lines=[2, 3])
        self.assertIn("beta = 2", out)
        self.assertIn("gamma = 3", out)
        self.assertNotIn("alpha = 1", out)

    def test_map_lists_symbols(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        out = self._read(target)
        self.assertIn("OutThing", out)
        self.assertIn("out_0", out)
        self.assertIn("[map only", out)

    def test_symbol_read_returns_that_symbol(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        out = self._read(target, symbols=["out_7"])
        self.assertIn("def out_7(x):", out)
        self.assertNotIn("def out_3(x):", out)

    def test_unknown_symbol_falls_back_to_the_map(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        out = self._read(target, symbols=["nope_not_here"])
        self.assertIn("symbols not found", out)
        self.assertIn("out_0", out)

    def test_directory_map(self):
        (self.outside / "a.py").write_text(_py(4, "aa"), encoding="utf-8")
        (self.outside / "b.py").write_text(_py(4, "bb"), encoding="utf-8")
        out = self._read(self.outside)
        self.assertIn("a.py", out)
        self.assertIn("b.py", out)
        self.assertIn("AaThing", out)

    def test_missing_outside_file_still_reports_not_found(self):
        out = self._read(self.outside / "nope.py")
        self.assertIn("File not found", out)

    def test_batch_mixes_inside_and_outside(self):
        (self.proj / "inside.py").write_text(_py(3, "ins"), encoding="utf-8")
        (self.outside / "out.py").write_text(_py(3, "out"), encoding="utf-8")
        out = handle_read(f"inside.py,{self.outside / 'out.py'}",
                          None, None, True, self.svc, _finalize)
        self.assertIn("InsThing", out)
        self.assertIn("OutThing", out)


class TestOutsideReadIsLabelled(_Base):
    def test_banner_names_the_absolute_path(self):
        target = self.outside / "lib.py"
        target.write_text("x = 1\n", encoding="utf-8")
        out = self._read(target, lines=[1, 1])
        self.assertTrue(out.startswith(BANNER), out[:120])
        self.assertIn(target.resolve().as_posix(), out.splitlines()[0])

    def test_banner_on_map_and_directory_too(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        self.assertTrue(self._read(target).startswith(BANNER))
        self.assertTrue(self._read(self.outside).startswith(BANNER))

    def test_inside_reads_carry_no_banner(self):
        target = self.proj / "inside.py"
        target.write_text(_py(12, "ins"), encoding="utf-8")
        self.assertNotIn(BANNER, self._read(target))
        self.assertNotIn(BANNER, self._read(target, symbols=["ins_2"]))
        self.assertNotIn(BANNER, self._read(self.proj))


class TestOutsideReadNeverIndexes(_Base):
    """The whole reason outside files get a transient record."""

    def test_file_reads_write_nothing_to_the_store(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        self._read(target)
        self._read(target, symbols=["out_1"])
        self._read(target, lines=[1, 3])
        self.assertEqual(self._indexed(), [])
        self.assertEqual(self.svc.file_memory.list_tracked(), [])

    def test_directory_map_writes_nothing_to_the_store(self):
        (self.outside / "a.py").write_text(_py(6, "aa"), encoding="utf-8")
        (self.outside / "b.py").write_text(_py(6, "bb"), encoding="utf-8")
        self._read(self.outside)
        self.assertEqual(self._indexed(), [])

    def _spy_on_extraction(self):
        calls = []
        real = self.svc.file_memory.build_transient_record

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)

        self.svc.file_memory.build_transient_record = spy
        return calls

    def test_a_line_read_never_parses_the_file(self):
        """A slice must not pay for a full parse it never uses — the whole
        reason the large-file bounds exist."""
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        calls = self._spy_on_extraction()
        out = self._read(target, lines=[1, 2])
        self.assertEqual(calls, [])
        self.assertIn("OutThing", out)

    def test_a_map_read_parses_exactly_once(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        calls = self._spy_on_extraction()
        self._read(target)
        self.assertEqual(len(calls), 1)

    def test_a_symbol_read_parses_exactly_once(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        calls = self._spy_on_extraction()
        self._read(target, symbols=["out_3"])
        self.assertEqual(len(calls), 1)

    def test_inside_reads_still_index(self):
        target = self.proj / "inside.py"
        target.write_text(_py(12, "ins"), encoding="utf-8")
        self._read(target)
        self.assertEqual(len(self._indexed()), 1)
        self.assertEqual(self.svc.file_memory.list_tracked(), ["inside.py"])


class TestCompressAgrees(_Base):
    """c3_compress serves the same map; the two surfaces must not disagree
    about where a file is allowed to live — it had the identical crash."""

    def test_single_file_map(self):
        target = self.outside / "wide.py"
        target.write_text(_py(12, "out"), encoding="utf-8")
        out = handle_compress(str(target), "map", self.svc, _finalize, None)
        self.assertTrue(out.startswith(BANNER), out[:120])
        self.assertIn("out_0", out)
        self.assertEqual(self._indexed(), [])

    def test_batch_mixes_inside_and_outside(self):
        (self.proj / "inside.py").write_text(_py(12, "ins"), encoding="utf-8")
        (self.outside / "out.py").write_text(_py(12, "out"), encoding="utf-8")
        out = handle_compress(f"inside.py,{self.outside / 'out.py'}", "map",
                              self.svc, _finalize, None)
        self.assertIn("ins_0", out)
        self.assertIn("out_0", out)
        self.assertNotIn("not in the subpath", out)
        self.assertEqual(self.svc.file_memory.list_tracked(), ["inside.py"])


class TestProjectKey(unittest.TestCase):
    def test_inside_is_relative_and_not_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            key, external = project_key(root / "pkg" / "a.py", str(root))
            self.assertEqual(key, "pkg/a.py")
            self.assertFalse(external)

    def test_dotdot_escape_is_external_and_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            key, external = project_key(root / ".." / "sibling.py", str(root))
            self.assertTrue(external)
            self.assertTrue(key.endswith("/sibling.py"))
            self.assertNotIn("..", key)


class TestGuardStillRulesOutsideTheRoot(_Base):
    """Serving outside paths must not become a way around Access Guard."""

    def _mock_svc(self):
        svc = MagicMock()
        svc.project_path = str(self.proj)
        svc.hybrid_config = {}
        svc.session_mgr = None
        svc.edit_ledger = None
        svc.memory = None
        return svc

    def test_builtin_env_deny_binds_on_an_outside_path(self):
        secret = self.outside / ".env"
        secret.write_text("TOPSECRET=1\n", encoding="utf-8")
        resp = handle_read(str(secret), svc=self._mock_svc(), finalize=_finalize)
        self.assertIn(DENIED, resp)
        self.assertNotIn("TOPSECRET", resp)

    def test_project_deny_rule_binds_on_an_outside_path(self):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": {"deny": ["**/keys/**"]}}), encoding="utf-8")
        (self.outside / "keys").mkdir()
        target = self.outside / "keys" / "id.txt"
        target.write_text("TOPSECRET\n", encoding="utf-8")
        resp = handle_read(str(target), svc=self._mock_svc(), finalize=_finalize)
        self.assertIn(DENIED, resp)
        self.assertNotIn("TOPSECRET", resp)


if __name__ == "__main__":
    unittest.main()
