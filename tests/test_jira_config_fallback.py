"""load_jira_config falls back to the global ~/.c3/config.json — wholesale.

Mirrors the Bitbucket fallback tests, plus the no-field-merge security
invariant: a project config that has no usable account of its own must not
be able to override fields (base_url, TLS settings) of a home-registered
account.
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
        json.dumps({"jira": section}), encoding="utf-8"
    )


def _account(base_url: str, **kw) -> dict:
    entry = {
        "base_url": base_url,
        "username": "alice@example.com",
        "deployment": "cloud",
        "default_project": "",
        "verify_tls": True,
        "ca_bundle": "",
    }
    entry.update(kw)
    return entry


class TestJiraConfigFallback(unittest.TestCase):
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

    def test_falls_back_to_home_when_project_has_no_usable_account(self):
        _write_cfg(self.proj, {"default_account": "", "accounts": {}})
        _write_cfg(self.home, {
            "default_account": "work",
            "accounts": {"work": _account("https://home.atlassian.net")},
        })
        cfg = core_config.load_jira_config(str(self.proj))
        self.assertEqual(cfg["default_account"], "work")
        self.assertEqual(
            cfg["accounts"]["work"]["base_url"], "https://home.atlassian.net"
        )

    def test_project_account_takes_precedence_over_home(self):
        _write_cfg(self.proj, {
            "default_account": "work",
            "accounts": {"work": _account("https://proj.atlassian.net")},
        })
        _write_cfg(self.home, {
            "default_account": "work",
            "accounts": {"work": _account("https://home.atlassian.net")},
        })
        cfg = core_config.load_jira_config(str(self.proj))
        self.assertEqual(
            cfg["accounts"]["work"]["base_url"], "https://proj.atlassian.net"
        )

    def test_no_configs_returns_defaults(self):
        cfg = core_config.load_jira_config(str(self.proj))
        self.assertEqual(cfg["default_account"], "")
        self.assertEqual(cfg["accounts"], {})

    def test_project_fields_never_merged_over_home_account(self):
        # Security invariant: an unusable project section (no resolvable
        # default_account) must be discarded WHOLESALE, not field-merged —
        # otherwise a malicious repo could weaken TLS or redirect the
        # base_url of a home-registered account.
        _write_cfg(self.proj, {
            "accounts": {
                "work": _account("https://evil.example", verify_tls=False),
            },
        })
        _write_cfg(self.home, {
            "default_account": "work",
            "accounts": {"work": _account("https://home.atlassian.net")},
        })
        cfg = core_config.load_jira_config(str(self.proj))
        entry = cfg["accounts"]["work"]
        self.assertEqual(entry["base_url"], "https://home.atlassian.net")
        self.assertTrue(entry["verify_tls"])
        self.assertNotIn("evil", json.dumps(cfg))

    def test_dangling_default_account_falls_back_to_home(self):
        _write_cfg(self.proj, {"default_account": "ghost", "accounts": {}})
        _write_cfg(self.home, {
            "default_account": "work",
            "accounts": {"work": _account("https://home.atlassian.net")},
        })
        cfg = core_config.load_jira_config(str(self.proj))
        self.assertEqual(cfg["default_account"], "work")


if __name__ == "__main__":
    unittest.main()
