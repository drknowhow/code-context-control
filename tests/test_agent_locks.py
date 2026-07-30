"""Tests for the Agent Locks lease engine (docs/agent-locks.md Layer B)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_locks as al  # noqa: E402


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".c3").mkdir()
        (self.root / "services").mkdir()
        (self.root / "services" / "router.py").write_text("x\n", encoding="utf-8")
        self.clock = _Clock()
        self.store = al.LockStore(self.root, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def grab(self, paths, session="s1", agent=None, intent=""):
        return self.store.acquire(
            paths, agent_id=agent or al.agent_id_for(session),
            session_id=session, intent=intent)


class TestPathIdentity(unittest.TestCase):
    def test_wsl_and_windows_collide(self):
        root_win = "U:/repo"
        self.assertEqual(
            al.normalize_relpath("/mnt/u/repo/src/API.PY", root_win),
            al.normalize_relpath(r"U:\repo\src\api.py", root_win))

    def test_leading_slash_is_repo_relative(self):
        self.assertEqual(al.normalize_relpath("/src/a.py", "U:/repo"), "src/a.py")

    def test_outside_repo_refused_not_guessed(self):
        with self.assertRaises(al.UnsupportedPathError) as c:
            al.normalize_relpath("U:/other/a.py", "U:/repo")
        self.assertEqual(c.exception.reason, "outside_repo")

    def test_unc_refused(self):
        with self.assertRaises(al.UnsupportedPathError) as c:
            al.normalize_relpath("//server/share/a.py", "U:/repo")
        self.assertEqual(c.exception.reason, "unc")

    def test_repo_root_itself_refused(self):
        with self.assertRaises(al.UnsupportedPathError) as c:
            al.normalize_relpath("U:/repo", "U:/repo")
        self.assertEqual(c.exception.reason, "is_root")

    def test_absolute_and_relative_spellings_of_one_file_agree(self):
        """The bug CI caught, asserted the only way that works on every OS.

        Only drive-letter and /mnt forms counted as absolute, so on POSIX an
        absolute path fell into the repo-relative branch: a lease taken as
        'services/router.py' was looked up as 'tmp/xyz/services/router.py' and
        found nothing. A key computable two ways is not a key.

        This must use a REAL temp root, not a literal '/tmp/x': canonical_root
        calls os.path.abspath, which on Windows rewrites '/tmp/x' to the
        current drive and makes a POSIX-literal assertion meaningless there.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(
                al.normalize_relpath("services/router.py", root),
                al.normalize_relpath(root / "services" / "router.py", root))

    def test_resolved_path_and_unresolved_root_agree(self):
        """The CI-Windows case. handle_edit calls Path.resolve() before locking
        (expanding RUNNER~1 → runneradmin) while project_path stays as given,
        so the file looked OUTSIDE its own repo, normalize_relpath raised
        outside_repo, and the lease was silently never taken.

        Trivially true on a dev box whose paths need no resolving — which is
        exactly why it was missed — but it is the real assertion on CI."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "services" / "router.py"
            self.assertEqual(
                al.normalize_relpath(target.resolve(), root),
                al.normalize_relpath("services/router.py", root))

    def test_short_name_root_agrees(self):
        """Reproduces the CI-Windows failure directly rather than by analogy.

        The runner's home is the 8.3 alias RUNNER~1; handle_edit resolves it to
        runneradmin while project_path keeps the alias, so the file read as
        outside_repo and the lease was silently never taken. Verified both ways
        before shipping: with the pre-fix logic the two spellings do not agree.

        Skips where 8.3 generation is disabled — there the condition genuinely
        cannot occur, and a silent pass would be the same trap again."""
        if os.name != "nt":
            self.skipTest("8.3 aliases are a Windows filesystem feature")
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        with tempfile.TemporaryDirectory(prefix="averylongdirectoryname_") as td:
            root = Path(td)
            (root / "services").mkdir()
            got = ctypes.windll.kernel32.GetShortPathNameW(str(root), buf, 1024)
            short = buf.value if got else str(root)
            if short.casefold() == str(root).casefold():
                self.skipTest("8.3 generation is off on this volume")
            self.assertEqual(
                al.normalize_relpath((root / "services" / "router.py").resolve(),
                                     short),
                al.normalize_relpath("services/router.py", root))

    def test_symlinked_root_agrees(self):
        """macOS /var → /private/var in one assertion. Skipped on Windows,
        where creating a symlink needs privilege."""
        if os.name == "nt":
            self.skipTest("symlink creation requires privilege on Windows")
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real"
            (real / "services").mkdir(parents=True)
            link = Path(td) / "link"
            os.symlink(real, link)
            self.assertEqual(
                al.normalize_relpath(real / "services" / "router.py", link),
                al.normalize_relpath("services/router.py", real))

    def test_leading_slash_outside_root_stays_repo_relative(self):
        """The agent convention '/src/api.py' must keep working — it is not an
        absolute path that happens to miss, it is how agents write relpaths."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(al.normalize_relpath("/src/api.py", td),
                             "src/api.py")

    def test_nonexistent_path_still_normalizes(self):
        """Purely lexical: create-mode paths must key the same as real ones."""
        self.assertEqual(
            al.normalize_relpath("U:/repo/does/not/exist.py", "U:/repo"),
            "does/not/exist.py")

    def test_agent_id_matches_fleetdeck_convention(self):
        self.assertEqual(al.agent_id_for("a3f19c2b-dead-beef"),
                         "claude-code:a3f19c2b")


