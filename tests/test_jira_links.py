"""Tests for services/jira_links.py — pure local, no network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import jira_links


class TestExtractIssueKeys(unittest.TestCase):
    def test_finds_and_dedupes_in_order(self):
        text = "Fix PROJ-123 and ENG-7; PROJ-123 again"
        self.assertEqual(
            jira_links.extract_issue_keys(text), ["PROJ-123", "ENG-7"]
        )

    def test_rejects_lowercase_in_prose(self):
        self.assertEqual(jira_links.extract_issue_keys("see proj-123"), [])

    def test_word_boundaries(self):
        self.assertEqual(jira_links.extract_issue_keys("xPROJ-1 PROJ-2x"), [])

    def test_acronym_denylist(self):
        text = "UTF-8 SHA-256 ISO-8601 RFC-2616 CVE-2024 MD-5 REAL-9"
        self.assertEqual(jira_links.extract_issue_keys(text), ["REAL-9"])

    def test_loose_variant_uppercases_branch_style(self):
        self.assertEqual(
            jira_links.extract_issue_keys_loose("feature/proj-123-fix-thing"),
            ["PROJ-123"],
        )

    def test_empty_and_none_safe(self):
        self.assertEqual(jira_links.extract_issue_keys(""), [])
        self.assertEqual(jira_links.extract_issue_keys_loose(""), [])


class TestBranchDetection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".git").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_head(self, content: str):
        (self.proj / ".git" / "HEAD").write_text(content, encoding="utf-8")

    def test_branch_with_key(self):
        self._write_head("ref: refs/heads/feature/PROJ-123-drawer\n")
        out = jira_links.branch_issue_keys(str(self.proj))
        self.assertEqual(out["branch"], "feature/PROJ-123-drawer")
        self.assertEqual(out["keys"], ["PROJ-123"])

    def test_lowercase_branch_key_normalized(self):
        self._write_head("ref: refs/heads/bugfix/eng-42-hotfix\n")
        self.assertEqual(
            jira_links.branch_issue_keys(str(self.proj))["keys"], ["ENG-42"]
        )

    def test_detached_head_is_empty(self):
        self._write_head("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678\n")
        out = jira_links.branch_issue_keys(str(self.proj))
        self.assertEqual(out["branch"], "")
        self.assertEqual(out["keys"], [])

    def test_missing_repo_is_empty(self):
        out = jira_links.branch_issue_keys(str(self.proj / "nope"))
        self.assertEqual(out, {"branch": "", "keys": []})


class TestLedgerActivity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_ledger(self, entries: list[dict]):
        path = self.proj / ".c3" / "edit_ledger.jsonl"
        path.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )

    def test_filters_to_entries_with_keys(self):
        self._write_ledger([
            {"file": "a.py", "summary": "fix PROJ-1 crash", "timestamp": "t1"},
            {"file": "b.py", "summary": "no key here", "timestamp": "t2"},
            {"file": "c.py", "summary": "PROJ-2 follow-up", "timestamp": "t3"},
        ])
        entries = jira_links.ledger_activity(str(self.proj))
        self.assertEqual([e["keys"] for e in entries], [["PROJ-2"], ["PROJ-1"]])
        self.assertEqual(entries[0]["file"], "c.py")  # newest first

    def test_key_filter_and_enrichment_patches_skipped(self):
        self._write_ledger([
            {"file": "a.py", "summary": "fix PROJ-1", "timestamp": "t1"},
            {"target_id": "x", "summary": "PROJ-1 enrichment patch"},
            {"file": "b.py", "summary": "ENG-2 work", "timestamp": "t2"},
        ])
        entries = jira_links.ledger_activity(str(self.proj), key="proj-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["file"], "a.py")

    def test_missing_ledger_is_empty(self):
        self.assertEqual(jira_links.ledger_activity(str(self.proj)), [])

    def test_malformed_lines_skipped(self):
        path = self.proj / ".c3" / "edit_ledger.jsonl"
        path.write_text(
            '{"file": "a.py", "summary": "PROJ-9 ok"}\nNOT JSON\n',
            encoding="utf-8",
        )
        entries = jira_links.ledger_activity(str(self.proj))
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
