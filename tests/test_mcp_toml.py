"""Round-trip tests for the shared MCP-TOML helpers (core/mcp_toml.py).

These pin the behaviour that used to be duplicated (and drifted) across
cli/server.py and cli/hub_server.py, including the reconciled choices:
quote-stripped keys and unlink-on-empty removal.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.mcp_toml import (  # noqa: E402
    parse_toml_mcp_servers,
    remove_toml_section,
    toml_escape_str,
    upsert_toml_section,
)


class TestParse(unittest.TestCase):
    def test_basic(self):
        toml = '[mcp_servers.c3]\ncommand = "python"\nargs = ["a", "b"]\nenabled = true\n'
        self.assertEqual(
            parse_toml_mcp_servers(toml),
            {"c3": {"command": "python", "args": ["a", "b"], "enabled": True}},
        )

    def test_quoted_key_is_stripped(self):
        toml = '[mcp_servers.c3]\n"command" = "py"\n'
        self.assertEqual(parse_toml_mcp_servers(toml)["c3"]["command"], "py")

    def test_enabled_false(self):
        self.assertFalse(
            parse_toml_mcp_servers('[mcp_servers.c3]\nenabled = false\n')["c3"]["enabled"]
        )

    def test_ignores_non_mcp_sections(self):
        toml = '[other]\nx = 1\n[mcp_servers.a]\ncommand = "c"\n'
        self.assertEqual(set(parse_toml_mcp_servers(toml)), {"a"})


class TestEscape(unittest.TestCase):
    def test_backslash_to_slash(self):
        self.assertEqual(toml_escape_str("C:\\foo\\bar"), "C:/foo/bar")


class TestUpsertRemove(unittest.TestCase):
    def _tmp(self):
        d = tempfile.mkdtemp()
        return Path(d) / "config.toml"

    def test_round_trip(self):
        f = self._tmp()
        upsert_toml_section(f, "mcp_servers.c3", {"command": "python", "args": ["x"]})
        parsed = parse_toml_mcp_servers(f.read_text(encoding="utf-8"))
        self.assertEqual(parsed["c3"]["command"], "python")
        self.assertEqual(parsed["c3"]["args"], ["x"])

    def test_upsert_replaces_existing(self):
        f = self._tmp()
        upsert_toml_section(f, "mcp_servers.c3", {"command": "old"})
        upsert_toml_section(f, "mcp_servers.c3", {"command": "new"})
        parsed = parse_toml_mcp_servers(f.read_text(encoding="utf-8"))
        self.assertEqual(parsed["c3"]["command"], "new")

    def test_upsert_preserves_other_sections(self):
        f = self._tmp()
        f.write_text("[keep]\nx = 1\n", encoding="utf-8")
        upsert_toml_section(f, "mcp_servers.c3", {"command": "python"})
        text = f.read_text(encoding="utf-8")
        self.assertIn("[keep]", text)
        self.assertIn("[mcp_servers.c3]", text)

    def test_remove_returns_false_when_absent(self):
        self.assertFalse(remove_toml_section(self._tmp(), "mcp_servers.none"))

    def test_remove_unlinks_emptied_file(self):
        f = self._tmp()
        upsert_toml_section(f, "mcp_servers.c3", {"command": "python"})
        self.assertTrue(remove_toml_section(f, "mcp_servers.c3"))
        self.assertFalse(f.exists())

    def test_remove_keeps_other_sections(self):
        f = self._tmp()
        f.write_text("[keep]\nx = 1\n", encoding="utf-8")
        upsert_toml_section(f, "mcp_servers.c3", {"command": "python"})
        remove_toml_section(f, "mcp_servers.c3")
        text = f.read_text(encoding="utf-8")
        self.assertIn("[keep]", text)
        self.assertNotIn("mcp_servers.c3", text)
        self.assertTrue(f.exists())


if __name__ == "__main__":
    unittest.main()