class TestAcquireDenyRelease(_Base):
    def test_first_acquire_granted(self):
        r = self.grab(["services/router.py"], intent="refactor backoff")
        self.assertTrue(r["granted"])
        self.assertEqual(r["locks"][0]["relpath"], "services/router.py")

    def test_second_session_denied_with_owner_and_intent(self):
        self.grab(["services/router.py"], session="s1", intent="refactor backoff")
        r = self.grab(["services/router.py"], session="s2")
        self.assertFalse(r["granted"])
        c = r["conflicts"][0]
        self.assertEqual(c["owner"], "claude-code:s1")
        self.assertEqual(c["intent"], "refactor backoff")

    def test_same_session_is_reentrant(self):
        """One agent edits a file repeatedly; that must not self-deadlock."""
        a = self.grab(["services/router.py"], session="s1")
        b = self.grab(["services/router.py"], session="s1")
        self.assertTrue(b["granted"])
        self.assertEqual(a["locks"][0]["fencing_token"],
                         b["locks"][0]["fencing_token"],
                         "re-acquiring must not mint a new token")

    def test_release_frees_for_others(self):
        self.grab(["services/router.py"], session="s1")
        self.store.release(session_id="s1")
        self.assertTrue(self.grab(["services/router.py"], session="s2")["granted"])

    def test_release_only_touches_own_leases(self):
        self.grab(["a.py"], session="s1")
        self.grab(["b.py"], session="s2")
        self.store.release(session_id="s1")
        self.assertEqual([r["relpath"] for r in self.store.snapshot()["locks"]],
                         ["b.py"])


class TestAllOrNothing(_Base):
    def test_partial_conflict_grants_nothing(self):
        self.grab(["b.py"], session="s2")
        r = self.grab(["a.py", "b.py", "c.py"], session="s1")
        self.assertFalse(r["granted"])
        held = {x["relpath"] for x in self.store.snapshot()["locks"]}
        self.assertEqual(held, {"b.py"},
                         "a.py/c.py must NOT have been grabbed on a failed multi-acquire")

    def test_opposite_order_cannot_deadlock(self):
        """Sorted acquisition means the loser loses cleanly rather than each
        holding one of the pair."""
        r1 = self.grab(["a.py", "b.py"], session="s1")
        r2 = self.grab(["b.py", "a.py"], session="s2")
        self.assertTrue(r1["granted"])
        self.assertFalse(r2["granted"])


class TestLeaseExpiry(_Base):
    def test_ttl_frees_a_crashed_agent(self):
        self.grab(["services/router.py"], session="dead")
        self.assertFalse(self.grab(["services/router.py"], session="s2")["granted"])
        self.clock.advance(al.DEFAULT_TTL_S + 1)
        self.assertTrue(self.grab(["services/router.py"], session="s2")["granted"])

    def test_snapshot_hides_expired_without_mutating(self):
        self.grab(["a.py"], session="s1")
        self.clock.advance(al.DEFAULT_TTL_S + 1)
        self.assertEqual(self.store.snapshot()["count"], 0)
        raw = json.loads((self.root / ".c3" / "locks.json").read_text())
        self.assertEqual(len(raw["locks"]), 1, "a read must not rewrite state")

    def test_sweep_removes_expired(self):
        self.grab(["a.py"], session="s1")
        self.clock.advance(al.DEFAULT_TTL_S + 1)
        self.assertEqual(self.store.sweep()["count"], 1)
        raw = json.loads((self.root / ".c3" / "locks.json").read_text())
        self.assertEqual(raw["locks"], [])

    def test_renew_extends(self):
        self.grab(["a.py"], session="s1")
        self.clock.advance(al.DEFAULT_TTL_S - 10)
        self.assertTrue(self.store.renew(["a.py"], session_id="s1")["ok"])
        self.clock.advance(20)
        self.assertFalse(self.grab(["a.py"], session="s2")["granted"])

    def test_renew_rejects_non_owner(self):
        self.grab(["a.py"], session="s1")
        r = self.store.renew(["a.py"], session_id="s2")
        self.assertFalse(r["ok"])
        self.assertEqual(r["rejected"][0]["reason"], "not_owner")


