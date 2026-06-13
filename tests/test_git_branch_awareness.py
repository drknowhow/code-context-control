"""Branch-awareness coverage: GitContext, BranchWatchAgent, ledger/session
stamping, and snapshot branch-change warnings — exercised against a real
temporary git repository.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from services.agents import BranchWatchAgent
from services.context_snapshot import ContextSnapshot
from services.edit_ledger import EditLedger
from services.file_memory import FileMemoryStore
from services.git_context import GitContext
from services.notifications import NotificationStore


def _git(cwd, *args):
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True, **kwargs,
    )


class _StubSession:
    def __init__(self):
        self.current_session = {
            "id": "sess-1",
            "decisions": [],
            "files_touched": [{"file": "mod.py", "type": "code", "summary": "x"}],
            "context_notes": [],
            "context_budget": {"response_tokens": 1, "call_count": 1},
        }


class _StubMemory:
    facts = []

    def recall(self, *args, **kwargs):
        return []


@unittest.skipUnless(shutil.which("git"), "git not available")
class TestBranchAwareness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir(exist_ok=True)
        _git(self.project, "init")
        _git(self.project, "config", "user.email", "t@example.com")
        _git(self.project, "config", "user.name", "Test")
        _git(self.project, "config", "commit.gpgsign", "false")
        (self.project / "mod.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-m", "init")
        _git(self.project, "branch", "-M", "main")

    def tearDown(self):
        self.tmp.cleanup()

    # ── Tier 0: GitContext ────────────────────────────────────────────
    def test_state_reports_branch_and_dirty(self):
        g = GitContext(self.project)
        st = g.state()
        self.assertTrue(st["available"])
        self.assertEqual(st["branch"], "main")
        self.assertTrue(st["head_sha"])
        self.assertFalse(st["dirty"])
        self.assertFalse(st["detached"])

        (self.project / "mod.py").write_text("def a():\n    return 2\n", encoding="utf-8")
        st2 = g.state(force=True)
        self.assertTrue(st2["dirty"])
        self.assertIn("mod.py", g.dirty_files())

    def test_changed_files_between_commits(self):
        g = GitContext(self.project)
        old = g.head_sha()
        _git(self.project, "checkout", "-b", "feature")
        (self.project / "mod.py").write_text("def a():\n    return 99\n", encoding="utf-8")
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-m", "change")
        new = g.state(force=True)["head_sha"]
        self.assertNotEqual(old, new)
        self.assertEqual(g.branch(), "feature")
        self.assertIn("mod.py", g.changed_files(old, new))

    def test_detached_head_does_not_crash(self):
        g = GitContext(self.project)
        head = g.head_sha()
        _git(self.project, "checkout", head)
        st = g.state(force=True)
        self.assertTrue(st["available"])
        self.assertTrue(st["detached"])
        self.assertIsNone(st["branch"])

    # ── Tier 2/3: BranchWatchAgent ────────────────────────────────────
    def test_branch_switch_queues_scoped_reindex(self):
        fm = FileMemoryStore(str(self.project))
        fm.update("mod.py")
        notifs = NotificationStore(str(self.project))
        agent = BranchWatchAgent(fm, notifs, str(self.project), enabled=False)

        agent.check()  # baseline — records HEAD, no notification
        self.assertEqual([n["title"] for n in notifs.get_history()], [])

        _git(self.project, "checkout", "-b", "feature")
        (self.project / "mod.py").write_text("def a():\n    return 99\n", encoding="utf-8")
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-m", "change")

        agent.check()  # detect switch
        self.assertIn("Branch changed", [n["title"] for n in notifs.get_history()])
        self.assertIn("mod.py", fm.drain_queue())

    def test_fetch_like_noop_does_not_notify(self):
        fm = FileMemoryStore(str(self.project))
        fm.update("mod.py")
        notifs = NotificationStore(str(self.project))
        agent = BranchWatchAgent(fm, notifs, str(self.project), enabled=False)
        agent.check()   # baseline
        agent.check()   # no ref movement, clean tree
        self.assertEqual([n["title"] for n in notifs.get_history()], [])

    def test_dirty_file_fast_path_queues_outside_edit(self):
        fm = FileMemoryStore(str(self.project))
        fm.update("mod.py")
        notifs = NotificationStore(str(self.project))
        agent = BranchWatchAgent(fm, notifs, str(self.project), enabled=False)
        agent.check()  # baseline
        # Edit made "outside" C3 (no ledger/queue), HEAD unchanged.
        (self.project / "mod.py").write_text("def a():\n    return 7\n", encoding="utf-8")
        agent.check()
        self.assertIn("mod.py", fm.drain_queue())

    # ── Tier 1: ledger stamping + filter ──────────────────────────────
    def test_ledger_stamps_branch_and_filters(self):
        ledger = EditLedger(str(self.project))
        entry = ledger.log_edit("mod.py", "modified", "tweak")
        self.assertEqual(entry["git"]["branch"], "main")
        self.assertTrue(entry["git"]["head_sha"])

        hist = ledger.get_history(branch="main")
        self.assertTrue(any(e["id"] == entry["id"] for e in hist))
        self.assertEqual(ledger.get_history(branch="does-not-exist"), [])

    # ── Tier 1: snapshot branch warning ───────────────────────────────
    def test_snapshot_warns_on_branch_change(self):
        snap = ContextSnapshot(str(self.project))
        snap.capture(_StubSession(), _StubMemory(), task_description="task")
        _git(self.project, "checkout", "-b", "feature")
        restored = snap.restore("latest")
        self.assertIn("Branch changed", restored["briefing"])


if __name__ == "__main__":
    unittest.main()
