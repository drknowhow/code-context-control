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

    def test_activity_report_is_read_tier_no_required_args(self):
        read_only = ToolRegistry(self.exec, max_tier=TIER_READ)
        self.assertIn("activity_report", read_only.tool_names())
        # No required args → callable with an empty args object.
        out = read_only.call_tool("activity_report", {})
        self.assertTrue(out["ok"])
        name, _args = self.exec.calls[-1]
        self.assertEqual(name, "activity_report")

    def test_project_and_artifacts_are_read_tier(self):
        read_only = set(ToolRegistry(self.exec, max_tier=TIER_READ).tool_names())
        self.assertIn("c3_project", read_only)
        self.assertIn("c3_artifacts", read_only)

    def test_no_spec_declares_allow_write(self):
        # The write-verb kill switch: allow_write must not exist as a declared
        # parameter anywhere, so call_tool's undeclared-key drop strips it.
        for spec in TOOL_SPECS:
            self.assertNotIn("allow_write", spec["parameters"]["properties"],
                             f"{spec['name']} must not declare allow_write")

    def test_call_tool_drops_allow_write_for_c3_project(self):
        reg = ToolRegistry(self.exec, max_tier=TIER_READ)
        out = reg.call_tool("c3_project", {"action": "list", "allow_write": True})
        self.assertTrue(out["ok"])
        _name, args = self.exec.calls[-1]
        self.assertNotIn("allow_write", args)

    def test_project_enum_excludes_write_and_scan_verbs(self):
        spec = next(s for s in TOOL_SPECS if s["name"] == "c3_project")
        allowed = set(spec["parameters"]["properties"]["action"]["enum"])
        for forbidden in ("edit", "shell", "register", "unregister", "scan",
                          "sub_add", "sub_remove", "sub_cascade", "filter"):
            self.assertNotIn(forbidden, allowed)

    def test_artifacts_enum_excludes_scan_and_restore(self):
        spec = next(s for s in TOOL_SPECS if s["name"] == "c3_artifacts")
        allowed = set(spec["parameters"]["properties"]["action"]["enum"])
        self.assertNotIn("scan", allowed)
        self.assertNotIn("restore", allowed)

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