class TestFencing(_Base):
    def test_tokens_are_monotonic(self):
        t1 = self.grab(["a.py"], session="s1")["locks"][0]["fencing_token"]
        t2 = self.grab(["b.py"], session="s1")["locks"][0]["fencing_token"]
        self.assertGreater(t2, t1)

    def test_force_release_bumps_so_holder_is_stale(self):
        before = self.grab(["a.py"], session="s1")["locks"][0]["fencing_token"]
        self.store.force_release("a.py", by="dimitri")
        after = self.grab(["a.py"], session="s2")["locks"][0]["fencing_token"]
        self.assertGreater(after, before + 0,
                           "counter must move past the forced-out token")
        self.assertGreater(json.loads(
            (self.root / ".c3" / "locks.json").read_text())["fencing"], before)

    def test_force_release_reports_previous_owner(self):
        self.grab(["a.py"], session="s1")
        r = self.store.force_release("a.py", by="dimitri")
        self.assertTrue(r["forced"])
        self.assertEqual(r["previous_owner"], "claude-code:s1")


class TestCrossProcessState(_Base):
    def test_two_stores_share_one_file(self):
        """Separate LockStore objects stand in for separate c3-mcp processes."""
        other = al.LockStore(self.root, clock=self.clock)
        self.assertTrue(self.grab(["a.py"], session="s1")["granted"])
        r = other.acquire(["a.py"], agent_id="claude-code:s2",
                          session_id="s2")
        self.assertFalse(r["granted"])
        self.assertEqual(r["conflicts"][0]["owner"], "claude-code:s1")

    def test_corrupt_state_is_quarantined_not_emptied(self):
        self.grab(["a.py"], session="s1")
        (self.root / ".c3" / "locks.json").write_text("{ not json",
                                                      encoding="utf-8")
        self.store.snapshot()
        self.assertTrue((self.root / ".c3" / "locks.json.corrupt").is_file(),
                        "corrupt state must be preserved for inspection")


class TestGate(_Base):
    def setUp(self):
        super().setUp()
        # al.check() builds its own store on the real clock, so the gate tests
        # must too — a fake-clock lease reads as long expired to it.
        self.store = al.LockStore(self.root)

    def test_store_and_gate_agree_on_the_key(self):
        """Guard the whole class: leases are taken by relative path and looked
        up by absolute path, so if those two spellings ever disagree every
        gate test below passes vacuously (None == 'not held')."""
        self.assertEqual(
            self.store._rel("services/router.py"),
            self.store._rel(self.root / "services" / "router.py"))

    def test_check_returns_none_for_the_holder(self):
        self.grab(["services/router.py"], session="s1")
        # Assert the lease actually exists first — otherwise "None" below
        # would prove nothing.
        self.assertEqual(self.store.snapshot()["count"], 1)
        self.assertIsNone(al.check(self.root / "services" / "router.py",
                                   self.root, "s1"))

    def test_check_returns_holder_for_others(self):
        self.grab(["services/router.py"], session="s1", intent="refactor")
        h = al.check(self.root / "services" / "router.py", self.root, "s2")
        self.assertIsNotNone(h)
        self.assertEqual(h["agent_id"], "claude-code:s1")

    def test_refusal_names_the_antipattern(self):
        self.grab(["services/router.py"], session="s1", intent="refactor")
        h = al.check(self.root / "services" / "router.py", self.root, "s2")
        msg = al.refusal(h, "services/router.py")
        self.assertIn(al.TAG_HELD, msg)
        self.assertIn("claude-code:s1", msg)
        self.assertIn("refactor", msg)
        self.assertIn("do not route around", msg)

    def test_check_never_raises_on_bad_state(self):
        (self.root / ".c3" / "locks.json").write_text("garbage", encoding="utf-8")
        self.assertIsNone(al.check(self.root / "a.py", self.root, "s1"))

    def test_disabled_by_config(self):
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"locks": {"enabled": False}}), encoding="utf-8")
        self.grab(["services/router.py"], session="s1")
        self.assertIsNone(al.check(self.root / "services" / "router.py",
                                   self.root, "s2"))


