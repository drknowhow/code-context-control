"""The CLAUDE.md updater agent must not churn a managed instruction doc.

Observed 2026-09-04 on the C3 repo itself: c3_artifacts history alternated
``install_mcp`` / ``scan`` on every session start. Two defects composed:

1. ``check_staleness`` diffed the project tree and tech stack against a doc
   that, in repo-map mode (default since 2.60.0), carries neither — so every
   detected technology was "not listed" and the doc was always stale.
2. The updater wrote ``generate()``'s block body with a raw ``write_text``,
   stripping the ``C3:BEGIN``/``C3:END`` markers and any user content around
   them; the next ``c3 install-mcp`` re-wrapped the template as a legacy doc.

These tests pin: no staleness in map mode; staleness still detected in
legacy mode; regeneration lands inside the block with user notes intact; an
installer-written doc survives an agent cycle byte for byte; whole-file
writes refuse to drop markers; every agent write is attributed.
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from services import claude_md as cm
from services.agents import ClaudeMdUpdaterAgent
from services.claude_md import (
    C3_BLOCK_BEGIN,
    C3_BLOCK_END,
    ClaudeMdManager,
    wrap_c3_block,
    write_c3_instruction_doc,
)

USER_HEAD = "# My notes\nkeep me above\n"
USER_TAIL = "# User Notes\nkeep me below\n"


def _session_mgr():
    """Just enough SessionManager for the legacy (embedded tree) paths."""
    return types.SimpleNamespace(
        _detect_tech_stack=lambda: "python, javascript",
        _scan_project_structure=lambda: "```\nsrc/\ndocs/\n```",
        current_session=None,
    )


class _Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (self.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, cfg: dict):
        (self.root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def manager(self):
        return ClaudeMdManager(str(self.root), _session_mgr(), None,
                               types.SimpleNamespace(facts=[]))

    def agent(self, mgr):
        return ClaudeMdUpdaterAgent(
            claude_md=mgr, memory=mgr.memory, session_mgr=mgr.session_mgr,
            watcher=types.SimpleNamespace(_handler=types.SimpleNamespace(change_count=3)),
            notifications=mock.MagicMock(), enabled=True, auto_apply=True)

    def doc(self):
        return self.root / "CLAUDE.md"

    def write_managed(self, body: str) -> str:
        """A managed doc with user notes above and below the block."""
        text = USER_HEAD + "\n" + wrap_c3_block(body) + "\n\n" + USER_TAIL
        self.doc().write_text(text, encoding="utf-8")
        return text


class TestStaleness(_Project):
    def test_map_mode_is_not_stale_without_tree_or_tech_stack(self):
        mgr = self.manager()
        self.write_managed(mgr.generate()["content"])
        result = mgr.check_staleness()
        self.assertEqual(result["status"], "ok", result)
        self.assertFalse([i for i in result["issues"] if "Tech Stack" in i["message"]])

    def test_legacy_mode_still_reports_drift(self):
        self.config({"map": {"enabled": False}})
        mgr = self.manager()
        # A doc that lists no tech stack and no tree: both diffs must fire.
        self.write_managed("## C3 Tools — MANDATORY\nUse c3_* tools.\n")
        result = mgr.check_staleness()
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("Tech Stack" in i["message"] for i in result["issues"]))


class TestUpdaterWrites(_Project):
    def test_installer_doc_survives_an_agent_cycle_in_map_mode(self):
        mgr = self.manager()
        before = self.write_managed(mgr.generate()["content"])
        self.agent(mgr).check()
        self.assertEqual(self.doc().read_text(encoding="utf-8"), before)

    def test_regeneration_lands_inside_the_block(self):
        self.config({"map": {"enabled": False}})
        mgr = self.manager()
        self.write_managed("## C3 Tools — MANDATORY\nstale body\n")
        self.agent(mgr).check()
        text = self.doc().read_text(encoding="utf-8")
        self.assertEqual(text.count(C3_BLOCK_BEGIN), 1)
        self.assertEqual(text.count(C3_BLOCK_END), 1)
        self.assertTrue(text.startswith(USER_HEAD.rstrip()), text[:80])
        self.assertTrue(text.rstrip().endswith(USER_TAIL.rstrip()), text[-80:])
        self.assertIn("# Project Context", text)
        self.assertNotIn("stale body", text)
        self.assertLess(text.index(C3_BLOCK_BEGIN), text.index("# Project Context"))
        self.assertLess(text.index("# Project Context"), text.index(C3_BLOCK_END))

    def test_second_cycle_is_a_no_op(self):
        self.config({"map": {"enabled": False}})
        mgr = self.manager()
        self.write_managed("## C3 Tools — MANDATORY\nstale body\n")
        agent = self.agent(mgr)
        agent.check()
        once = self.doc().read_text(encoding="utf-8")
        agent._last_action_hash = ""  # forget the dedupe; the doc must stand on its own
        agent.check()
        self.assertEqual(self.doc().read_text(encoding="utf-8"), once)

    def test_whole_file_write_refuses_to_drop_markers(self):
        mgr = self.manager()
        before = self.write_managed("body")
        agent = self.agent(mgr)
        self.assertFalse(agent._write_claude_md("a block body with no markers"))
        self.assertEqual(self.doc().read_text(encoding="utf-8"), before)
        self.assertTrue(agent._write_claude_md(before.replace("body", "body2")))
        self.assertIn("body2", self.doc().read_text(encoding="utf-8"))

    def test_writes_are_attributed_to_the_agent(self):
        mgr = self.manager()
        before = self.write_managed("body")
        agent = self.agent(mgr)
        with mock.patch("services.artifact_defs.note_pending_write") as note:
            agent._write_managed_body("new body")
            agent._write_claude_md(before)
        sources = [call.args[2] for call in note.call_args_list]
        self.assertEqual(sources, ["claude_md_updater", "claude_md_updater"])

    def test_installer_source_is_the_default(self):
        with mock.patch("services.artifact_defs.note_pending_write") as note:
            write_c3_instruction_doc(self.doc(), "body", project_path=self.root)
        self.assertEqual(note.call_args.args[2], "install_mcp")
        self.assertIs(cm.write_c3_instruction_doc, write_c3_instruction_doc)


if __name__ == "__main__":
    unittest.main()
