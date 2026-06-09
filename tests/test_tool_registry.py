"""Tests for oracle/services/tool_registry.py — tiers, dispatch, OpenAPI."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from oracle.services.tool_registry import TIER_ACTION, TIER_READ, TOOL_SPECS, ToolRegistry


class _RecordingExecutor:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name, args=None):
        self.calls.append((name, args))
        return {"ok": True, "name": name, "args": args}


class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.exec = _RecordingExecutor()

    def test_all_specs_have_object_schema(self):
        for spec in TOOL_SPECS:
            self.assertIn(spec["tier"], (TIER_READ, TIER_ACTION))
            params = spec["parameters"]
            self.assertEqual(params.get("type"), "object")
            self.assertIn("properties", params)
            self.assertIn("required", params)

    def test_no_code_edit_tools(self):
        names = {s["name"] for s in TOOL_SPECS}
        for forbidden in ("c3_edit", "edit", "c3_shell", "write", "c3_delegate"):
            self.assertNotIn(forbidden, names)

    def test_tier_filtering(self):
        read_only = set(ToolRegistry(self.exec, max_tier=TIER_READ).tool_names())
        self.assertIn("c3_search", read_only)
        self.assertNotIn("suggest_action", read_only)
        self.assertNotIn("delegate_task", read_only)

        full = set(ToolRegistry(self.exec, max_tier=TIER_ACTION).tool_names())
        self.assertIn("suggest_action", full)
        self.assertIn("delegate_task", full)

    def test_call_tool_filters_unknown_args(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_ACTION)
        out = reg.call_tool("c3_search", {"project_path": "/p", "query": "x", "bogus": 1})
        self.assertTrue(out["ok"])
        name, args = self.exec.calls[-1]
        self.assertEqual(name, "c3_search")
        self.assertNotIn("bogus", args)
        self.assertEqual(args["project_path"], "/p")

    def test_call_tool_missing_required(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_ACTION)
        out = reg.call_tool("c3_search", {"project_path": "/p"})  # query missing
        self.assertIn("error", out)
        self.assertEqual(self.exec.calls, [])

    def test_call_tool_tier_blocked(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_READ)
        out = reg.call_tool("delegate_task", {"agent_id": "a", "task": "t"})
        self.assertIn("error", out)
        self.assertEqual(self.exec.calls, [])

    def test_call_tool_unknown(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_ACTION)
        self.assertIn("error", reg.call_tool("does_not_exist", {}))

    def test_openapi_has_path_per_tool(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_ACTION)
        spec = reg.openapi_spec("http://127.0.0.1:3331/")
        self.assertEqual(spec["openapi"], "3.1.0")
        for name in reg.tool_names():
            self.assertIn(f"/api/discovery/tools/{name}", spec["paths"])
        self.assertIn("bearerAuth", spec["components"]["securitySchemes"])
        self.assertEqual(spec["servers"][0]["url"], "http://127.0.0.1:3331")

    def test_openapi_excludes_blocked_tier(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_READ)
        spec = reg.openapi_spec()
        self.assertNotIn("/api/discovery/tools/delegate_task", spec["paths"])


if __name__ == "__main__":
    unittest.main()
