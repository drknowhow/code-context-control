"""Override Requests — waking the asking session when a human decides.

Frozen spec: docs/override-requests.md §7.1.

The bug this defends against is not "the wake command was wrong". It is
**silence**: before this, a decision was a write to disk that nobody was
listening for, and a correctly-minted grant expired unused while the user
believed their tap had worked. So the tests assert the command actually ran
with the identifiers a woken agent needs — by running a real subprocess that
writes its argv to disk — rather than mocking ``subprocess.run`` and checking
we called it, which is the same silence with a green tick on it.

Two invariants beyond "it fires":

* A wake that fails NEVER unwinds a decision. Exit 1, a missing binary, a
  timeout — the approval stands and the grant is intact.
* A wake spec we cannot parse fails the whole ``override`` section closed. It
  names a command this machine will run; degrading a typo into "no wake, carry
  on" would silently restore the bug.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

from services import access_guard as ag  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402
from services import override_wake as owake  # noqa: E402

SESSION = "sess-wake"
ALL_LAYERS_ON = {k: True for k in opol.LAYER_KEYS}

# Writes its own argv to the path in argv[1], then exits with argv[2]'s code.
# A real process, so "the command ran" is evidence rather than an assertion
# about a mock.
_RECORDER = (
    "import json,sys;"
    "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(sys.argv[3:]));"
    "sys.exit(int(sys.argv[2]))"
)


class WakeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = root / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / "secrets").mkdir()
        self.blocked = self.proj / "secrets" / "key.txt"
        self.blocked.write_text("k", encoding="utf-8")
        self.receipt = root / "wake-argv.json"

        self._store = root / "override_requests.json"
        self._patches = [
            mock.patch.object(opol, "_global_base", return_value=None),
            mock.patch.object(orq, "store_path", return_value=self._store),
        ]
        for p in self._patches:
            p.start()
        self.write_config()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def wake_spec(self, *, exit_code=0, extra_args=(), **kw):
        spec = {
            "command": [sys.executable, "-c", _RECORDER, str(self.receipt),
                        str(exit_code), "{request_id}", "{status}",
                        "{grant_id}", "{message}", *extra_args],
            "cwd": str(self.proj),
            "timeout_s": 15,
        }
        spec.update(kw)
        return spec

    def write_config(self, *, wake=None, layers=None, enabled=True):
        section = {"enabled": enabled, "layers": dict(layers or ALL_LAYERS_ON)}
        if wake is not None:
            section["wake"] = wake
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": {"deny": ["secrets/**"]},
                        "override": section}), encoding="utf-8")

    def request(self, **kw):
        args = dict(session_id=SESSION, tool="Read", op="read",
                    path=str(self.blocked),
                    denial=ag.check(str(self.blocked), "read", str(self.proj)),
                    justification="the pairing fixture needs this key")
        args.update(kw)
        return orq.create(str(self.proj), **args)

    def recorded(self):
        """The substituted argv the wake command actually received."""
        return json.loads(self.receipt.read_text(encoding="utf-8"))

    def events(self):
        return [e.get("event") for e in og.read_audit(str(self.proj), 0)]


class SpecValidation(unittest.TestCase):
    """§7.1 — what counts as a wake spec. Absent is fine; wrong is not."""

    def test_absent_is_valid(self):
        self.assertTrue(owake.validate_spec(None))

    def test_minimal_command_list(self):
        self.assertTrue(owake.validate_spec({"command": ["echo", "hi"]}))

    def test_string_command_is_rejected(self):
        # The whole safety story is "argv, never a shell string". Accepting a
        # string here would be the one change that reintroduces quoting bugs
        # and injection at the same time.
        self.assertFalse(owake.validate_spec({"command": "echo hi"}))

    def test_empty_and_non_string_args_rejected(self):
        self.assertFalse(owake.validate_spec({"command": []}))
        self.assertFalse(owake.validate_spec({"command": ["echo", 7]}))
        self.assertFalse(owake.validate_spec({"command": ["echo", ""]}))

    def test_unknown_key_rejected(self):
        self.assertFalse(owake.validate_spec(
            {"command": ["echo"], "shell": True}))

    def test_bad_types(self):
        self.assertFalse(owake.validate_spec({"command": ["e"], "cwd": 3}))
        self.assertFalse(owake.validate_spec({"command": ["e"], "timeout_s": 0}))
        self.assertFalse(owake.validate_spec(
            {"command": ["e"], "timeout_s": True}))
        self.assertFalse(owake.validate_spec({"command": ["e"], "on": []}))
        self.assertFalse(owake.validate_spec(
            {"command": ["e"], "on": ["expired"]}))

    def test_substitution_survives_braces(self):
        # A justification or a rule glob routinely contains braces; str.format
        # would raise KeyError and lose the wake for exactly those requests.
        out = owake._subst("{message}", {"message": "rule {a,b}/**"})
        self.assertEqual(out, "rule {a,b}/**")
        self.assertEqual(owake._subst("{nope}", {"a": "1"}), "{nope}")


class WakeFires(WakeBase):
    """The decision reaches the agent."""

    def test_approve_runs_the_command_with_identifiers(self):
        self.write_config(wake=self.wake_spec())
        row = self.request()
        decided = orq.decide(row["id"], "approve", confirm="secrets/**")

        rid, status, grant_id, message = self.recorded()
        self.assertEqual(rid, row["id"])
        self.assertEqual(status, orq.STATUS_APPROVED)
        self.assertEqual(grant_id, decided["grant_id"])
        # The message must carry the one instruction that makes the tap
        # useful. A woken agent that has to rediscover what to do burns the
        # grant's TTL doing it.
        self.assertIn("APPROVED", message)
        self.assertIn("Retry the SAME call once", message)
        self.assertIn(decided["grant_id"], message)
        self.assertIn(owake.EV_WOKE, self.events())

    def test_deny_wakes_too(self):
        # Otherwise the agent sits in `wait` until the request lapses and
        # reports a timeout for a question answered in two seconds.
        self.write_config(wake=self.wake_spec())
        row = self.request()
        orq.decide(row["id"], "deny", note="use the fixture instead")

        rid, status, grant_id, message = self.recorded()
        self.assertEqual((rid, status, grant_id),
                         (row["id"], orq.STATUS_DENIED, ""))
        self.assertIn("DENIED", message)
        self.assertIn("do not re-ask", message)

    def test_on_filter_narrows_to_approvals(self):
        self.write_config(wake=self.wake_spec(on=["approved"]))
        row = self.request()
        orq.decide(row["id"], "deny")
        self.assertFalse(self.receipt.exists())

        row2 = self.request()
        orq.decide(row2["id"], "approve", confirm="secrets/**")
        self.assertTrue(self.receipt.exists())

    def test_no_wake_configured_is_a_quiet_no_op(self):
        self.write_config()  # no `wake` key at all
        row = self.request()
        decided = orq.decide(row["id"], "approve", confirm="secrets/**")
        self.assertEqual(decided["status"], orq.STATUS_APPROVED)
        self.assertFalse(decided["wake"]["fired"])
        self.assertNotIn(owake.EV_WOKE, self.events())


class WakeFailureIsContained(WakeBase):
    """A wake is a shortcut past waiting, not the mechanism. It may fail."""

    def test_nonzero_exit_does_not_unwind_the_approval(self):
        self.write_config(wake=self.wake_spec(exit_code=3))
        row = self.request()
        decided = orq.decide(row["id"], "approve", confirm="secrets/**")

        self.assertEqual(decided["status"], orq.STATUS_APPROVED)
        self.assertEqual(decided["wake"]["exit_code"], 3)
        self.assertFalse(decided["wake"]["ok"])
        self.assertIn(owake.EV_WAKE_FAILED, self.events())
        # The grant is the thing that matters, and it is untouched.
        grants, corrupt = og.load(str(self.proj))
        self.assertFalse(corrupt)
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["uses_remaining"], 1)
        self.assertEqual(grants[0]["id"], decided["grant_id"])

    def test_missing_binary_does_not_raise(self):
        self.write_config(wake={"command": ["c3-no-such-binary-xyz", "{message}"],
                                "cwd": str(self.proj)})
        row = self.request()
        decided = orq.decide(row["id"], "approve", confirm="secrets/**")
        self.assertEqual(decided["status"], orq.STATUS_APPROVED)
        self.assertFalse(decided["wake"].get("ok"))
        self.assertIn(owake.EV_WAKE_FAILED, self.events())

    def test_missing_cwd_is_refused_before_spawning(self):
        self.write_config(wake=self.wake_spec(cwd=str(self.proj / "gone")))
        row = self.request()
        decided = orq.decide(row["id"], "approve", confirm="secrets/**")
        self.assertFalse(decided["wake"]["fired"])
        self.assertFalse(self.receipt.exists())
        self.assertIn(owake.EV_WAKE_FAILED, self.events())

    def test_timeout_is_bounded_and_survivable(self):
        self.write_config(wake={
            "command": [sys.executable, "-c", "import time;time.sleep(30)"],
            "cwd": str(self.proj), "timeout_s": 1})
        row = self.request()
        decided = orq.decide(row["id"], "approve", confirm="secrets/**")
        self.assertEqual(decided["status"], orq.STATUS_APPROVED)
        self.assertFalse(decided["wake"]["ok"])
        self.assertIn(owake.EV_WAKE_FAILED, self.events())


class CorruptWakeFailsClosed(WakeBase):
    """A spec we cannot parse disables overrides — it does not skip the wake."""

    def test_invalid_wake_disables_the_section(self):
        self.write_config(wake={"command": "rm -rf /"})   # string form
        policy = opol.resolve(str(self.proj))
        self.assertFalse(policy.enabled)
        self.assertTrue(policy.corrupt_scopes)
        with self.assertRaises(orq.OverrideError):
            self.request()

    def test_valid_wake_never_crosses_the_wire(self):
        # `as_dict` is what the phone's policy screen renders. The argv is not
        # a policy setting; only whether anything is listening.
        self.write_config(wake=self.wake_spec())
        shown = opol.resolve(str(self.proj)).as_dict()
        self.assertTrue(shown["wake_configured"])
        self.assertNotIn("wake", shown)
        self.assertNotIn(_RECORDER, json.dumps(shown))


if __name__ == "__main__":
    unittest.main()
