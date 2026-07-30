"""Tests for c3_edit's cross-process same-file serialization (docs/agent-locks.md §5).

c3_edit used to guard concurrent edits with a per-file ``threading.Lock``, which
only serializes threads inside ONE process. Every Claude Code session spawns its
own ``c3-mcp`` stdio server, so two sessions editing the same file could tear
each other's writes — and create mode was not guarded at all, so two agents
creating the same path both reported success and one file silently won.

These tests pin the three properties that fix depends on:
  1. the lock sidecar is derived from the TARGET path, not the caller's project;
  2. a lock held by another process blocks edit AND create;
  3. contention surfaces as a ``[c3-lock:busy]`` refusal, never a traceback.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.task_store as task_store  # noqa: E402
from cli.tools import edit as edit_mod  # noqa: E402
from cli.tools.edit import _lock_sidecar, handle_edit  # noqa: E402


def _make_svc(project_path: Path):
    svc = MagicMock()
    svc.project_path = str(project_path)
    svc.edit_ledger = None          # skip ledger side-effects
    svc.activity_log = None
    svc.session_mgr = None
    return svc


def _finalize(name, args, resp, summ, **kw):
    return resp


# Subprocess that grabs the sidecar lock, announces itself, and holds until the
# ready-file is deleted. A real second process is the only honest way to prove
# cross-process exclusion — an in-process thread would prove nothing.
_HOLDER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {repo!r})
    from services.task_store import _FileLock
    lock = _FileLock({sidecar!r})
    lock.acquire()
    open({ready!r}, "w", encoding="utf-8").close()
    while True:
        import os
        if not os.path.exists({ready!r}):
            break
        time.sleep(0.02)
    lock.release()
""")


