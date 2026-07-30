"""Tests for per-IDE adaptation of the generated C3 workflow.

The shared workflow is written for Claude Code, which enforces the c3-first
rule with PreToolUse hooks and has /clear. Every other IDE has neither, so the
generated instruction doc must restate the mandate honestly (a "you are
blocked" line the agent can disprove undermines the whole document) and must
not leave a hole in the numbered workflow where an unsupported step was cut.
"""
import re
import unittest

from services.claude_md import (
    C3_COMPACT_WORKFLOW,
    C3_NANO_WORKFLOW,
    NO_HOOK_HEADER,
    VSCODE_INSTRUCTIONS_FILE,
    VSCODE_SESSION_INIT,
    adapt_workflow_for_ide,
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
