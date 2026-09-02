"""Tests for per-IDE adaptation of the generated C3 workflow.

The shared workflow is written for Claude Code, which enforces the c3-first
rule with PreToolUse hooks and has /clear. Every other IDE has neither, so the
generated instruction doc must restate the mandate honestly (a "you are
blocked" line the agent can disprove undermines the whole document) and must
not leave a hole in the numbered workflow where an unsupported step was cut.
"""
import re
import tempfile
import unittest
from pathlib import Path

from services.claude_md import (
    C3_BLOCK_BEGIN,
    C3_COMPACT_WORKFLOW,
    C3_NANO_WORKFLOW,
    NO_HOOK_HEADER,
    VSCODE_INSTRUCTIONS_FILE,
    VSCODE_SESSION_INIT,
    adapt_workflow_for_ide,
    write_c3_instruction_doc,
)


def _hookless(workflow=C3_COMPACT_WORKFLOW, **kw):
    return adapt_workflow_for_ide(workflow, supports_hooks=False,
                                  supports_clear=False, **kw)


class TestHooklessAdaptation(unittest.TestCase):
    def test_claude_code_workflow_is_unchanged(self):
        self.assertEqual(
            adapt_workflow_for_ide(C3_COMPACT_WORKFLOW,
                                   supports_hooks=True, supports_clear=True),
            C3_COMPACT_WORKFLOW,
        )

    def test_no_hook_ide_does_not_claim_tools_are_blocked(self):
        adapted = _hookless()
        self.assertIn(NO_HOOK_HEADER, adapted)
        self.assertNotIn("enforced by hooks", adapted)
        self.assertNotIn("blocked by PreToolUse hooks", adapted)
        # The mandate itself must survive the restatement.
        self.assertIn("MANDATORY", adapted)
        self.assertIn("must NOT be used before a c3_* tool", adapted)

    def test_nano_workflow_gets_the_short_restatement(self):
        adapted = _hookless(C3_NANO_WORKFLOW, nano=True)
        self.assertIn(NO_HOOK_HEADER, adapted)
        self.assertNotIn("BLOCKED", adapted)


class TestUnsupportedClauseTrimming(unittest.TestCase):
    def test_log_step_survives_without_clear_support(self):
        adapted = _hookless()
        log_lines = [l for l in adapted.splitlines() if l.startswith("8.")]
        self.assertEqual(len(log_lines), 1, "step 8 must not be dropped wholesale")
        self.assertIn("c3_session(action='log')", log_lines[0])
        self.assertNotIn("snapshot", log_lines[0])
        self.assertNotIn("/clear", log_lines[0])

    def test_numbered_steps_have_no_gaps(self):
        adapted = _hookless()
        numbers = [int(m.group(1)) for m in
                   (re.match(r"^(\d+)\.\s+\*\*", l) for l in adapted.splitlines())
                   if m]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers, list(range(1, max(numbers) + 1)),
                         "a cut step must not leave a hole in the numbering")

    def test_blank_line_structure_is_preserved(self):
        adapted = _hookless()
        self.assertIn("\n\n## Workflow (follow this order", adapted)
        self.assertIn("\n\n## Anti-patterns", adapted)


class TestVSCodeSessionInit(unittest.TestCase):
    def test_copilot_doc_opens_with_the_tool_load_step(self):
        from cli.c3 import _COPILOT_INSTRUCTIONS_CONTENT

        self.assertTrue(_COPILOT_INSTRUCTIONS_CONTENT.startswith(VSCODE_SESSION_INIT))
        self.assertIn("tool_search_tool_regex", _COPILOT_INSTRUCTIONS_CONTENT)
        self.assertIn("^mcp_c3_", _COPILOT_INSTRUCTIONS_CONTENT)
        self.assertNotIn("enforced by hooks", _COPILOT_INSTRUCTIONS_CONTENT)

    def test_agents_md_is_hookless_but_has_no_vscode_step(self):
        from cli.c3 import _AGENTS_MD_CONTENT

        self.assertNotIn("enforced by hooks", _AGENTS_MD_CONTENT)
        self.assertNotIn("tool_search_tool_regex", _AGENTS_MD_CONTENT)
        self.assertIn("[mcp_servers.c3]", _AGENTS_MD_CONTENT)

    def test_claude_md_keeps_hook_enforcement(self):
        from cli.c3 import _CLAUDE_MD_CONTENT

        self.assertIn("enforced by hooks", _CLAUDE_MD_CONTENT)
        self.assertNotIn("tool_search_tool_regex", _CLAUDE_MD_CONTENT)


class TestHookOnlyClaimsAreRestated(unittest.TestCase):
    """v2.100.0 shipped "WRITES to these files PAUSE by default" into every
    instruction doc, including the ones for IDEs where no hook intercepts a
    native write. That is the same disprovable-line problem the header and
    lede already avoid, one paragraph lower down."""

    def test_hookless_docs_do_not_claim_native_writes_are_intercepted(self):
        adapted = _hookless()
        self.assertIn("agent-config", adapted.lower())
        self.assertNotIn("native writes via hooks", adapted)
        self.assertIn("no PreToolUse hooks", adapted)

    def test_claude_code_keeps_the_full_claim(self):
        self.assertIn("native writes via hooks", C3_COMPACT_WORKFLOW)

    def test_both_docs_still_teach_the_confirm_flow(self):
        for doc in (C3_COMPACT_WORKFLOW, _hookless()):
            self.assertIn("CONFIRM HOLDS", doc)
            self.assertIn("[c3-access:confirm]", doc)
            self.assertIn("timeout_s=180", doc)

    def test_the_nano_workflow_teaches_it_too(self):
        # Nano is what a line-limited IDE gets; a hold it has never heard of
        # reads as an unexplained refusal.
        self.assertIn("[c3-access:confirm]", C3_NANO_WORKFLOW)


