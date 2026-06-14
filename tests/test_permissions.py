"""Test coverage for the C3 permission tier system.

Catches the class of bug where a new c3_* MCP tool gets added to the server
but isn't registered in _C3_MCP_ALLOW — users then hit a permission prompt
on every call until someone notices.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.c3 import (  # noqa: E402
    _C3_MCP_ALLOW,
    _STALE_MCP_TOOLS,
    _TIER_ALIASES,
    PERMISSION_TIERS,
    _apply_permission_tier,
    _build_permission_tier,
    _clean_stale_tools,
    _detect_current_tier,
    _find_stale_tools,
)


def _registered_c3_tools() -> set[str]:
    """Scrape cli/mcp_server.py for @mcp.tool() async def c3_* registrations."""
    mcp_server = Path(__file__).parent.parent / "cli" / "mcp_server.py"
    text = mcp_server.read_text(encoding="utf-8")
    # Match: @mcp.tool() (+newline) async def c3_xxx(
    pattern = re.compile(r"@mcp\.tool\(\)\s*\nasync\s+def\s+(c3_\w+)\s*\(")
    return {f"mcp__c3__{name}" for name in pattern.findall(text)}


class TestPermissionRegistry(unittest.TestCase):
    def test_all_registered_tools_in_allow_list(self):
        """_C3_MCP_ALLOW must include every c3_* tool registered in mcp_server.py."""
        registered = _registered_c3_tools()
        allow_set = set(_C3_MCP_ALLOW)
        missing = registered - allow_set
        self.assertFalse(
            missing,
            f"Registered c3_* tools missing from _C3_MCP_ALLOW: {missing}. "
            "Add them to cli/c3.py::_C3_MCP_ALLOW to avoid permission prompts.",
        )

    def test_no_obsolete_tools_in_allow_list(self):
        """_C3_MCP_ALLOW must not contain any names also in _STALE_MCP_TOOLS."""
        overlap = set(_C3_MCP_ALLOW) & _STALE_MCP_TOOLS
        self.assertFalse(overlap, f"_C3_MCP_ALLOW contains stale tool names: {overlap}")


class TestTierBuilder(unittest.TestCase):
    def test_all_tiers_build(self):
        for tier in PERMISSION_TIERS:
            perms = _build_permission_tier(tier)["permissions"]
            self.assertIn("allow", perms)
            self.assertIn("deny", perms)
            self.assertIsInstance(perms["allow"], list)
            self.assertIsInstance(perms["deny"], list)

    def test_every_tier_includes_all_c3_tools(self):
        for tier in PERMISSION_TIERS:
            perms = _build_permission_tier(tier)["permissions"]
            allow = set(perms["allow"])
            missing = set(_C3_MCP_ALLOW) - allow
            self.assertFalse(missing, f"Tier '{tier}' missing c3 tools: {missing}")

    def test_c3_strict_denies_native_file_tools(self):
        perms = _build_permission_tier("c3-strict")["permissions"]
        deny = set(perms["deny"])
        for tool in ("Read(*)", "Grep(*)", "Glob(*)", "Edit(*)", "Write(*)"):
            self.assertIn(tool, deny, f"c3-strict must deny {tool}")

    def test_read_only_denies_writes(self):
        perms = _build_permission_tier("read-only")["permissions"]
        deny = set(perms["deny"])
        self.assertIn("Write(*)", deny)
        self.assertIn("Edit(*)", deny)

    def test_permissive_no_deny(self):
        perms = _build_permission_tier("permissive")["permissions"]
        self.assertEqual(perms["deny"], [])

    def test_include_mcp_wildcard_adds_entry(self):
        with_wild = _build_permission_tier("standard", include_mcp_wildcard=True)["permissions"]
        without_wild = _build_permission_tier("standard", include_mcp_wildcard=False)["permissions"]
        self.assertIn("mcp__*", with_wild["allow"])
        self.assertNotIn("mcp__*", without_wild["allow"])

    def test_tier_aliases_resolve(self):
        for alias, canonical in _TIER_ALIASES.items():
            self.assertEqual(
                _build_permission_tier(alias)["permissions"],
                _build_permission_tier(canonical)["permissions"],
                f"Alias '{alias}' should resolve to '{canonical}'",
            )


class TestTierDetection(unittest.TestCase):
    def _write(self, tmp: Path, perms: dict) -> Path:
        path = tmp / "settings.local.json"
        path.write_text(json.dumps({"permissions": perms}), encoding="utf-8")
        return path

    def test_roundtrip_all_tiers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for tier in PERMISSION_TIERS:
                perms = _build_permission_tier(tier)["permissions"]
                path = self._write(tmp, perms)
                detected = _detect_current_tier(path)
                self.assertEqual(detected, tier, f"Round-trip failed for '{tier}'")

    def test_roundtrip_with_mcp_wildcard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.local.json"
            perms = _build_permission_tier("c3-strict", include_mcp_wildcard=True)["permissions"]
            path.write_text(json.dumps({"permissions": perms}), encoding="utf-8")
            self.assertEqual(_detect_current_tier(path), "c3-strict")


class TestStaleCleanup(unittest.TestCase):
    def test_find_stale_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.local.json"
            path.write_text(json.dumps({
                "permissions": {
                    "allow": [
                        "mcp__c3__c3_memory",          # current
                        "mcp__c3__c3_remember",        # stale
                        "mcp__c3__c3_file_map",        # stale
                        "Bash(ls:*)",
                    ],
                    "deny": ["mcp__c3__c3_extract"],   # stale
                },
            }), encoding="utf-8")
            stale = _find_stale_tools(path)
            self.assertIn("mcp__c3__c3_remember", stale)
            self.assertIn("mcp__c3__c3_file_map", stale)
            self.assertIn("mcp__c3__c3_extract", stale)
            self.assertNotIn("mcp__c3__c3_memory", stale)

    def test_clean_removes_stale_preserves_current(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.local.json"
            path.write_text(json.dumps({
                "permissions": {
                    "allow": ["mcp__c3__c3_memory", "mcp__c3__c3_remember", "Bash(ls:*)"],
                    "deny": ["mcp__c3__c3_extract"],
                },
            }), encoding="utf-8")
            removed = _clean_stale_tools(path)
            self.assertEqual(removed, 2)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["permissions"]["allow"], ["mcp__c3__c3_memory", "Bash(ls:*)"])
            self.assertEqual(data["permissions"]["deny"], [])

    def test_clean_no_stale_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.local.json"
            path.write_text(json.dumps({
                "permissions": {"allow": ["mcp__c3__c3_memory"], "deny": []},
            }), encoding="utf-8")
            self.assertEqual(_clean_stale_tools(path), 0)


class TestApplyPreservesOtherKeys(unittest.TestCase):
    def _setup_project(self, tmpdir: str) -> Path:
        project = Path(tmpdir)
        (project / ".claude").mkdir()
        (project / ".c3").mkdir()
        return project

    def test_apply_preserves_user_custom_and_other_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = self._setup_project(tmpdir)
            settings = project / ".claude" / "settings.local.json"
            settings.write_text(json.dumps({
                "enableAllProjectMcpServers": True,
                "hooks": {"PostToolUse": [{"matcher": "*", "hooks": []}]},
                "permissions": {
                    "allow": ["Bash(mycustomtool:*)"],  # user-custom — must survive
                    "deny": ["Edit(*)"],                # stale tier entry — dropped under standard
                    "ask": ["Bash(git push:*)"],        # non-allow/deny key — must survive
                },
            }), encoding="utf-8")
            _apply_permission_tier(str(project), "standard")
            data = json.loads(settings.read_text(encoding="utf-8"))
            # Other top-level keys preserved
            self.assertTrue(data["enableAllProjectMcpServers"])
            self.assertIn("hooks", data)
            perms = data["permissions"]
            # User-custom allow entry preserved (not a C3-managed rule)
            self.assertIn("Bash(mycustomtool:*)", perms["allow"])
            # Non-allow/deny permission sub-key preserved
            self.assertEqual(perms.get("ask"), ["Bash(git push:*)"])
            # Stale C3-managed deny entry not in standard → removed
            self.assertNotIn("Edit(*)", perms["deny"])
            # Chosen tier's entries applied
            self.assertIn("Bash(rm -rf *)", perms["deny"])
            # Tier stored in .c3/config.json
            cfg = json.loads((project / ".c3" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["permission_tier"], "standard")

    def test_tier_switch_drops_previous_tier_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = self._setup_project(tmpdir)
            settings = project / ".claude" / "settings.local.json"
            _apply_permission_tier(str(project), "c3-strict")
            mid = json.loads(settings.read_text(encoding="utf-8"))
            self.assertIn("Read(*)", mid["permissions"]["deny"])  # strict denies Read
            _apply_permission_tier(str(project), "permissive")
            data = json.loads(settings.read_text(encoding="utf-8"))
            # Switching tiers removes the previous tier's managed deny entries
            self.assertNotIn("Read(*)", data["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
