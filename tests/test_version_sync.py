"""The single-source-of-truth guard for version strings.

v2.56.0 shipped with pyproject.toml bumped but cli/c3.py.__version__ left at
2.55.0 — `c3 --version` lied and `c3 upgrade` looped on "update available".
This test makes that class of release impossible: CI (which gates every
release tag) fails whenever the two version sites disagree.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestVersionSync(unittest.TestCase):
    def test_cli_version_matches_pyproject(self):
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match, "no version line in pyproject.toml")
        pkg_version = match.group(1)

        from cli.c3 import __version__ as cli_version

        self.assertEqual(
            cli_version, pkg_version,
            f"cli/c3.py __version__ ({cli_version}) != pyproject.toml "
            f"({pkg_version}) — bump BOTH before tagging a release",
        )


if __name__ == "__main__":
    unittest.main()
