"""RepoMapService — byte stability, staleness, locking, truncation, boundaries."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from services.repo_map import (
    DIRTY_NAME,
    LOCK_NAME,
    MAP_NAME,
    META_NAME,
    RepoMapService,
    is_structural_change,
    mark_map_dirty,
)


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_app.py").write_text("pass\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Demo\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def svc(self) -> RepoMapService:
        return RepoMapService(str(self.root))


class TestRender(_Base):
    def test_generates_map_and_meta(self):
        result = self.svc().ensure()
        self.assertEqual(result["action"], "regenerated")
        self.assertTrue((self.root / ".c3" / MAP_NAME).exists())
        meta = json.loads((self.root / ".c3" / META_NAME).read_text(encoding="utf-8"))
        self.assertEqual(meta["schema"], 1)
        self.assertGreater(meta["tokens"], 0)

    def test_map_is_data_not_instructions(self):
        self.svc().ensure()
        content = (self.root / ".c3" / MAP_NAME).read_text(encoding="utf-8")
        self.assertIn("not instructions", content)
        self.assertIn("Do not edit", content)

    def test_no_timestamps_in_map_body(self):
        """Byte stability: volatile state lives in meta, never MAP.md."""
        self.svc().ensure()
        content = (self.root / ".c3" / MAP_NAME).read_text(encoding="utf-8")
        import re
        self.assertNotRegex(content, r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

    def test_commands_and_sections_detected(self):
        self.svc().ensure()
        content = (self.root / ".c3" / MAP_NAME).read_text(encoding="utf-8")
        self.assertIn("python -m pytest", content)
        self.assertIn("## Tree", content)
        self.assertIn("src/", content)


class TestByteStability(_Base):
    def test_second_ensure_is_fresh_noop(self):
        svc = self.svc()
        svc.ensure()
        self.assertEqual(svc.ensure()["action"], "fresh")

    def test_forced_refresh_without_changes_keeps_bytes(self):
        svc = self.svc()
        svc.ensure()
        before = (self.root / ".c3" / MAP_NAME).read_bytes()
        result = svc.refresh()
        self.assertEqual(result["action"], "meta_only")
        self.assertEqual((self.root / ".c3" / MAP_NAME).read_bytes(), before)

    def test_content_change_writes_backup(self):
        svc = self.svc()
        svc.ensure()
        (self.root / "src" / "newmod.py").write_text("y = 2\n", encoding="utf-8")
        result = svc.refresh()
        self.assertEqual(result["action"], "regenerated")
        self.assertTrue((self.root / ".c3" / (MAP_NAME + ".bak")).exists())

    def test_no_tmp_leftovers(self):
        svc = self.svc()
        svc.ensure()
        svc.refresh()
        leftovers = [p.name for p in (self.root / ".c3").iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])


class TestStaleness(_Base):
    def test_missing_map_is_stale(self):
        status = self.svc().status()
        self.assertTrue(status["stale"])
        self.assertIn("missing_map", status["reasons"])

    def test_dirty_sentinel_marks_stale_and_ensure_clears_it(self):
        svc = self.svc()
        svc.ensure()
        mark_map_dirty(str(self.root), "test")
        status = svc.status()
        self.assertTrue(status["stale"])
        self.assertIn("dirty_sentinel", status["reasons"])
        svc.ensure()
        self.assertFalse((self.root / ".c3" / DIRTY_NAME).exists())
        self.assertFalse(svc.status()["stale"])

    def test_new_file_changes_plain_fingerprint(self):
        svc = self.svc()
        svc.ensure()
        time.sleep(0.01)
        (self.root / "src" / "extra.py").write_text("z = 3\n", encoding="utf-8")
        status = svc.status()
        self.assertTrue(status["stale"])
        self.assertIn("worktree_changed", status["reasons"])

    def test_schema_bump_invalidates(self):
        svc = self.svc()
        svc.ensure()
        meta_path = self.root / ".c3" / META_NAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["schema"] = 0
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertIn("schema_changed", svc.status()["reasons"])


@unittest.skipUnless(_git_available(), "git not on PATH")
class TestGitStaleness(_Base):
    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True,
            timeout=15, check=False,
        )

    def setUp(self):
        super().setUp()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@t.t")
        self._git("config", "user.name", "t")
        self._git("add", "-A")
        self._git("commit", "-m", "init")

    def test_new_commit_changes_head(self):
        svc = self.svc()
        svc.ensure()
        (self.root / "src" / "extra.py").write_text("z\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "more")
        status = svc.status()
        self.assertTrue(status["stale"])
        self.assertIn("head_changed", status["reasons"])

    def test_branch_switch_detected(self):
        svc = self.svc()
        svc.ensure()
        self._git("checkout", "-b", "feature")
        self.assertIn("branch_changed", svc.status()["reasons"])

    def test_untracked_file_changes_worktree_sig(self):
        svc = self.svc()
        svc.ensure()
        (self.root / "untracked.py").write_text("u\n", encoding="utf-8")
        self.assertIn("worktree_changed", svc.status()["reasons"])


class TestLocking(_Base):
    def test_held_lock_skips_regeneration(self):
        svc = self.svc()
        (self.root / ".c3").mkdir(exist_ok=True)
        lock = self.root / ".c3" / LOCK_NAME
        lock.write_text("held", encoding="utf-8")
        result = svc.ensure()
        self.assertEqual(result["action"], "locked")
        self.assertFalse((self.root / ".c3" / MAP_NAME).exists())

    def test_stale_lock_is_recovered(self):
        svc = self.svc()
        (self.root / ".c3").mkdir(exist_ok=True)
        lock = self.root / ".c3" / LOCK_NAME
        lock.write_text("dead", encoding="utf-8")
        old = time.time() - 600
        os.utime(lock, (old, old))
        result = svc.ensure()
        self.assertEqual(result["action"], "regenerated")
        self.assertFalse(lock.exists())

    def test_lock_released_after_ensure(self):
        svc = self.svc()
        svc.ensure()
        self.assertFalse((self.root / ".c3" / LOCK_NAME).exists())


class TestTruncationAndBudget(_Base):
    def test_file_cap_truncates_with_visible_marker(self):
        for i in range(30):
            (self.root / "src" / f"m{i:02d}.py").write_text("a\n", encoding="utf-8")
        (self.root / ".c3").mkdir(exist_ok=True)
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"map": {"file_cap": 10}}), encoding="utf-8")
        svc = self.svc()
        result = svc.ensure()
        self.assertTrue(result["truncated"])
        content = (self.root / ".c3" / MAP_NAME).read_text(encoding="utf-8")
        self.assertIn("truncated", content.lower())

    def test_token_budget_slims_tree(self):
        for i in range(40):
            (self.root / "src" / f"verylongmodulename{i:03d}.py").write_text(
                "a\n", encoding="utf-8")
        (self.root / ".c3").mkdir(exist_ok=True)
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"map": {"token_budget": 120}}), encoding="utf-8")
        svc = self.svc()
        svc.ensure()
        content = (self.root / ".c3" / MAP_NAME).read_text(encoding="utf-8")
        self.assertNotIn("verylongmodulename000.py", content)
        self.assertIn("src/", content)


class TestSubprojectBoundaries(_Base):
    def test_subproject_rendered_as_boundary_not_expanded(self):
        child = self.root / "childproj"
        (child / "deep").mkdir(parents=True)
        (child / "deep" / "secret.py").write_text("s\n", encoding="utf-8")
        (self.root / ".c3").mkdir(exist_ok=True)
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"subprojects": [{"rel_path": "childproj"}]}),
            encoding="utf-8")
        svc = self.svc()
        svc.ensure()
        content = (self.root / ".c3" / MAP_NAME).read_text(encoding="utf-8")
        self.assertIn("[sub-project]", content)
        self.assertIn("childproj/.c3/MAP.md", content)
        self.assertNotIn("secret.py", content)


class TestStructuralChange(unittest.TestCase):
    def test_create_delete_rename_are_structural(self):
        for ct in ("create", "delete", "rename"):
            self.assertTrue(is_structural_change("src/x.py", ct))

    def test_plain_edit_is_not_structural(self):
        self.assertFalse(is_structural_change("src/x.py", "edit"))

    def test_manifest_edit_is_structural(self):
        self.assertTrue(is_structural_change("pyproject.toml", "edit"))
        self.assertTrue(is_structural_change("sub/dir/package.json", "edit"))


class TestDirtySentinel(unittest.TestCase):
    def test_mark_dirty_never_raises_and_caps_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(300):
                mark_map_dirty(tmp, f"reason-{i}" * 10)
            sentinel = Path(tmp) / ".c3" / DIRTY_NAME
            self.assertTrue(sentinel.exists())
            self.assertLess(sentinel.stat().st_size, 16384)


class TestDisabled(_Base):
    def test_disabled_via_config(self):
        (self.root / ".c3").mkdir(exist_ok=True)
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"map": {"enabled": False}}), encoding="utf-8")
        result = self.svc().ensure()
        self.assertEqual(result["action"], "disabled")
        self.assertFalse((self.root / ".c3" / MAP_NAME).exists())


if __name__ == "__main__":
    unittest.main()
