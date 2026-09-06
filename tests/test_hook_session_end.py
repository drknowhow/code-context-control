"""D0b (v2.126.0): SessionStart / SessionEnd hooks + the host↔C3 join key.

Before this, the activity log recorded a session's start (via the MCP
server) and effectively never its end (14 ``session_save`` rows against 525
``session_start`` rows in this repo), and the C3 session id never met the
Claude Code UUID the hooks carry. These tests pin:

- ``cli.hook_session_open`` writes ``session_open`` + a ``session``
  notification (``ref_id`` = host id); ``source: compact`` writes nothing;
- ``cli.hook_session_end`` writes ``session_end`` carrying BOTH ids when the
  MCP server left a link file, and the notification ``Session ended <8>``;
- the dispatcher routes ``start`` / ``end`` to them for every host (Codex
  keeps its lifecycle hook first) — the exact route lists are pinned here
  and in tests/test_hook_dispatch.py on purpose;
- the dispatcher subprocess accepts the events end to end and prints nothing
  (SessionStart stdout would become model context);
- ``c3 install-mcp`` registers ``SessionStart`` and ``SessionEnd`` for
  Claude Code with the same merge discipline as ``Stop``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import (  # noqa: E402
    hook_dispatch,
    hook_session_end,
    hook_session_open,
)
from cli._hook_utils import HOST_CLAUDE, HOST_CODEX, HOST_GEMINI  # noqa: E402
from services.host_sessions import link_path, write_link  # noqa: E402

HOST_SID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
C3_SID = "20260906_101500_deadbeef0123"


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        (self.project / ".c3").mkdir(parents=True)
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"hybrid": {"ollama_base_url": "http://127.0.0.1:9"}}),
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def activity(self) -> list[dict]:
        return _rows(self.project / ".c3" / "activity_log.jsonl")

    def notifications(self) -> list[dict]:
        return _rows(self.project / ".c3" / "notifications.jsonl")


class TestSessionOpen(_Base):
    def _payload(self, source="startup", **extra):
        return {"session_id": HOST_SID, "cwd": str(self.project),
                "hook_event_name": "SessionStart", "source": source, **extra}

    def test_writes_session_open_row_and_notification(self):
        out = hook_session_open.run(self._payload(), self.project)
        self.assertIsNone(out)                       # stdout would be model context
        rows = [r for r in self.activity() if r["type"] == "session_open"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host_session_id"], HOST_SID)
        self.assertEqual(rows[0]["source"], "claude")
        self.assertEqual(rows[0]["start_source"], "startup")
        ntf = self.notifications()
        self.assertEqual(len(ntf), 1)
        self.assertEqual(ntf[0]["kind"], "session")
        self.assertEqual(ntf[0]["ref_id"], HOST_SID)
        self.assertEqual(ntf[0]["severity"], "info")
        self.assertEqual(ntf[0]["title"], f"Session started {HOST_SID[:8]}")
        self.assertEqual(ntf[0]["agent"], "session")

    def test_compact_writes_nothing(self):
        hook_session_open.run(self._payload(source="compact"), self.project)
        self.assertEqual(self.activity(), [])
        self.assertEqual(self.notifications(), [])

    def test_resume_and_clear_are_recorded(self):
        for src in ("resume", "clear"):
            hook_session_open.run(self._payload(source=src, session_id=f"{src}-sid"), self.project)
        starts = {r["start_source"] for r in self.activity() if r["type"] == "session_open"}
        self.assertEqual(starts, {"resume", "clear"})

    def test_finds_project_from_cwd_subdirectory(self):
        sub = self.project / "src" / "pkg"
        sub.mkdir(parents=True)
        payload = self._payload()
        payload["cwd"] = str(sub)
        hook_session_open.run(payload, None)
        self.assertEqual(len([r for r in self.activity() if r["type"] == "session_open"]), 1)

    def test_no_project_is_a_noop(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        self.assertIsNone(hook_session_open.run(
            {"session_id": HOST_SID, "cwd": str(outside), "source": "startup"}, None))
        self.assertFalse((outside / ".c3").exists())

    def test_notification_failure_does_not_lose_the_row(self):
        from services.notifications import NotificationStore
        with mock.patch.object(NotificationStore, "add", side_effect=OSError("locked")):
            hook_session_open.run(self._payload(), self.project)
        self.assertEqual(len([r for r in self.activity() if r["type"] == "session_open"]), 1)
        self.assertEqual(self.notifications(), [])


class TestSessionEnd(_Base):
    def _payload(self, reason="prompt_input_exit", **extra):
        return {"session_id": HOST_SID, "cwd": str(self.project),
                "hook_event_name": "SessionEnd", "reason": reason, **extra}

    def test_writes_session_end_with_both_ids_when_linked(self):
        # The MCP server wrote the link at session start (services.host_sessions).
        self.assertIsNotNone(write_link(self.project, "claude-code", HOST_SID, C3_SID))
        self.assertTrue(link_path(self.project, "claude", HOST_SID).is_file())
        self.assertIsNone(hook_session_end.run(self._payload(), self.project))
        rows = [r for r in self.activity() if r["type"] == "session_end"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], C3_SID)
        self.assertEqual(rows[0]["host_session_id"], HOST_SID)
        self.assertEqual(rows[0]["reason"], "prompt_input_exit")
        self.assertEqual(rows[0]["source"], "claude")
        ntf = self.notifications()
        self.assertEqual(len(ntf), 1)
        self.assertEqual(ntf[0]["kind"], "session")
        self.assertEqual(ntf[0]["ref_id"], HOST_SID)
        self.assertEqual(ntf[0]["severity"], "info")
        self.assertEqual(ntf[0]["title"], f"Session ended {HOST_SID[:8]}")
        self.assertIn(C3_SID, ntf[0]["message"])

    def test_unlinked_session_end_has_empty_c3_id(self):
        hook_session_end.run(self._payload(reason="logout"), self.project)
        rows = [r for r in self.activity() if r["type"] == "session_end"]
        self.assertEqual(rows[0]["session_id"], "")
        self.assertEqual(rows[0]["host_session_id"], HOST_SID)
        self.assertEqual(rows[0]["reason"], "logout")

    def test_link_for_another_host_session_is_not_used(self):
        write_link(self.project, "claude", "some-other-uuid", "20260101_000000_other")
        hook_session_end.run(self._payload(), self.project)
        rows = [r for r in self.activity() if r["type"] == "session_end"]
        self.assertEqual(rows[0]["session_id"], "")

    def test_two_sessions_end_as_two_notifications(self):
        hook_session_end.run(self._payload(session_id="aaaa1111-x"), self.project)
        hook_session_end.run(self._payload(session_id="bbbb2222-y"), self.project)
        titles = sorted(n["title"] for n in self.notifications())
        self.assertEqual(titles, ["Session ended aaaa1111", "Session ended bbbb2222"])

    def test_codex_payload_is_recorded_under_the_codex_host(self):
        payload = {"session_id": "thread-1", "cwd": str(self.project),
                   "hook_event_name": "SessionEnd", "_c3_host": "codex"}
        write_link(self.project, "codex", "thread-1", C3_SID)
        hook_session_end.run(payload, self.project)
        rows = [r for r in self.activity() if r["type"] == "session_end"]
        self.assertEqual(rows[0]["source"], "codex")
        self.assertEqual(rows[0]["session_id"], C3_SID)


class TestDispatcherRouting(unittest.TestCase):
    """The route lists are pinned EXACTLY (see tests/test_hook_dispatch.py)."""

    def _routes(self, event, host):
        return list(hook_dispatch._routes(event, "", "", host))

    def test_start_and_end_route_for_every_host(self):
        for host in (HOST_CLAUDE, HOST_GEMINI):
            self.assertEqual(self._routes("start", host), ["hook_session_open"])
            self.assertEqual(self._routes("end", host), ["hook_session_end"])
            self.assertEqual(self._routes("compact", host), [])

    def test_codex_keeps_its_lifecycle_hook_first(self):
        self.assertEqual(self._routes("start", HOST_CODEX),
                         ["hook_codex_lifecycle", "hook_session_open"])
        self.assertEqual(self._routes("end", HOST_CODEX),
                         ["hook_codex_lifecycle", "hook_session_end"])
        self.assertEqual(self._routes("compact", HOST_CODEX), ["hook_codex_lifecycle"])


class TestDispatcherEndToEnd(_Base):
    def _dispatch(self, event: str, payload: dict) -> str:
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "cli" / "hook_dispatch.py"), event,
             "--project", str(self.project)],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            cwd=str(self.project), encoding="utf-8")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_start_then_end_through_the_dispatcher_prints_nothing(self):
        out = self._dispatch("start", {"session_id": HOST_SID, "cwd": str(self.project),
                                       "hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual(out.strip(), "")
        out = self._dispatch("end", {"session_id": HOST_SID, "cwd": str(self.project),
                                     "hook_event_name": "SessionEnd", "reason": "other"})
        self.assertEqual(out.strip(), "")
        types = [r["type"] for r in self.activity()]
        self.assertEqual(types, ["session_open", "session_end"])
        kinds = [n["kind"] for n in self.notifications()]
        self.assertEqual(kinds, ["session", "session"])
        self.assertEqual({n["ref_id"] for n in self.notifications()}, {HOST_SID})


class TestInstallerRegistersLifecycleHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        self._saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.root)
        os.environ["USERPROFILE"] = str(self.root)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _install(self):
        from cli.c3 import cmd_install_mcp
        with mock.patch("shutil.which", return_value=None):
            cmd_install_mcp(SimpleNamespace(
                project_path=str(self.project), ide="claude", mcp_mode="direct"))
        return json.loads(
            (self.project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))

    @staticmethod
    def _cmds(hooks: dict, event: str) -> list[str]:
        return [hk.get("command", "") for h in hooks.get(event, []) for hk in h.get("hooks", [])]

    def test_session_start_and_end_are_registered(self):
        hooks = self._install()["hooks"]
        start = self._cmds(hooks, "SessionStart")
        end = self._cmds(hooks, "SessionEnd")
        self.assertEqual(len(start), 1)
        self.assertEqual(len(end), 1)
        self.assertTrue("hook_dispatch.py" in start[0] and start[0].endswith(" start"), start)
        self.assertTrue("hook_dispatch.py" in end[0] and end[0].endswith(" end"), end)
        self.assertEqual([h.get("matcher") for h in hooks["SessionStart"]], [""])

    def test_reinstall_is_idempotent_and_keeps_user_hooks(self):
        claude_dir = self.project / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.local.json").write_text(json.dumps({
            "hooks": {"SessionEnd": [{"matcher": "", "hooks": [
                {"type": "command", "command": "echo user-end"}]}]},
        }), encoding="utf-8")
        hooks = self._install()["hooks"]
        hooks = self._install()["hooks"]                # second run: no duplicate
        end = self._cmds(hooks, "SessionEnd")
        self.assertEqual(sum(1 for c in end if c.endswith(" end")), 1)
        self.assertIn("echo user-end", end)
        self.assertEqual(sum(1 for c in self._cmds(hooks, "SessionStart") if c.endswith(" start")), 1)

    def test_uninstall_strips_them(self):
        from cli.c3 import _c3_references_in_settings, _strip_c3_from_settings
        settings = self._install()
        refs = _c3_references_in_settings(settings)
        self.assertIn("hooks.SessionStart", refs)
        self.assertIn("hooks.SessionEnd", refs)
        stripped = _strip_c3_from_settings(settings)
        self.assertNotIn("SessionStart", stripped.get("hooks", {}))
        self.assertNotIn("SessionEnd", stripped.get("hooks", {}))


if __name__ == "__main__":
    unittest.main()
