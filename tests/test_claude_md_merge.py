"""Tests for non-destructive C3 instruction-doc merging.

Covers wrap_c3_block / merge_c3_block / write_c3_instruction_doc — the helpers
that let `c3 install-mcp` / `c3 init` regenerate CLAUDE.md (and AGENTS.md /
GEMINI.md) without clobbering user-written content. Mirrors the behaviour of
the global ~/.claude/CLAUDE.md merge.
"""
import tempfile
import unittest
from pathlib import Path

from services.claude_md import (
    C3_BLOCK_BEGIN,
    C3_BLOCK_END,
    C3_BLOCK_HEADING,
    ClaudeMdManager,
    merge_c3_block,
    wrap_c3_block,
    write_c3_instruction_doc,
)

C3_CONTENT = "## C3 Tools — MANDATORY\nUse c3_* tools.\n\n# Project Context\nstuff"


class TestWrapC3Block(unittest.TestCase):
    def test_wrap_adds_markers_and_heading(self):
        block = wrap_c3_block(C3_CONTENT)
        self.assertTrue(block.startswith(C3_BLOCK_BEGIN))
        self.assertTrue(block.rstrip().endswith(C3_BLOCK_END))
        self.assertIn(C3_BLOCK_HEADING, block)
        self.assertIn("## C3 Tools — MANDATORY", block)


class TestMergeC3Block(unittest.TestCase):
    def test_replaces_marked_region_in_place(self):
        existing = (
            "# My personal header\nkeep me above\n\n"
            + wrap_c3_block("OLD C3 BODY")
            + "\n\n# My footer\nkeep me below\n"
        )
        merged = merge_c3_block(existing, wrap_c3_block("NEW C3 BODY"))
        self.assertIn("# My personal header", merged)
        self.assertIn("keep me above", merged)
        self.assertIn("# My footer", merged)
        self.assertIn("keep me below", merged)
        self.assertIn("NEW C3 BODY", merged)
        self.assertNotIn("OLD C3 BODY", merged)
        # Exactly one managed block remains (no duplication).
        self.assertEqual(merged.count(C3_BLOCK_BEGIN), 1)
        self.assertEqual(merged.count(C3_BLOCK_END), 1)

    def test_legacy_doc_is_replaced_and_user_notes_preserved(self):
        legacy = (
            "## C3 Tools — MANDATORY\nold workflow text\n\n"
            "# Project Context\nold context\n\n"
            "# User Notes\nmy precious notes\n"
        )
        merged = merge_c3_block(legacy, wrap_c3_block(C3_CONTENT))
        self.assertIn("# User Notes", merged)
        self.assertIn("my precious notes", merged)
        self.assertNotIn("old workflow text", merged)
        self.assertNotIn("old context", merged)
        self.assertEqual(merged.count(C3_BLOCK_BEGIN), 1)

    def test_genuine_user_file_is_appended_not_destroyed(self):
        user_file = "# Our House Rules\nAlways write tests.\nNo force pushes.\n"
        merged = merge_c3_block(user_file, wrap_c3_block(C3_CONTENT))
        self.assertIn("# Our House Rules", merged)
        self.assertIn("Always write tests.", merged)
        self.assertIn("No force pushes.", merged)
        self.assertIn(C3_BLOCK_BEGIN, merged)
        # User content stays above the appended C3 block.
        self.assertLess(merged.index("Our House Rules"), merged.index(C3_BLOCK_BEGIN))


class TestWriteC3InstructionDoc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "CLAUDE.md"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_wrapped_file_when_missing(self):
        write_c3_instruction_doc(self.path, C3_CONTENT)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(C3_BLOCK_BEGIN, text)
        self.assertIn(C3_BLOCK_END, text)
        self.assertIn("## C3 Tools — MANDATORY", text)

    def test_idempotent_regeneration_preserves_user_content(self):
        # 1. Initial generation.
        write_c3_instruction_doc(self.path, C3_CONTENT)
        # 2. User edits below the block.
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text + "\n# User Notes\nhand-written\n", encoding="utf-8")
        # 3. Regenerate with new C3 content.
        write_c3_instruction_doc(self.path, "## C3 Tools — MANDATORY\nv2 workflow")
        final = self.path.read_text(encoding="utf-8")
        self.assertIn("hand-written", final)
        self.assertIn("v2 workflow", final)
        # No duplicated managed block after repeated regeneration.
        self.assertEqual(final.count(C3_BLOCK_BEGIN), 1)
        self.assertEqual(final.count(C3_BLOCK_END), 1)


class TestCompactPreservesMarkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.path = self.project / "CLAUDE.md"

    def tearDown(self):
        self.tmp.cleanup()

    def _manager(self):
        # compact() only touches the on-disk file + pure text helpers, so the
        # session_mgr / indexer / memory collaborators are unused here.
        return ClaudeMdManager(str(self.project), None, None, None,
                               instructions_file="CLAUDE.md")

    def test_compact_keeps_markers_and_outer_user_content(self):
        inner = (
            "## C3 Tools — MANDATORY\nline A\nline A\nline B\n\n"
            "# Project Context\nctx line\n"
        )
        self.path.write_text(
            "# My Header\nkeep above\n\n"
            + wrap_c3_block(inner)
            + "\n\n# User Notes\nkeep below\n",
            encoding="utf-8",
        )
        result = self._manager().compact(target_lines=3)
        content = result["content"]
        # Markers + managed heading survive.
        self.assertEqual(content.count(C3_BLOCK_BEGIN), 1)
        self.assertEqual(content.count(C3_BLOCK_END), 1)
        self.assertIn(C3_BLOCK_HEADING, content)
        # User content outside the block survives.
        self.assertIn("My Header", content)
        self.assertIn("keep above", content)
        self.assertIn("keep below", content)
        # Compaction still happened inside the block (duplicate line removed).
        self.assertEqual(content.count("line A"), 1)

    def test_compact_without_markers_still_works(self):
        self.path.write_text(
            "## C3 Tools — MANDATORY\ndup\ndup\nother\n# Project Context\nx\n",
            encoding="utf-8",
        )
        result = self._manager().compact(target_lines=2)
        self.assertNotIn(C3_BLOCK_BEGIN, result["content"])
        self.assertIn("## C3 Tools", result["content"])


if __name__ == "__main__":
    unittest.main()