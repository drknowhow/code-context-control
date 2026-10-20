"""Tests for cli/tools/_json_reply.py — format="json" replies."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.tools import _json_reply as jr


class TestWantsJson(unittest.TestCase):
    def test_flag_values(self):
        self.assertTrue(jr.wants_json({"format": "json"}))
        self.assertTrue(jr.wants_json({"format": " JSON "}))
        self.assertFalse(jr.wants_json({"format": "text"}))
        self.assertFalse(jr.wants_json({"format": ""}))
        self.assertFalse(jr.wants_json({"format": None}))
        self.assertFalse(jr.wants_json({}))


class TestOk(unittest.TestCase):
    def test_dict_data(self):
        out = json.loads(jr.ok("get_issue", {"key": "PROJ-1"}))
        self.assertEqual(out, {"ok": True, "action": "get_issue", "data": {"key": "PROJ-1"}})

    def test_non_dict_is_wrapped(self):
        out = json.loads(jr.ok("search", [1, 2]))
        self.assertEqual(out["data"], {"values": [1, 2]})

    def test_input_dict_not_mutated(self):
        data = {"comments": list(range(5000))}
        jr.ok("get_issue", data, cap=2000)
        self.assertEqual(len(data["comments"]), 5000)

    def test_keep_tail_lists_keep_newest(self):
        data = {"key": "PROJ-1", "comments": [{"n": i, "body": "x" * 50} for i in range(2000)]}
        raw = jr.ok("get_issue", data, cap=20_000)
        self.assertLessEqual(len(raw), 20_000)
        out = json.loads(raw)
        self.assertEqual(out["truncated"], ["comments"])
        self.assertEqual(out["data"]["key"], "PROJ-1")
        self.assertEqual(out["data"]["comments"][-1]["n"], 1999)

    def test_other_lists_keep_head(self):
        data = {"values": [{"n": i, "pad": "y" * 50} for i in range(2000)]}
        out = json.loads(jr.ok("list_prs", data, cap=20_000))
        self.assertEqual(out["data"]["values"][0]["n"], 0)
        self.assertEqual(out["truncated"], ["values"])

    def test_long_string_is_cut(self):
        raw = jr.ok("get_pr_diff", {"diff": "d" * 100_000}, cap=10_000)
        self.assertLessEqual(len(raw), 10_000)
        out = json.loads(raw)
        self.assertTrue(out["data"]["diff"].startswith("ddd"))
        self.assertEqual(out["truncated"], ["diff"])

    def test_no_truncated_key_when_it_fits(self):
        out = json.loads(jr.ok("whoami", {"name": "me"}))
        self.assertNotIn("truncated", out)


class TestErrorAndText(unittest.TestCase):
    def test_error_minimal(self):
        out = json.loads(jr.error("get_issue", "boom"))
        self.assertEqual(out, {"ok": False, "action": "get_issue", "error": "boom"})

    def test_error_status_and_code(self):
        out = json.loads(jr.error("get_issue", "gone", status=404, code="http-404"))
        self.assertEqual(out["status"], 404)
        self.assertEqual(out["code"], "http-404")

    def test_text_rides_in_data(self):
        out = json.loads(jr.text("merge_pr", "[bitbucket:merged] PR 12"))
        self.assertEqual(out["data"], {"text": "[bitbucket:merged] PR 12"})

    def test_is_json_reply(self):
        self.assertTrue(jr.is_json_reply(jr.ok("a", {})))
        self.assertTrue(jr.is_json_reply(jr.error("a", "e")))
        self.assertFalse(jr.is_json_reply("[jira:issue] PROJ-1"))
        self.assertFalse(jr.is_json_reply('{"other": 1}'))


if __name__ == "__main__":
    unittest.main()
