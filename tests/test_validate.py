import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import load_hybrid_config
from services import parser


class TestValidate(unittest.TestCase):
    def test_python_clean_returns_structured_result(self):
        result = parser.check_syntax_native("x = 1\n", ".py")
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["checker"], "python_ast")
        self.assertEqual(result["errors"], [])

    def test_python_syntax_error_returns_line_info(self):
        result = parser.check_syntax_native("def broken(:\n    pass\n", ".py")
        self.assertEqual(result["status"], "syntax_error")
        self.assertEqual(result["checker"], "python_ast")
        self.assertTrue(result["errors"])
        self.assertEqual(result["errors"][0]["line"], 1)

    def test_unsupported_extension_is_explicit(self):
        result = parser.check_syntax_native("hello", ".txt")
        self.assertEqual(result["status"], "unsupported")
        self.assertIn(".txt", result["detail"])

    def test_subprocess_unavailable_is_explicit(self):
        fake = parser._result("checker_unavailable", "bash -n", detail="bash missing")
        with patch("services.parser._subproc_check", return_value=fake):
            result = parser._native_shell("echo hi\n")
        self.assertEqual(result["status"], "checker_unavailable")
        self.assertEqual(result["checker"], "bash -n")

    def test_subprocess_failure_is_explicit(self):
        fake = parser._result("checker_failed", "bash -n", detail="access denied")
        with patch("services.parser._subproc_check", return_value=fake):
            result = parser._native_shell("echo hi\n")
        self.assertEqual(result["status"], "checker_failed")
        self.assertIn("access denied", result["detail"])

    # ── JSX-in-.js fallback (C3's UI serves JSX from .js files) ──

    JSX_SNIPPET = 'const App = () => (\n  <div className="x" />\n);\n'
    _NODE_SYNTAX_ERR = {
        "status": "ok", "returncode": 1, "stdout": "",
        "stderr": "file.js:2\n  <div className=\"x\" />\n"
                  "SyntaxError: Unexpected token '<'\n",
    }

    def _js_subproc(self, tsc):
        """side_effect for _subproc_check: node fails, tsc behaves as told."""
        def fake(cmd, content, ext, checker, timeout=None, **kw):
            if cmd[0] == "node":
                return dict(self._NODE_SYNTAX_ERR)
            if tsc == "unavailable":
                return parser._result("checker_unavailable", "tsc",
                                      detail="tsc missing")
            if tsc == "error":
                return {"status": "ok", "returncode": 2, "stderr": "",
                        "stdout": "f.jsx(3,5): error TS1005: '}' expected.\n"}
            return {"status": "ok", "returncode": 0, "stderr": "", "stdout": ""}
        return fake

    def test_js_with_jsx_revalidated_clean_by_tsc(self):
        with patch("services.parser._subproc_check",
                   side_effect=self._js_subproc(tsc="clean")):
            result = parser._native_js(self.JSX_SNIPPET)
        self.assertEqual(result["status"], "clean")
        self.assertIn("JSX in a .js file", result["detail"])

    def test_js_with_jsx_and_no_tsc_is_unsupported_not_syntax_error(self):
        # The toasts.js regression: a valid-JSX .js file must never bank a
        # false "has syntax errors" fact just because tsc is absent.
        with patch("services.parser._subproc_check",
                   side_effect=self._js_subproc(tsc="unavailable")):
            result = parser._native_js(self.JSX_SNIPPET)
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("JSX", result["detail"])

    def test_js_with_jsx_and_real_error_reports_tsc_positions(self):
        with patch("services.parser._subproc_check",
                   side_effect=self._js_subproc(tsc="error")):
            result = parser._native_js(self.JSX_SNIPPET)
        self.assertEqual(result["status"], "syntax_error")
        self.assertEqual(result["checker"], "tsc")

    def test_plain_js_error_never_consults_the_jsx_fallback(self):
        calls = []
        def fake(cmd, content, ext, checker, timeout=None, **kw):
            calls.append(cmd[0])
            return dict(self._NODE_SYNTAX_ERR)
        with patch("services.parser._subproc_check", side_effect=fake):
            result = parser._native_js("const x = 1;;;function {\n")
        self.assertEqual(result["status"], "syntax_error")
        self.assertEqual(result["checker"], "node --check")
        self.assertEqual(calls, ["node"])

    def test_subproc_check_resolves_checker_through_which(self):
        # npm-global tools on Windows are .cmd shims that a bare-name Popen
        # cannot start — the checker must be launched by its resolved path.
        seen = {}

        class _FakeProc:
            returncode = 0
            def communicate(self, timeout=None):
                return "", ""

        def fake_popen(argv, **kw):
            seen["argv0"] = argv[0]
            return _FakeProc()

        with patch("services.parser._shutil.which",
                   return_value=r"C:\Users\u\AppData\Roaming\npm\tsc.CMD"), \
             patch("services.parser._subprocess.Popen", side_effect=fake_popen):
            result = parser._subproc_check(["tsc", "--noEmit"], "x", ".jsx", "tsc")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen["argv0"], r"C:\Users\u\AppData\Roaming\npm\tsc.CMD")

    def test_subproc_check_unresolved_checker_is_unavailable_without_popen(self):
        with patch("services.parser._shutil.which", return_value=None), \
             patch("services.parser._subprocess.Popen") as popen:
            result = parser._subproc_check(["tsc", "--noEmit"], "x", ".jsx", "tsc")
        self.assertEqual(result["status"], "checker_unavailable")
        popen.assert_not_called()

    def test_timeout_wrapper_returns_timeout_status(self):
        import time

        def _slow_checker(content, ext):
            time.sleep(5)
            return parser._result("clean", "python_ast")

        with patch("services.parser.check_syntax_native", side_effect=_slow_checker):
            result = parser.check_syntax_native_with_timeout("x = 1\n", ".py", timeout_seconds=1)
        self.assertEqual(result["status"], "checker_timeout")
        self.assertEqual(result["checker"], "python_ast")

    def test_hybrid_config_migrates_old_validate_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_dir = Path(tmpdir) / ".c3"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(json.dumps({
                "hybrid": {
                    "validate_review_timeout_seconds": 33
                }
            }), encoding="utf-8")
            result = load_hybrid_config(tmpdir)
        self.assertEqual(result["validate_timeout_seconds"], 33)


if __name__ == "__main__":
    unittest.main()
