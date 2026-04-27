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
