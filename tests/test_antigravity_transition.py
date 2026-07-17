"""Gemini CLI deprecation / Antigravity promotion (phase 1).

Antigravity reads AGENTS.md (preferring it over GEMINI.md when both exist),
so its profile must not depend on GEMINI.md; GEMINI.md generation is gated
to Gemini CLI projects; and project-local Gemini configs are refreshed when
already present but never seeded from other IDE installs.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ide import PROFILES  # noqa: E402


class TestAntigravityProfile(unittest.TestCase):
    def test_antigravity_uses_agents_md(self):
        self.assertEqual(PROFILES["antigravity"].instructions_file, "AGENTS.md")

    def test_antigravity_config_is_user_global(self):
        profile = PROFILES["antigravity"]
        self.assertTrue(profile.config_path_global)
        self.assertEqual(profile.config_path, ".gemini/antigravity/mcp_config.json")


class TestInstructionDocGating(unittest.TestCase):
    def test_cleanup_list_still_includes_gemini_md(self):
        # Uninstall/cleanup paths enumerate this list to delete stale docs,
        # so the deprecated GEMINI.md must stay listed here.
        from cli.c3 import _instruction_documents_for_project
        names = [name for name, _ in _instruction_documents_for_project()]
        self.assertIn("GEMINI.md", names)

    def test_gemini_md_generated_only_for_gemini_ide(self):
        from cli.c3 import _instruction_documents_to_generate
        for ide in ("claude-code", "codex", "vscode", "cursor", "antigravity"):
            names = [name for name, _ in _instruction_documents_to_generate(ide)]
            self.assertNotIn("GEMINI.md", names, f"ide={ide}")
            self.assertIn("CLAUDE.md", names, f"ide={ide}")
            self.assertIn("AGENTS.md", names, f"ide={ide}")
        gemini_names = [name for name, _ in _instruction_documents_to_generate("gemini")]
        self.assertIn("GEMINI.md", gemini_names)


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

    def test_gemini_project_config_refreshed_when_present(self):
        from cli.c3 import _ensure_project_session_configs
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            gemini_path = target / ".gemini" / "settings.json"
            gemini_path.parent.mkdir(parents=True)
            gemini_path.write_text('{"mcpServers": {}}', encoding="utf-8")
            _ensure_project_session_configs(target, "server.py", primary_profile="codex")
            data = json.loads(gemini_path.read_text(encoding="utf-8"))
            self.assertIn("c3", data["mcpServers"])


if __name__ == "__main__":
    unittest.main()