class TestManagedDocsStayInSync(unittest.TestCase):
    """`_ensure_instruction_workflow` treated marker PRESENCE as up to date,
    and `.github/copilot-instructions.md` was missing from the managed-block
    sync list — so the third git-tracked instruction doc silently drifted a
    release behind on every template change."""

    def _project(self, td):
        root = Path(td)
        (root / ".c3").mkdir()
        return root, root / ".github" / "copilot-instructions.md"

    #: The step heading, not the bare phrase: item 12 cross-references
    #: "the CONFIRM HOLDS flow above", so the phrase alone is present even
    #: in a doc whose step 11.6 has been doctored away.
    STEP = "11.6. **CONFIRM HOLDS**"

    def _stale(self):
        """A doc an OLDER C3 generated: managed markers, every marker the
        presence check looks for, and a body missing a current step."""
        from cli.c3 import _COPILOT_INSTRUCTIONS_CONTENT

        return _COPILOT_INSTRUCTIONS_CONTENT.replace(
            self.STEP, "11.6. **SOMETHING ELSE**")

    def test_copilot_doc_is_in_the_managed_sync_list(self):
        from cli.c3 import _instruction_documents_to_generate

        names = [name for name, _ in _instruction_documents_to_generate()]
        self.assertIn(VSCODE_INSTRUCTIONS_FILE, names)

    def test_a_stale_managed_doc_is_regenerated_not_kept(self):
        from cli.c3 import _ensure_vscode_instructions_workflow

        with tempfile.TemporaryDirectory() as td:
            root, doc = self._project(td)
            write_c3_instruction_doc(doc, self._stale(), project_path=str(root))
            self.assertNotIn(self.STEP, doc.read_text(encoding="utf-8"))

            state = _ensure_vscode_instructions_workflow(doc, str(root))
            self.assertEqual(state, "updated")
            self.assertIn(self.STEP, doc.read_text(encoding="utf-8"))

    def test_an_up_to_date_doc_is_kept(self):
        from cli.c3 import _COPILOT_INSTRUCTIONS_CONTENT, _ensure_vscode_instructions_workflow

        with tempfile.TemporaryDirectory() as td:
            root, doc = self._project(td)
            write_c3_instruction_doc(doc, _COPILOT_INSTRUCTIONS_CONTENT,
                                     project_path=str(root))
            self.assertEqual(
                _ensure_vscode_instructions_workflow(doc, str(root)), "kept")

    def test_user_content_outside_the_block_survives_a_refresh(self):
        from cli.c3 import _ensure_vscode_instructions_workflow

        with tempfile.TemporaryDirectory() as td:
            root, doc = self._project(td)
            write_c3_instruction_doc(doc, self._stale(), project_path=str(root))
            doc.write_text(
                doc.read_text(encoding="utf-8")
                + "\n# Team Notes\n\nAsk before touching infra.\n",
                encoding="utf-8")

            _ensure_vscode_instructions_workflow(doc, str(root))
            text = doc.read_text(encoding="utf-8")
            self.assertIn("Ask before touching infra.", text)
            self.assertIn(self.STEP, text)
            self.assertEqual(text.count(C3_BLOCK_BEGIN), 1)

    def test_a_legacy_markerless_doc_still_takes_the_prepend_path(self):
        from cli.c3 import _ensure_vscode_instructions_workflow

        with tempfile.TemporaryDirectory() as td:
            root, doc = self._project(td)
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# My own notes\n", encoding="utf-8")
            self.assertEqual(
                _ensure_vscode_instructions_workflow(doc, str(root)), "updated")
            text = doc.read_text(encoding="utf-8")
            self.assertIn("My own notes", text)
            self.assertIn("Existing Project Instructions", text)


class TestGeneratorWiring(unittest.TestCase):
    """ClaudeMdManager must apply the same adaptation it hands to the CLI."""

    def _manager(self, **kw):
        from services.claude_md import ClaudeMdManager

        return ClaudeMdManager(project_path=".", session_mgr=None, indexer=None,
                               memory=None, **kw)

    def test_vscode_profile_prepends_session_init(self):
        mgr = self._manager(instructions_file=VSCODE_INSTRUCTIONS_FILE,
                            supports_hooks=False, supports_clear=False)
        workflow = mgr._build_c3_workflow()
        self.assertTrue(workflow.startswith(VSCODE_SESSION_INIT))
        self.assertNotIn("enforced by hooks", workflow)

    def test_claude_code_profile_is_untouched(self):
        mgr = self._manager(instructions_file="CLAUDE.md",
                            supports_hooks=True, supports_clear=True)
        workflow = mgr._build_c3_workflow()
        self.assertEqual(workflow, C3_COMPACT_WORKFLOW)


if __name__ == "__main__":
    unittest.main()
