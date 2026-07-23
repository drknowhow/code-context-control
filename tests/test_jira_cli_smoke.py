"""Smoke tests for the `c3 jira` CLI subcommand.

Everything runs offline: the keyring is stubbed and login uses
--no-verify-login so no network is touched (this box has no Jira instance;
live validation happens on a separate account per the handoff checklist).
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.c3 import cmd_jira
from cli.commands.parser import build_parser
from services import jira_credentials as jr_creds


class _StubKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


def _parse(argv: list[str]):
    parser = build_parser("0.0-test", lambda value: value)
    return parser.parse_args(argv)


class TestJiraCliSmoke(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._patcher = mock.patch.object(
            jr_creds, "_keyring_module", return_value=self._stub
        )
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = self._tmp.name

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> str:
        args = _parse(argv)
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_jira(args)
        return out.getvalue()

    def _section(self) -> dict:
        cfg = Path(self.proj) / ".c3" / "config.json"
        if not cfg.exists():
            return {}
        return json.loads(cfg.read_text(encoding="utf-8")).get("jira", {})

    def test_bare_jira_prints_usage(self):
        out = self._run(["jira"])
        self.assertIn("Usage: c3 jira", out)

    def test_login_offline_stores_credentials_without_network(self):
        out = self._run([
            "jira", "login", "--url", "https://x.atlassian.net",
            "--username", "a@x.com", "--token", "sec",
            "--no-verify-login", self.proj,
        ])
        self.assertIn("[OK] Stored credentials", out)
        self.assertIn("probe skipped", out)
        section = self._section()
        self.assertEqual(section["default_account"], "x")
        self.assertEqual(section["accounts"]["x"]["deployment"], "cloud")
        self.assertEqual(
            self._stub.get_password("c3-jira", "https://x.atlassian.net|a@x.com"),
            "sec",
        )

    def test_login_infers_cloud_for_atlassian_net(self):
        out = self._run([
            "jira", "login", "--url", "https://team.atlassian.net",
            "--username", "a@x.com", "--token", "t",
            "--no-verify-login", self.proj,
        ])
        self.assertIn("[cloud", out)

    def test_login_self_hosted_requires_deployment(self):
        out = self._run([
            "jira", "login", "--url", "https://jira.corp.example",
            "--username", "alice", "--token", "t",
            "--no-verify-login", self.proj,
        ])
        self.assertIn("--deployment cloud|data_center is required", out)
        self.assertEqual(self._section(), {})

    def test_login_rejects_http_url(self):
        out = self._run([
            "jira", "login", "--url", "http://jira.local",
            "--deployment", "data_center",
            "--username", "alice", "--token", "t",
            "--no-verify-login", self.proj,
        ])
        self.assertIn("[error]", out)
        self.assertIn("https://", out)

    def test_status_graceful_in_clean_project(self):
        out = self._run(["jira", "status", self.proj])
        self.assertIn("[jira:status]", out)
        self.assertIn("no default account", out)

    def test_status_after_login_shows_account(self):
        self._run([
            "jira", "login", "--url", "https://x.atlassian.net",
            "--name", "work", "--username", "a@x.com", "--token", "sec",
            "--no-verify-login", self.proj,
        ])
        out = self._run(["jira", "status", self.proj])
        self.assertIn("* work: a@x.com@https://x.atlassian.net [cloud]", out)

    def test_set_default_before_login_is_graceful(self):
        out = self._run(["jira", "set-default", "--project", "PROJ", self.proj])
        self.assertIn("[error]", out)

    def test_use_unknown_account_is_graceful(self):
        out = self._run(["jira", "use", "--name", "ghost", self.proj])
        self.assertIn("[error]", out)

    def test_logout_round_trip(self):
        self._run([
            "jira", "login", "--url", "https://x.atlassian.net",
            "--name", "work", "--username", "a@x.com", "--token", "sec",
            "--no-verify-login", self.proj,
        ])
        out = self._run(["jira", "logout", self.proj])
        self.assertIn("[OK] Removed Jira account 'work'", out)
        out = self._run(["jira", "logout", self.proj])
        self.assertIn("No account specified and no default account", out)


if __name__ == "__main__":
    unittest.main()
