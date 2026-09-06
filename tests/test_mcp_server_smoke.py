"""Smoke tests for cli/mcp_server.py.

Verifies the module imports cleanly, version is readable, the FastMCP
instance is constructed, and the documented C3 tools are registered.
"""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestMcpServerSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing mcp_server triggers FastMCP setup at module level.
        # If this raises, the test fails — which is the smoke check.
        from cli import mcp_server  # noqa: F401
        cls.mod = mcp_server

    def test_version_is_readable(self):
        version = self.mod.C3_VERSION
        self.assertIsInstance(version, str)
        self.assertRegex(version, r"\d+\.\d+\.\d+")

    def test_main_is_callable_entry_point(self):
        # We don't actually call main() — it would block on stdio.
        # We just verify it exists and is callable, since pyproject.toml
        # declares `c3-mcp = "cli.mcp_server:main"`.
        self.assertTrue(callable(getattr(self.mod, "main", None)),
                        "cli.mcp_server.main must exist for the c3-mcp entry-point")

    def test_fastmcp_instance_present(self):
        mcp = getattr(self.mod, "mcp", None)
        self.assertIsNotNone(mcp, "FastMCP instance `mcp` not found at module level")

    def test_core_c3_tools_registered(self):
        """The FastMCP server must expose the canonical c3_* toolset."""
        mcp = self.mod.mcp
        # FastMCP exposes registered tools via different attrs depending on
        # version; try the known accessors and fall back to introspection.
        names: set[str] = set()
        for attr in ("_tools", "tools", "_tool_manager"):
            obj = getattr(mcp, attr, None)
            if obj is None:
                continue
            try:
                if hasattr(obj, "keys"):
                    names.update(str(k) for k in obj.keys())
                elif hasattr(obj, "_tools"):
                    names.update(str(k) for k in obj._tools.keys())
            except Exception:
                continue
        # If no introspection path worked, at least confirm the module
        # imported `handle_*` for the documented tool surface.
        if not names:
            for tool in ("handle_search", "handle_read",
                         "handle_edit", "handle_validate", "handle_filter",
                         "handle_status", "handle_session", "handle_memory",
                         "handle_delegate", "handle_shell"):
                self.assertTrue(hasattr(self.mod, tool),
                                f"missing tool handler import: {tool}")
            return

        for expected in ("c3_search", "c3_read", "c3_edit",
                         "c3_validate", "c3_filter", "c3_status", "c3_shell"):
            # Names may be prefixed by FastMCP; substring match is sufficient.
            self.assertTrue(any(expected in n for n in names),
                            f"tool {expected!r} not registered (registered: {sorted(names)})")
        # 2.124.0: the map is c3_read's; c3_compress is not an MCP tool any more.
        self.assertFalse(any("c3_compress" in n for n in names), sorted(names))


if __name__ == "__main__":
    unittest.main()