class TestSidecarIdentity(unittest.TestCase):
    """The sidecar must be a function of the target file alone."""

    def test_same_target_same_sidecar(self):
        p = Path(tempfile.gettempdir()) / "c3lock" / "api.py"
        self.assertEqual(_lock_sidecar(p), _lock_sidecar(Path(str(p))))

    def test_different_targets_differ(self):
        base = Path(tempfile.gettempdir()) / "c3lock"
        self.assertNotEqual(_lock_sidecar(base / "a.py"),
                            _lock_sidecar(base / "b.py"))

    def test_case_follows_platform(self):
        """Windows paths are case-insensitive, so two spellings of one file must
        collide. POSIX paths are case-sensitive, so they must not.

        Known residual: macOS ships a case-INsensitive filesystem by default, so
        there API.py and api.py are one file but get two sidecars. Documented in
        docs/agent-locks.md §9 rather than papered over — detecting per-volume
        case sensitivity at runtime costs more than the bug is worth."""
        base = Path(tempfile.gettempdir()) / "c3lock"
        same = _lock_sidecar(base / "API.py") == _lock_sidecar(base / "api.py")
        self.assertEqual(same, os.name == "nt")

    def test_sidecar_is_spelling_independent(self):
        """The invariant CI caught: exclusion evaporates unless every spelling of
        one file hashes to one sidecar. handle_edit resolves before locking, so
        anything computing a sidecar from an UNRESOLVED path locks a different
        file and blocks nothing — silently, which is the dangerous part."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pkg").mkdir()
            target = root / "pkg" / "mod.py"
            target.write_text("x\n", encoding="utf-8")

            spellings = [
                target,
                root / "pkg" / "." / "mod.py",
                root / "pkg" / ".." / "pkg" / "mod.py",
            ]
            sidecars = {str(_lock_sidecar(p)) for p in spellings}
            self.assertEqual(len(sidecars), 1, f"spellings diverged: {sidecars}")

    def test_sidecar_follows_symlinked_parent(self):
        """macOS /var -> /private/var is exactly this case, and it is what made
        the first version of these tests pass on Linux while proving nothing on
        macOS: the holder locked one path, c3_edit locked another."""
        if os.name == "nt":
            self.skipTest("symlink creation requires privilege on Windows")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real"
            real.mkdir()
            (real / "m.py").write_text("x\n", encoding="utf-8")
            link = root / "link"
            os.symlink(real, link)
            self.assertEqual(_lock_sidecar(real / "m.py"),
                             _lock_sidecar(link / "m.py"))

    def test_sidecar_ignores_calling_project(self):
        """c3_project(action='edit') proxies into handle_edit with the CALLER's
        svc. If the sidecar were derived from svc.project_path, two agents in
        different projects would take different locks on the same file."""
        target = Path(tempfile.gettempdir()) / "c3lock" / "shared.py"
        # _lock_sidecar takes only the target path — there is no project input
        # to get wrong. This asserts that signature stays that way.
        import inspect
        params = list(inspect.signature(_lock_sidecar).parameters)
        self.assertEqual(params, ["path"])
        self.assertTrue(str(_lock_sidecar(target)).endswith(".lock"))


class TestCrossProcessExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = _make_svc(self.root)
        # Keep sidecars out of the developer's real ~/.c3 during tests.
        self._orig_dir = edit_mod._EDIT_LOCK_DIR
        edit_mod._EDIT_LOCK_DIR = self.root / ".locks"
        # 30s is right in production, unusable in a test.
        self._orig_timeout = task_store._LOCK_TIMEOUT_S
        task_store._LOCK_TIMEOUT_S = 0.3
        self.holder = None

    def tearDown(self):
        self._stop_holder()
        edit_mod._EDIT_LOCK_DIR = self._orig_dir
        task_store._LOCK_TIMEOUT_S = self._orig_timeout
        self.tmp.cleanup()

    def _hold(self, target: Path):
        """Start a second process holding `target`'s sidecar lock."""
        sidecar = _lock_sidecar(target)
        # If the holder's sidecar ever diverges from the one c3_edit computes,
        # every exclusion test below silently proves nothing — it locks a file
        # nobody contends for. Assert here so the failure names its own cause.
        self.assertEqual(sidecar, _lock_sidecar(target.resolve()),
                         "sidecar must not depend on how the path is spelled")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        ready = self.root / "held.flag"
        repo = str(Path(__file__).resolve().parents[1])
        code = _HOLDER.format(repo=repo, sidecar=str(sidecar), ready=str(ready))
        self.holder = subprocess.Popen([sys.executable, "-c", code])
        self.ready = ready
        deadline = time.monotonic() + 20
        while not ready.exists():
            if time.monotonic() > deadline:
                self.fail("holder subprocess never acquired the lock")
            if self.holder.poll() is not None:
                self.fail(f"holder exited early (rc={self.holder.returncode})")
            time.sleep(0.02)

    def _stop_holder(self):
        if self.holder is None:
            return
        try:
            self.ready.unlink(missing_ok=True)
            self.holder.wait(timeout=10)
        except Exception:
            self.holder.kill()
        finally:
            self.holder = None

    # ── the actual regressions ───────────────────────────────────────────────

    def test_edit_blocked_while_another_process_holds_the_file(self):
        target = self.root / "router.py"
        target.write_text("alpha\n", encoding="utf-8")
        self._hold(target)

        out = handle_edit(str(target), "alpha", "beta", "", "", False,
                          self.svc, _finalize)

        self.assertIn("[c3-lock:busy]", out)
        # The refusal must not be silently ignorable: it names the anti-pattern.
        self.assertIn("do not route around", out)
        # And the file must be untouched.
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha\n")

    def test_create_blocked_while_another_process_holds_the_path(self):
        """The gap this phase closes: create mode ran entirely outside the lock,
        so two agents creating one path both 'succeeded'."""
        target = self.root / "new_module.py"
        self.assertFalse(target.exists())
        self._hold(target)

        out = handle_edit(str(target), "", "first writer\n", "", "", False,
                          self.svc, _finalize)

        self.assertIn("[c3-lock:busy]", out)
        self.assertFalse(target.exists(), "create must not win a contested path")

    def test_batch_blocked_while_another_process_holds_the_file(self):
        target = self.root / "batch.py"
        target.write_text("one\ntwo\n", encoding="utf-8")
        self._hold(target)

        out = handle_edit(str(target), "", "", "", "", False, self.svc, _finalize,
                          edits='[{"old_string": "one", "new_string": "1"}]')

        self.assertIn("[c3-lock:busy]", out)
        self.assertEqual(target.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_lock_is_released_so_the_next_edit_succeeds(self):
        """Contention must be transient — a released lock leaves no residue."""
        target = self.root / "seq.py"
        target.write_text("alpha\n", encoding="utf-8")
        self._hold(target)
        blocked = handle_edit(str(target), "alpha", "beta", "", "", False,
                              self.svc, _finalize)
        self.assertIn("[c3-lock:busy]", blocked)

        self._stop_holder()

        out = handle_edit(str(target), "alpha", "beta", "", "", False,
                          self.svc, _finalize)
        self.assertNotIn("[c3-lock:busy]", out)
        self.assertEqual(target.read_text(encoding="utf-8"), "beta\n")


class TestUncontendedPathUnchanged(unittest.TestCase):
    """Locking must be invisible when nothing is contending."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = _make_svc(self.root)
        self._orig_dir = edit_mod._EDIT_LOCK_DIR
        edit_mod._EDIT_LOCK_DIR = self.root / ".locks"

    def tearDown(self):
        edit_mod._EDIT_LOCK_DIR = self._orig_dir
        self.tmp.cleanup()

    def test_single_edit_still_applies(self):
        target = self.root / "a.py"
        target.write_text("hello world\n", encoding="utf-8")
        handle_edit(str(target), "world", "there", "", "", False,
                    self.svc, _finalize)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello there\n")

    def test_create_still_creates(self):
        target = self.root / "sub" / "b.py"
        handle_edit(str(target), "", "made\n", "", "", False, self.svc, _finalize)
        self.assertEqual(target.read_text(encoding="utf-8"), "made\n")

    def test_batch_still_applies(self):
        target = self.root / "c.py"
        target.write_text("one\ntwo\n", encoding="utf-8")
        handle_edit(str(target), "", "", "", "", False, self.svc, _finalize,
                    edits='[{"old_string": "one", "new_string": "1"},'
                          ' {"old_string": "two", "new_string": "2"}]')
        self.assertEqual(target.read_text(encoding="utf-8"), "1\n2\n")

    def test_reentrant_same_thread(self):
        """Two sequential edits in one thread must not deadlock on the sidecar."""
        target = self.root / "d.py"
        target.write_text("a\n", encoding="utf-8")
        handle_edit(str(target), "a", "b", "", "", False, self.svc, _finalize)
        handle_edit(str(target), "b", "c", "", "", False, self.svc, _finalize)
        self.assertEqual(target.read_text(encoding="utf-8"), "c\n")


if __name__ == "__main__":
    unittest.main()
