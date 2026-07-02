"""Tests for the Oracle bridge's c3_project / c3_artifacts wrappers (Wave 2).

Pins the read-only contract: blocked actions return errors WITHOUT touching
the underlying handlers, allowed actions proxy with a resolved+validated path
and allow_write hard-coded False, unregistered on-disk .c3 paths are rejected
(resolve_project accepts them; Oracle must not), and the bridge's blocked
memory set stays in lockstep with cli.tools.project._MEMORY_WRITE.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.tools.artifacts as artifacts_mod  # noqa: E402
import cli.tools.project as project_mod  # noqa: E402
from oracle.services import c3_bridge as cb  # noqa: E402
from oracle.services.c3_bridge import C3Bridge  # noqa: E402


class _StubScanner:
    def __init__(self, paths):
        self._paths = paths

    def discover(self, force=False):
        return [{"path": p, "has_c3": True} for p in self._paths]


def _mkproject(tmp: str, name: str) -> str:
    d = Path(tmp) / name
    (d / ".c3").mkdir(parents=True)
    return str(d.resolve())


class _BridgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registered = _mkproject(self.tmp.name, "known")
        self.unregistered = _mkproject(self.tmp.name, "rogue")
        self.bridge = C3Bridge(scanner=_StubScanner([self.registered]))

        # Any call into the real handlers is a test failure unless the test
        # explicitly swaps in a recorder.
        self._project_calls: list[dict] = []
        self._artifact_calls: list[dict] = []

        def recording_handle_project(action, svc, finalize, **kw):
            self._project_calls.append({"action": action, "svc": svc, **kw})
            return f"[c3_project:ok] {action}"

        def recording_handle_artifacts(action, svc, finalize, **kw):
            self._artifact_calls.append({"action": action, "svc": svc, **kw})
            return f"[artifacts:ok] {action}"

        for patcher in (
            mock.patch.object(project_mod, "handle_project", recording_handle_project),
            mock.patch.object(artifacts_mod, "handle_artifacts", recording_handle_artifacts),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        # get_runtime would build a real C3Runtime; artifacts tests only need
        # an opaque svc object handed to the handler.
        self._fake_runtime = object()
        patcher = mock.patch.object(
            C3Bridge, "get_runtime",
            lambda _self, p: self._fake_runtime
            if str(Path(p).resolve()) == self.registered
            else (_ for _ in ()).throw(ValueError(f"Unknown project: {p}")),
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestC3ProjectWrapper(_BridgeBase):
    def test_blocked_actions_never_reach_handler(self):
        for action in ("edit", "shell", "register", "unregister", "scan",
                       "sub_add", "sub_remove", "sub_cascade", "filter", "bogus"):
            with self.subTest(action=action):
                out = self.bridge.c3_project(action, project=self.registered)
                self.assertIn("error", out)
        self.assertEqual(self._project_calls, [])

    def test_blocked_memory_actions_never_reach_handler(self):
        for mem_action in sorted(cb._BLOCKED_MEMORY_ACTIONS):
            out = self.bridge.c3_project("memory", project=self.registered,
                                         mem_action=mem_action)
            self.assertIn("error", out)
        self.assertEqual(self._project_calls, [])

    def test_blocked_edits_and_status_variants(self):
        self.assertIn("error", self.bridge.c3_project(
            "edits", project=self.registered, edits_action="log"))
        self.assertIn("error", self.bridge.c3_project(
            "status", project=self.registered, view="ghost_files"))
        self.assertEqual(self._project_calls, [])

    def test_allowed_action_proxies_with_allow_write_false(self):
        out = self.bridge.c3_project("search", project=self.registered, query="q")
        self.assertEqual(out["project"], self.registered)
        call = self._project_calls[-1]
        self.assertEqual(call["action"], "search")
        self.assertIs(call["allow_write"], False)
        self.assertIsNone(call["svc"])
        self.assertEqual(call["project"], self.registered)  # resolved path

    def test_list_needs_no_project(self):
        out = self.bridge.c3_project("list")
        self.assertEqual(out["project"], "all")
        self.assertEqual(self._project_calls[-1]["action"], "list")

    def test_unregistered_on_disk_c3_path_rejected(self):
        # resolve_project accepts ANY on-disk .c3 folder; Oracle must re-check
        # membership against discovered projects before proxying.
        with self.assertRaises(ValueError):
            self.bridge.c3_project("search", project=self.unregistered, query="q")
        self.assertEqual(self._project_calls, [])

    def test_blocked_memory_set_matches_project_tool(self):
        # If cli.tools.project grows a new memory write verb, the Oracle
        # blocklist must grow with it — pin the two sets together.
        self.assertEqual(cb._BLOCKED_MEMORY_ACTIONS, project_mod._MEMORY_WRITE)


class TestC3ArtifactsWrapper(_BridgeBase):
    def test_scan_and_restore_blocked_without_runtime(self):
        for action in ("scan", "restore"):
            out = self.bridge.c3_artifacts(self.registered, action=action)
            self.assertIn("error", out)
        self.assertEqual(self._artifact_calls, [])

    def test_read_actions_proxy_through_runtime(self):
        out = self.bridge.c3_artifacts(self.registered, action="list", cls="settings")
        self.assertEqual(out["project"], self.registered)
        call = self._artifact_calls[-1]
        self.assertEqual(call["action"], "list")
        self.assertIs(call["svc"], self._fake_runtime)
        self.assertEqual(call["cls"], "settings")

    def test_unregistered_path_rejected(self):
        with self.assertRaises(ValueError):
            self.bridge.c3_artifacts(self.unregistered, action="list")
        self.assertEqual(self._artifact_calls, [])


if __name__ == "__main__":
    unittest.main()
