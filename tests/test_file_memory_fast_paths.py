"""list_tracked reads each record once, BranchWatch looks dirty files up one
by one, and the FileMemory stale scan skips records in nested checkouts."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from services.agents import BranchWatchAgent, FileMemoryAgent
from services.file_memory import FileMemoryStore
from services.notifications import NotificationStore
from services.watcher import _ChangeHandler


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestListTracked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root, "a.py", "def a():\n    return 1\n")
        _write(self.root, "pkg/b.py", "def b():\n    return 2\n")
        self.fm = FileMemoryStore(str(self.root))
        self.fm.update("a.py")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_and_dropped_records_show_up(self):
        self.assertEqual(self.fm.list_tracked(), ["a.py"])
        self.fm.update("pkg/b.py")
        self.assertEqual(sorted(self.fm.list_tracked()), ["a.py", "pkg/b.py"])
        self.fm.drop("a.py")
        self.assertEqual(self.fm.list_tracked(), ["pkg/b.py"])

    def test_a_record_already_listed_is_not_read_again(self):
        self.fm.list_tracked()
        store_file = self.fm._store_path("a.py")
        record = json.loads(store_file.read_text(encoding="utf-8"))
        record["path"] = "changed.py"
        store_file.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.fm.list_tracked(), ["a.py"])

    def test_unreadable_record_is_skipped_and_retried(self):
        broken = self.fm.store_dir / ("0" * 32 + ".json")
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.fm.list_tracked(), ["a.py"])
        broken.write_text(json.dumps({"path": "late.py"}), encoding="utf-8")
        self.assertEqual(sorted(self.fm.list_tracked()), ["a.py", "late.py"])

    def test_is_tracked_is_slash_agnostic(self):
        self.fm.update("pkg/b.py")
        self.assertTrue(self.fm.is_tracked("pkg/b.py"))
        self.assertTrue(self.fm.is_tracked("pkg\\b.py"))
        self.assertFalse(self.fm.is_tracked("pkg/missing.py"))


class TestAgentsSkipFullScans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".c3").mkdir()
        _write(self.root, "main.py", "def m():\n    return 1\n")
        # A linked worktree: a directory whose .git is a FILE.
        _write(self.root, "wt/copy.py", "def c():\n    return 1\n")
        _write(self.root, "wt/.git", "gitdir: elsewhere/.git/worktrees/wt\n")
        self.fm = FileMemoryStore(str(self.root))
        self.fm.update("main.py")
        self.fm.update("wt/copy.py")
        self.notifs = NotificationStore(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stale_scan_skips_records_in_nested_checkouts(self):
        handler = _ChangeHandler(root=str(self.root))
        agent = FileMemoryAgent(self.fm, SimpleNamespace(_handler=handler),
                                self.notifs, enabled=False)
        _write(self.root, "main.py", "def m():\n    return 2\n")
        _write(self.root, "wt/copy.py", "def c():\n    return 2\n")
        handler._changes.append({"type": "modified", "path": str(self.root / "main.py")})
        updated = []
        real_update = self.fm.update
        self.fm.update = lambda rel, *a, **k: (updated.append(rel), real_update(rel))[1]
        agent.check()
        self.assertEqual(updated, ["main.py"])

    def test_branch_watch_looks_up_dirty_files_without_listing_the_store(self):
        agent = BranchWatchAgent(self.fm, self.notifs, str(self.root), enabled=False)

        def _no_full_scan():
            raise AssertionError("list_tracked must not run for a dirty-file lookup")

        self.fm.list_tracked = _no_full_scan
        _write(self.root, "main.py", "def m():\n    return 3\n")
        queued = agent._queue_changed(["main.py", "not_tracked.py"])
        self.assertEqual(queued, 1)
        self.assertIn("main.py", self.fm.drain_queue())


if __name__ == "__main__":
    unittest.main()
