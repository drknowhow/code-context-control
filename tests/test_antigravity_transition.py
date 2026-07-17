"""Gemini CLI removal / Antigravity promotion (v2.52).

The Gemini CLI IDE profile is removed: Antigravity (which reads AGENTS.md)
replaces it, legacy .gemini markers detect as Antigravity, GEMINI.md is
never generated but stays in the cleanup enumeration, and session-config
sync no longer touches .gemini/settings.json.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ide import PROFILES, detect_ide  # noqa: E402


class TestAntigravityProfile(unittest.TestCase):
    def test_antigravity_uses_agents_md(self):
        self.assertEqual(PROFILES["antigravity"].instructions_file, "AGENTS.md")

    def test_antigravity_config_is_user_global(self):
        profile = PROFILES["antigravity"]
        self.assertTrue(profile.config_path_global)
        self.assertEqual(profile.config_path, ".gemini/antigravity/mcp_config.json")

    def test_gemini_profile_removed(self):
        self.assertNotIn("gemini", PROFILES)

    def test_legacy_gemini_markers_detect_as_antigravity(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / ".gemini").mkdir()
            self.assertEqual(detect_ide(td), "antigravity")
            (p / ".gemini" / "settings.json").write_text("{}", encoding="utf-8")
            self.assertEqual(detect_ide(td), "antigravity")


class TestInstructionDocGating(unittest.TestCase):
    def test_cleanup_list_still_includes_gemini_md(self):
        # Uninstall/cleanup paths enumerate this list to delete stale docs,
        # so the deprecated GEMINI.md must stay listed here.
        from cli.c3 import _instruction_documents_for_project
        names = [name for name, _ in _instruction_documents_for_project()]
        self.assertIn("GEMINI.md", names)

    def test_gemini_md_never_generated(self):
        from cli.c3 import _instruction_documents_to_generate
        names = [name for name, _ in _instruction_documents_to_generate()]
        self.assertNotIn("GEMINI.md", names)
        self.assertIn("CLAUDE.md", names)
        self.assertIn("AGENTS.md", names)


class TestSessionConfigSync(unittest.TestCase):
    def test_gemini_project_config_not_seeded(self):
        # Antigravity-primary install: codex config is cross-synced as before,
        # but a project-local Gemini config must no longer be seeded.
        from cli.c3 import _ensure_project_session_configs
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            _ensure_project_session_configs(target, "server.py", primary_profile="antigravity")
            self.assertFalse((target / ".gemini" / "settings.json").exists())
            self.assertTrue((target / ".codex" / "config.toml").exists())

    def test_gemini_project_config_left_untouched(self):
        # Profile removed in v2.52: session sync must not modify legacy files.
        from cli.c3 import _ensure_project_session_configs
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gemini_path = target / ".gemini" / "settings.json"
            gemini_path.parent.mkdir(parents=True)
            original = '{"mcpServers": {}}'
            gemini_path.write_text(original, encoding="utf-8")
            _ensure_project_session_configs(target, "server.py", primary_profile="codex")
            self.assertEqual(gemini_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
