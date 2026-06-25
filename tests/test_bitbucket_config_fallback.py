"""Issue 3: load_bitbucket_config falls back to the global ~/.c3/config.json.

A one-time `c3 bitbucket login` (or `login --global`) must be reusable from a
different C3 project that has never run login, while a project that does have an
active account still takes precedence.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import config as core_config


def _write_cfg(root: Path, section: dict) -> None:
    c3 = root / ".c3"
    c3.mkdir(parents=True, exist_ok=True)
    (c3 / "config.json").write_text(
        json.dumps({"bitbucket": section}), encoding="utf-8"
    )


class TestBitbucketConfigFallback(unittest.TestCase):
    def setUp(self):
        self._proj = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._proj.name)
        self.home = Path(self._home.name)
        self._home_patcher = mock.patch.object(Path, "home", return_value=self.home)
        self._home_patcher.start()

    def tearDown(self):
        self._home_patcher.stop()
        self._proj.cleanup()
        self._home.cleanup()

    def test_falls_back_to_home_when_project_has_no_active(self):
        _write_cfg(self.proj, {"active": {"base_url": "", "username": ""}})
        _write_cfg(
            self.home, {"active": {"base_url": "https://bb", "username": "alice"}}
        )
        cfg = core_config.load_bitbucket_config(str(self.proj))
        self.assertEqual(
            cfg["active"], {"base_url": "https://bb", "username": "alice"}
        )

    def test_project_active_takes_precedence_over_home(self):
        _write_cfg(
            self.proj, {"active": {"base_url": "https://proj", "username": "bob"}}
        )
        _write_cfg(
            self.home, {"active": {"base_url": "https://bb", "username": "alice"}}
        )
        cfg = core_config.load_bitbucket_config(str(self.proj))
        self.assertEqual(cfg["active"]["base_url"], "https://proj")

    def test_no_configs_returns_defaults(self):
        cfg = core_config.load_bitbucket_config(str(self.proj))
        self.assertEqual(cfg["active"], {"base_url": "", "username": ""})


if __name__ == "__main__":
    unittest.main()
