import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools.edit import handle_edit  # noqa: E402


class _Recorder:
    def __init__(self):
        self.detail = None
        self.duration_ms = None
        self.kw = None

    def record_tool_tokens(self, tool_name, *, raw_tokens=None,
                           optimized_tokens=None, duration_ms=None, detail=None):
        self.duration_ms = duration_ms
        self.detail = detail

    def finalize(self, name, args, resp, summ, **kw):
        self.kw = kw
        return resp


@pytest.fixture
def call(tmp_path):
    rec = _Recorder()
    svc = MagicMock()
    svc.project_path = str(tmp_path)
    svc.edit_ledger = None
    svc.session_mgr = rec
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\ntwo\n", encoding="utf-8")

    def run(old="", new="", edits="", path=target):
        handle_edit(str(path), old, new, "", "", False, svc, rec.finalize, edits)
        return rec
    return run


def test_successful_edit_reports_ok_and_counts(call):
    rec = call("one", "ONE")
    assert rec.kw["ok"] is True
    assert rec.detail["outcome"] == "success"
    assert (rec.detail["n_attempted"], rec.detail["n_applied"]) == (1, 1)
    assert rec.duration_ms >= 0


@pytest.mark.parametrize("old,outcome", [
    ("three", "not_found"),
    ("two", "ambiguous"),
])
def test_failed_edit_reports_not_ok(call, old, outcome):
    rec = call(old, "x")
    assert rec.kw["ok"] is False
    assert rec.detail["outcome"] == outcome


def test_unchanged_edit_is_ok_noop(call):
    rec = call("one", "one")
    assert rec.kw["ok"] is True
    assert rec.detail["outcome"] == "noop"


def test_missing_file_reports_not_found(call, tmp_path):
    rec = call("one", "x", path=tmp_path / "absent.txt")
    assert rec.kw["ok"] is False
    assert rec.detail["outcome"] == "not_found"


def test_batch_with_no_patch_applied_is_not_ok(call):
    rec = call(edits=json.dumps([{"old_string": "three", "new_string": "x"}]))
    assert rec.kw["ok"] is False
    assert rec.detail["outcome"] == "not_found"
    assert (rec.detail["n_attempted"], rec.detail["n_applied"]) == (1, 0)
