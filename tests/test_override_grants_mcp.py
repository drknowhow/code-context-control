"""P2a — the MCP content tools consult grants (override-requests.md §13).

These exist because the live end-to-end failed in exactly the gap the old
coverage matrix described: a user approved a request on their phone, a grant
was minted with the right shape, and `c3_edit` refused anyway because nothing
on the MCP surface ever looked at it. `c3 override list` afterwards still
read `1 use(s) left`.

Every test here asserts the *use counter*, not just the outcome. A write that
succeeds proves nothing on its own — the project that surfaced the bug runs
`enforcement: advisory`, where native writes land regardless of grants, and
that is precisely how a broken gate can look like a working one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools import _grants  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402

SESSION = "sess-mcp"


class _Svc:
    """The slice of the MCP runtime the gate touches."""

    def __init__(self, project_path, session_id=SESSION):
        self.project_path = str(project_path)
        self.session_mgr = type("M", (), {"current_session": {"id": session_id}})()


class GrantsOnMcpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        (self.proj / "docs").mkdir()
        self.ro = self.proj / "docs" / "note.md"
        self.ro.write_text("original\n", encoding="utf-8")
        self.write_config(
            access={"read_only": ["docs/**"]},
            override={"enabled": True, "layers": {"access_readonly": True}},
        )
        self._patch = mock.patch.object(opol, "_global_base", return_value=None)
        self._patch.start()
        self.svc = _Svc(self.proj)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def write_config(self, *, access=None, override=None):
        cfg = self.proj / ".c3" / "config.json"
        data = {}
        if access is not None:
            data["access"] = access
        if override is not None:
            data["override"] = override
        cfg.write_text(json.dumps(data), encoding="utf-8")

    def denial(self, op="write"):
        d = ag.check(str(self.ro), op, str(self.proj))
        self.assertIsNotNone(d, "fixture must actually be denied")
        return d

    def mint(self, **kw):
        args = dict(session_id=SESSION, layer=opol.GATE_ACCESS,
                    rule="docs/**", tool="c3_edit", op="write",
                    path=str(self.ro))
        args.update(kw)
        return og.mint(str(self.proj), **args)

    def uses_left(self):
        return [int(g.get("uses_remaining") or 0)
                for g in og.active(str(self.proj), SESSION)]


class TestGateHonoursGrant(GrantsOnMcpBase):
    def test_no_grant_stays_denied(self):
        self.assertIsNone(
            _grants.allow(self.svc, self.denial(), tool="c3_edit",
                          op="write", path=str(self.ro)))

    def test_live_grant_allows_and_is_consumed(self):
        self.mint()
        self.assertEqual(self.uses_left(), [1])
        ctx = _grants.allow(self.svc, self.denial(), tool="c3_edit",
                            op="write", path=str(self.ro))
        self.assertIsNotNone(ctx, "an approved grant must permit the retry")
        self.assertIn(opol.TAG_GRANTED, ctx)
        # The counter is the assertion that matters: this is the exact check
        # that exposed the original bug.
        self.assertEqual(self.uses_left(), [],
                         "the single use must be spent, not merely reported")

    def test_second_call_is_denied_again(self):
        self.mint()
        self.assertIsNotNone(_grants.allow(self.svc, self.denial(),
                                           tool="c3_edit", op="write",
                                           path=str(self.ro)))
        self.assertIsNone(_grants.allow(self.svc, self.denial(),
                                        tool="c3_edit", op="write",
                                        path=str(self.ro)),
                          "a single-use grant must not survive its use")

    def test_other_session_cannot_spend_it(self):
        self.mint()
        other = _Svc(self.proj, session_id="sess-other")
        self.assertIsNone(_grants.allow(other, self.denial(), tool="c3_edit",
                                        op="write", path=str(self.ro)))
        self.assertEqual(self.uses_left(), [1], "grant must remain unspent")

    def test_other_path_cannot_spend_it(self):
        self.mint()
        other = self.proj / "docs" / "elsewhere.md"
        other.write_text("x", encoding="utf-8")
        d = ag.check(str(other), "write", str(self.proj))
        self.assertIsNone(_grants.allow(self.svc, d, tool="c3_edit",
                                        op="write", path=str(other)))
        self.assertEqual(self.uses_left(), [1])

    def test_policy_off_voids_a_live_grant(self):
        """§12.8 — policy is read before grants, so switching off is instant."""
        self.mint()
        self.write_config(access={"read_only": ["docs/**"]},
                          override={"enabled": False})
        self.assertIsNone(_grants.allow(self.svc, self.denial(),
                                        tool="c3_edit", op="write",
                                        path=str(self.ro)))
        self.assertEqual(self.uses_left(), [1], "voided, not consumed")

    def test_layer_off_voids_a_live_grant(self):
        self.mint()
        self.write_config(access={"read_only": ["docs/**"]},
                          override={"enabled": True,
                                    "layers": {"access_readonly": False}})
        self.assertIsNone(_grants.allow(self.svc, self.denial(),
                                        tool="c3_edit", op="write",
                                        path=str(self.ro)))
        self.assertEqual(self.uses_left(), [1])

    def test_none_denial_never_touches_the_store(self):
        """An allowed call must not spend a grant it never needed."""
        self.mint()
        self.assertIsNone(_grants.allow(self.svc, None, tool="c3_edit",
                                        op="write", path=str(self.ro)))
        self.assertEqual(self.uses_left(), [1])

    def test_store_failure_fails_closed(self):
        self.mint()
        with mock.patch.object(og, "gate_access", side_effect=OSError("boom")):
            self.assertIsNone(_grants.allow(self.svc, self.denial(),
                                            tool="c3_edit", op="write",
                                            path=str(self.ro)))


class TestSessionIdIsOneDefinition(unittest.TestCase):
    """The invariant three modules used to assert only in comments.

    A grant is minted under the id `c3_override` computes and consumed under
    the one `c3_edit` computes. If those ever diverge, every approval silently
    fails to apply — which is unobservable from the outside, because it looks
    exactly like "the user did not approve".
    """

    def test_edit_locks_override_all_delegate(self):
        from cli.tools import edit, locks, override
        svc = _Svc("/tmp/x", session_id="sess-shared")
        ids = {edit._session_id(svc), locks._session_id(svc),
               override._session_id(svc), _grants.session_id(svc)}
        self.assertEqual(ids, {"sess-shared"})

    def test_falls_back_to_pid_never_empty(self):
        blank = type("S", (), {"session_mgr": None})()
        self.assertTrue(_grants.session_id(blank).startswith("pid-"))


if __name__ == "__main__":
    unittest.main()
