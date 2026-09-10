"""
The file watcher filters events with the index scanner's pruning.

A change counts toward IndexStalenessAgent's rebuild threshold only when the
file could be in the index. Before 2.129.1 the watcher had its own shorter
skip list and ignored the root .gitignore and nested checkouts, so a daemon
rewriting state JSON under a gitignored ``logs/`` every few seconds tripped
a full refresh every minute (75,968 "Index auto-rebuilt" notifications on
one project) and each refresh burst left the MCP server's process heap with
pinned 16 MB segments it never released — 28 GB of commit per server.
"""
import tempfile
import unittest
from pathlib import Path

from services.watcher import SKIP_DIRS, _ChangeHandler


class _Tree(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()

    def tearDown(self):
        self._td.cleanup()

    def touch(self, rel, content="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def handler(self, excluder=None):
        return _ChangeHandler(excluder, root=str(self.root))

    @staticmethod
    def recorded(h):
        return [Path(c["path"]).name for c in h.get_and_clear()]


class TestGitignoredDirectoriesDoNotCount(_Tree):
    def test_a_state_file_under_a_gitignored_dir_is_not_a_change(self):
        self.touch(".gitignore", "logs/\n.daemon-worktree/\n")
        h = self.handler()
        h._record("modified", str(self.touch("logs/.health.json", "{}")))
        h._record("modified", str(self.touch(
            ".daemon-worktree/logs/restart_failure_state.json", "{}")))
        h._record("modified", str(self.touch("src/real.py")))
        self.assertEqual(self.recorded(h), ["real.py"])

    def test_a_gitignore_glob_entry_prunes_too(self):
        self.touch(".gitignore", "*.egg-info/\n")
        h = self.handler()
        h._record("created", str(self.touch("c3.egg-info/PKG-INFO.md")))
        h._record("created", str(self.touch("pkg/mod.py")))
        self.assertEqual(self.recorded(h), ["mod.py"])

    def test_editing_the_root_gitignore_reloads_the_pruner(self):
        h = self.handler()
        h._record("modified", str(self.touch("logs/state.json", "{}")))
        self.assertEqual(self.recorded(h), ["state.json"])
        gi = self.touch(".gitignore", "logs/\n")
        h._record("modified", str(gi))  # the watcher sees the edit
        h._record("modified", str(self.touch("logs/state.json", "{}")))
        self.assertEqual(self.recorded(h), [])


class TestNestedCheckoutsDoNotCount(_Tree):
    def test_a_linked_worktree_copy_is_not_a_change(self):
        wt = self.root / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n",
                                 encoding="utf-8")
        h = self.handler()
        h._record("modified", str(self.touch("wt/src/copy.py")))
        h._record("modified", str(self.touch("src/real.py")))
        self.assertEqual(self.recorded(h), ["real.py"])

    def test_the_project_root_itself_is_never_treated_as_nested(self):
        (self.root / ".git").mkdir()
        h = self.handler()
        h._record("modified", str(self.touch("src/real.py")))
        self.assertEqual(self.recorded(h), ["real.py"])


class TestSkipListIsTheScannersSuperset(_Tree):
    def test_scanner_only_dirs_are_skipped_by_the_watcher(self):
        # 'target' and '.mypy_cache' were never in the watcher's own list.
        self.assertIn("target", SKIP_DIRS)
        h = self.handler()
        h._record("modified", str(self.touch("target/debug/build.rs")))
        h._record("modified", str(self.touch(".mypy_cache/3.12/x.json", "{}")))
        h._record("modified", str(self.touch("node_modules/a/index.js")))
        h._record("modified", str(self.touch("src/lib.rs")))
        self.assertEqual(self.recorded(h), ["lib.rs"])

    def test_extension_allowlist_still_applies(self):
        h = self.handler()
        h._record("modified", str(self.touch("src/blob.bin")))
        h._record("modified", str(self.touch("src/ok.py")))
        self.assertEqual(self.recorded(h), ["ok.py"])

    def test_sub_project_excluder_still_applies(self):
        h = self.handler(excluder=lambda p: "sub" in Path(p).parts)
        h._record("modified", str(self.touch("sub/inner.py")))
        h._record("modified", str(self.touch("src/outer.py")))
        self.assertEqual(self.recorded(h), ["outer.py"])

    def test_a_rootless_handler_keeps_the_legacy_behaviour(self):
        h = _ChangeHandler()
        h._record("modified", str(self.touch("node_modules/x/y.js")))
        h._record("modified", str(self.touch("src/z.js")))
        self.assertEqual(self.recorded(h), ["z.js"])


if __name__ == "__main__":
    unittest.main()
