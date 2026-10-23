"""core.mcp_stack names a fastmcp/mcp mismatch; the MCP server and the
SessionStart hook report it instead of dying with an import traceback."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import mcp_stack  # noqa: E402

FASTMCP_3_PIN = ["httpx>=0.28.1", "mcp<2.0,>=1.24.0", "openai>=1.102.0; extra == \"openai\""]
SLIM_4_PINS = ["mcp-types<3.0.0,>=2.0.0",
               "mcp<3.0.0,>=2.2.0; extra == \"client\"",
               "mcp<3.0.0,>=2.2.0; extra == \"server\""]


def _installed(versions: dict, requires: dict):
    def version(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return versions[name]

    def reqs(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return requires.get(name, [])

    return mock.patch.multiple(mcp_stack.metadata, version=version, requires=reqs)


class TestProblem(unittest.TestCase):
    def test_fastmcp3_with_mcp2_names_both_repairs(self):
        with _installed({"fastmcp": "3.2.4", "mcp": "2.3.0"}, {"fastmcp": FASTMCP_3_PIN}):
            msg = mcp_stack.problem("C:/Py/python.exe")
        self.assertIn("fastmcp 3.2.4 needs mcp<2.0,>=1.24.0, but mcp 2.3.0 is installed", msg)
        self.assertIn('"C:/Py/python.exe" -m pip install --upgrade fastmcp', msg)
        self.assertIn('"C:/Py/python.exe" -m pip install "mcp<2.0,>=1.24.0"', msg)

    def test_matching_slim_pin_is_satisfied(self):
        with _installed({"fastmcp": "4.1.3", "fastmcp-slim": "4.1.3", "mcp": "2.3.0"},
                        {"fastmcp": ["fastmcp-slim[client,server]==4.1.3"],
                         "fastmcp-slim": SLIM_4_PINS}):
            self.assertIsNone(mcp_stack.problem())

    def test_mcp_too_old_offers_only_the_mcp_repair(self):
        with _installed({"fastmcp": "4.1.3", "fastmcp-slim": "4.1.3", "mcp": "1.30.0"},
                        {"fastmcp": ["fastmcp-slim[client,server]==4.1.3"],
                         "fastmcp-slim": SLIM_4_PINS}):
            msg = mcp_stack.problem("python")
        self.assertIn("needs mcp<3.0.0,>=2.2.0, but mcp 1.30.0", msg)
        self.assertIn('install "mcp<3.0.0,>=2.2.0"', msg)
        self.assertNotIn("--upgrade fastmcp", msg)

    def test_stale_slim_at_other_version_is_ignored(self):
        with _installed({"fastmcp": "3.2.4", "fastmcp-slim": "4.1.3", "mcp": "1.30.0"},
                        {"fastmcp": FASTMCP_3_PIN, "fastmcp-slim": SLIM_4_PINS}):
            self.assertIsNone(mcp_stack.problem())

    def test_missing_mcp_is_reported(self):
        with _installed({"fastmcp": "3.2.4"}, {"fastmcp": FASTMCP_3_PIN}):
            msg = mcp_stack.problem("python")
        self.assertIn("mcp is not installed", msg)

    def test_no_fastmcp_is_not_judged(self):
        with _installed({"mcp": "2.3.0"}, {}):
            self.assertIsNone(mcp_stack.problem())

    def test_pythonw_is_swapped_for_console_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("python.exe", "pythonw.exe"):
                (Path(tmp) / name).write_bytes(b"")
            self.assertEqual(mcp_stack._console_python(str(Path(tmp) / "pythonw.exe")),
                             str(Path(tmp) / "python.exe"))


class TestSessionStartReports(unittest.TestCase):
    def test_mismatch_becomes_session_context(self):
        from cli import hook_session_open
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"C3_NO_UI_AUTOSTART": "1"}), \
                mock.patch.object(mcp_stack, "problem", return_value="fastmcp mismatch"):
            (Path(tmp) / ".c3").mkdir()
            out = hook_session_open.run(
                {"session_id": "sid", "cwd": tmp, "source": "startup"}, Path(tmp))
        self.assertEqual(out, {"_text": "fastmcp mismatch"})


class TestServerStartup(unittest.TestCase):
    def test_broken_stack_exits_with_repair_instead_of_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "site"
            (fake / "fastmcp").mkdir(parents=True)
            (fake / "fastmcp" / "__init__.py").write_text(
                "raise ImportError(\"cannot import name 'McpError' from 'mcp'\")\n",
                encoding="utf-8")
            for name, ver, extra in (("fastmcp", "3.2.4", "Requires-Dist: mcp<2.0,>=1.24.0\n"),
                                     ("mcp", "2.3.0", "")):
                info = fake / f"{name}-{ver}.dist-info"
                info.mkdir()
                (info / "METADATA").write_text(
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n{extra}",
                    encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(fake)}
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "cli" / "mcp_server.py"), "--project", tmp],
                capture_output=True, text=True, env=env, timeout=60,
                stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("fastmcp 3.2.4 needs mcp<2.0,>=1.24.0, but mcp 2.3.0", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
