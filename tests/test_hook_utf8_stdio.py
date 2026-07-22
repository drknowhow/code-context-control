"""UTF-8 stdio regression coverage for hook subprocess entrypoints."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import _hook_utils

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_python(code: str, *, input_bytes: bytes = b"", args: tuple[str, ...] = (),
                cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        timeout=15,
    )


class _RecordingStream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class TestHookUtf8Stdio(unittest.TestCase):
    def test_output_is_tolerant_but_input_stays_strict(self):
        stdin = _RecordingStream()
        stdout = _RecordingStream()
        stderr = _RecordingStream()
        with patch.object(_hook_utils.sys, "stdin", stdin), \
             patch.object(_hook_utils.sys, "stdout", stdout), \
             patch.object(_hook_utils.sys, "stderr", stderr):
            _hook_utils.ensure_utf8_stdio()
        self.assertEqual(stdin.calls, [{"encoding": "utf-8", "errors": "strict"}])
        self.assertEqual(stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_helper_overrides_cp1252_stdout(self):
        result = _run_python(
            "from cli._hook_utils import ensure_utf8_stdio; "
            "ensure_utf8_stdio(); print(chr(0x2500))"
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout.decode("utf-8").strip(), "─")

    def test_dispatcher_wraps_unicode_text_as_json_under_cp1252(self):
        code = """
import sys
from cli import hook_dispatch
hook_dispatch._RUN_CACHE.clear()
hook_dispatch._RUN_CACHE.update({
    'hook_session_stats': (lambda payload, project_path=None: None, ''),
    'hook_auto_snapshot': (lambda payload, project_path=None: None, ''),
    'hook_terse_advisor': (lambda payload, project_path=None: {'_text': chr(0x2500)}, ''),
})
sys.argv = ['hook_dispatch.py', 'stop']
hook_dispatch.main()
"""
        result = _run_python(code, input_bytes=b"{}")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        output = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(output, {"systemMessage": "─"})

    def test_posttool_plain_text_is_valid_json(self):
        code = """
import sys
from cli import hook_dispatch
hook_dispatch._RUN_CACHE.clear()
hook_dispatch._RUN_CACHE.update({
    'hook_edit_ledger': (lambda payload, project_path=None: {'_text': 'ledger line'}, ''),
    'hook_artifact': (lambda payload, project_path=None: None, ''),
})
sys.argv = ['hook_dispatch.py', 'posttool']
hook_dispatch.main()
"""
        payload = json.dumps({"tool_name": "Edit"}).encode("utf-8")
        result = _run_python(code, input_bytes=payload)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        output = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(output, {"systemMessage": "ledger line"})

    def test_terse_advisor_prints_divider_under_cp1252(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "transcript.jsonl"
            transcript.write_text(json.dumps({
                "message": {"role": "assistant", "content": "z" * 700},
            }) + "\n", encoding="utf-8")
            payload = json.dumps({
                "session_id": "utf8-test",
                "transcript_path": str(transcript),
            }).encode("utf-8")
            code = """
import sys
from pathlib import Path
from cli import hook_terse_advisor
hook_terse_advisor._STATE_FILE = Path(sys.argv[1])
hook_terse_advisor.main()
"""
            result = _run_python(
                code,
                input_bytes=payload,
                args=(str(root / "terse_state.json"),),
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("─", result.stdout.decode("utf-8"))

    def test_edit_ledger_prints_unicode_path_under_cp1252(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".c3").mkdir()
            unicode_path = root / "unicode_─.py"
            payload = json.dumps({
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(unicode_path),
                    "old_string": "a",
                    "new_string": "b",
                },
            }, ensure_ascii=False).encode("utf-8")
            result = _run_python(
                "from cli import hook_edit_ledger; hook_edit_ledger.main()",
                input_bytes=payload,
                cwd=root,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("unicode_─.py", result.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
