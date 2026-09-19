"""The agent's half of the Sessions view: c3_session list / stale / unstale /
note, and c3_task's ``session`` link (2.143.0).

WHAT THESE PIN.
1. ``target='current'`` resolves to THIS conversation's host id — from the
   session manager, else from the hooks' enforcement state.
2. ``stale`` demands a reason (a flag nobody can explain is noise), records
   ``by: agent`` and who marked it, and warns when the target is live.
3. ``list`` returns titles and flags only — never other sessions' prompts.
4. A task can link to a session, ``current`` included.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.tools.session import handle_session  # noqa: E402
from services import session_catalog as sc  # noqa: E402
from services.task_store import TaskStore  # noqa: E402
from tests.session_fixtures import U1, U2, SessionFixture  # noqa: E402


def _finalize(name, args, resp, summ, **kw):
    return resp


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = SessionFixture(Path(self._tmp.name))
        self._env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.fx.claude)})
        self._env.start()
        self._git = mock.patch.object(sc, "_local_branches", return_value=None)
        self._git.start()
        self.proj = self.fx.project("proj")
        self.fx.transcript(self.proj, U1, title="Old approach", prompt="SECRET-ish prompt text")
        self.fx.transcript(self.proj, U2, title="New approach")
        self.svc = SimpleNamespace(project_path=str(self.proj),
                                   session_mgr=SimpleNamespace(
                                       current_session={"id": "c3", "host_session_id": U2}))

    def tearDown(self):
        self._git.stop()
        self._env.stop()
        self._tmp.cleanup()

    def call(self, action, data="", reasoning="", target=""):
        return handle_session(action, data, reasoning, "", "", "auto", self.svc, _finalize,
                              target=target)


class TestSessionTool(_Base):
    def test_list_shows_titles_flags_and_this_session_only(self):
        out = self.call("list")
        self.assertIn("Old approach", out)
        self.assertIn("(this session)", out)
        self.assertNotIn("SECRET-ish", out)
        self.assertIn("[session:error]", self.call("list", target="bogus"))

    def test_stale_needs_target_and_reason(self):
        self.assertIn("target is required", self.call("stale", reasoning="x"))
        self.assertIn("reasoning is required", self.call("stale", target=U1[:8]))

    def test_stale_with_current_as_successor(self):
        out = self.call("stale", data="current", reasoning="superseded", target=U1[:8])
        self.assertIn("[session:stale]", out)
        marks = [json.loads(line) for line in
                 (self.proj / ".c3" / "session_marks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(marks[-1]["id"], U1)
        self.assertEqual(marks[-1]["successor"], U2)
        self.assertEqual((marks[-1]["by"], marks[-1]["by_session"]), ("agent", U2))
        self.assertIn("STALE: superseded", self.call("list", target="stale"))
        self.assertIn("[session:unstale]", self.call("unstale", target=U1))
        self.assertNotIn("STALE", self.call("list", target="all"))

    def test_stale_on_a_live_session_warns(self):
        self.fx.heartbeat(self.proj, "c3x", U1)
        out = self.call("stale", reasoning="abandoned", target=U1)
        self.assertIn("[session:warn]", out)

    def test_note_defaults_to_current(self):
        out = self.call("note", data="Hub tab done", reasoning="Desk next")
        self.assertIn("[session:note]", out)
        row = next(r for r in sc.list_sessions(self.proj, stale="all")["sessions"]
                   if r["id"] == U2)
        self.assertEqual(row["note"]["summary"], "Hub tab done")

    def test_current_falls_back_to_enforcement_state(self):
        self.svc.session_mgr.current_session = {"id": "c3"}
        (self.proj / ".c3" / "enforcement_state.json").write_text(
            json.dumps({"session_id": U1}), encoding="utf-8")
        self.call("note", data="from the hook's id")
        row = next(r for r in sc.list_sessions(self.proj, stale="all")["sessions"]
                   if r["id"] == U1)
        self.assertEqual(row["note"]["summary"], "from the hook's id")

    def test_note_without_a_known_id_asks_for_target(self):
        self.svc.session_mgr.current_session = {}
        self.assertIn("pass target", self.call("note", data="x"))


class TestTaskSessionLink(_Base):
    def test_link_current_session(self):
        from cli.tools.tasks import handle_task
        store = TaskStore(str(self.proj))
        task = store.create_task("Ship sessions")
        self.svc.task_store = store
        self.svc.hybrid_config = {}
        out = handle_task("link", self.svc, lambda n, a, r, s, **k: r,
                          task_id=task["id"], link_type="session", ref="current")
        self.assertIn("[task:linked]", out)
        links = store.get_task(task["id"])["links"]
        self.assertEqual(links, [{"type": "session", "ref": U2, "label": ""}])
        row = next(r for r in sc.list_sessions(self.proj, stale="all")["sessions"]
                   if r["id"] == U2)
        self.assertEqual(row["links"]["tasks"], 1)


if __name__ == "__main__":
    unittest.main()