class TestConfig(_Base):
    def test_defaults(self):
        c = al.config(self.root)
        self.assertEqual(c["mode"], "advisory")
        self.assertEqual(c["default_ttl_s"], al.DEFAULT_TTL_S)
        self.assertTrue(c["enabled"])

    def test_reads_overrides(self):
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"locks": {"mode": "strict", "default_ttl_s": 60}}),
            encoding="utf-8")
        c = al.config(self.root)
        self.assertEqual(c["mode"], "strict")
        self.assertEqual(c["default_ttl_s"], 60.0)

    def test_garbage_falls_back_to_defaults(self):
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"locks": {"mode": "nonsense", "default_ttl_s": -5}}),
            encoding="utf-8")
        c = al.config(self.root)
        self.assertEqual(c["mode"], "advisory")
        self.assertEqual(c["default_ttl_s"], al.DEFAULT_TTL_S)


class TestEditIntegration(unittest.TestCase):
    """The wiring that matters: does a lease actually stop the other agent's
    c3_edit? The engine passing its own tests proves nothing about that."""

    def setUp(self):
        from unittest.mock import MagicMock
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".c3").mkdir()
        self.target = self.root / "router.py"
        self.target.write_text("alpha\n", encoding="utf-8")
        self.MagicMock = MagicMock

    def tearDown(self):
        self.tmp.cleanup()

    def _svc(self, session_id):
        svc = self.MagicMock()
        svc.project_path = str(self.root)
        svc.edit_ledger = None
        svc.activity_log = None
        mgr = self.MagicMock()
        mgr.current_session = {"id": session_id}
        svc.session_mgr = mgr
        return svc

    @staticmethod
    def _finalize(name, args, resp, summ, **kw):
        return resp

    def _edit(self, session_id, old, new, summary=""):
        from cli.tools.edit import handle_edit
        return handle_edit(str(self.target), old, new, summary, "", False,
                           self._svc(session_id), self._finalize)

    def test_first_agent_edits_and_takes_a_lease(self):
        out = self._edit("sess-one", "alpha", "beta", summary="refactor backoff")
        self.assertNotIn(al.TAG_HELD, out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "beta\n")
        snap = al.LockStore(self.root).snapshot()
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["locks"][0]["agent_id"], "claude-code:sess-one")
        self.assertEqual(snap["locks"][0]["intent"], "refactor backoff")

    def test_second_agent_is_blocked_with_owner_and_intent(self):
        self._edit("sess-one", "alpha", "beta", summary="refactor backoff")
        out = self._edit("sess-two", "beta", "gamma")
        self.assertIn(al.TAG_HELD, out)
        self.assertIn("claude-code:sess-one", out)
        self.assertIn("refactor backoff", out)
        self.assertIn("do not route around", out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "beta\n",
                         "the blocked edit must not have landed")

    def test_holder_can_keep_editing(self):
        self._edit("sess-one", "alpha", "beta")
        out = self._edit("sess-one", "beta", "gamma")
        self.assertNotIn(al.TAG_HELD, out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "gamma\n")

    def test_release_unblocks_the_other_agent(self):
        self._edit("sess-one", "alpha", "beta")
        al.LockStore(self.root).release(session_id="sess-one")
        out = self._edit("sess-two", "beta", "gamma")
        self.assertNotIn(al.TAG_HELD, out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "gamma\n")

    def test_access_guard_denial_outranks_lease(self):
        """A file the agent may not write must report the POLICY reason, not
        'someone else is editing it' (spec §5 ordering)."""
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"access": {"read_only": ["router.py"]}}),
            encoding="utf-8")
        self._edit("sess-one", "alpha", "beta")  # leases nothing; denied first
        out = self._edit("sess-two", "alpha", "beta")
        self.assertIn("c3-access", out)
        self.assertNotIn(al.TAG_HELD, out)

    def test_two_anonymous_agents_still_block_each_other(self):
        """Regression: when session_mgr yields no id, both agents used to
        resolve to "" — counting as ONE session, so neither blocked the other.
        The identity falls back to the pid instead, never to ""."""
        from cli.tools.edit import _session_id
        svc = self.MagicMock()
        svc.session_mgr.current_session = {}
        self.assertTrue(_session_id(svc))
        self.assertIn("pid-", _session_id(svc))

    def test_edit_and_locks_agree_on_identity(self):
        """A lease taken by c3_edit must be recognised as ours by c3_locks."""
        from cli.tools.edit import _session_id as edit_id
        from cli.tools.locks import _session_id as locks_id
        svc = self._svc("")
        svc.session_mgr.current_session = {}
        self.assertEqual(edit_id(svc), locks_id(svc))

    def test_disabled_locks_leave_editing_untouched(self):
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"locks": {"enabled": False}}), encoding="utf-8")
        self._edit("sess-one", "alpha", "beta")
        out = self._edit("sess-two", "beta", "gamma")
        self.assertNotIn(al.TAG_HELD, out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "gamma\n")


if __name__ == "__main__":
    unittest.main()
