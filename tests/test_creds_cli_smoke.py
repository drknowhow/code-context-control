"""Smoke tests for the `c3 creds` CLI subcommand — offline, stubbed keyring."""
from __future__ import annotations

import base64
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

from cli.c3 import cmd_creds
from cli.commands.parser import build_parser
from services import credential_store as cs


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


class _StubFernet:
    def __init__(self, key):
        self._key = key

    @staticmethod
    def generate_key():
        return base64.urlsafe_b64encode(b"0" * 32)

    def encrypt(self, data):
        return base64.urlsafe_b64encode(self._key + b"|" + data)

    def decrypt(self, token):
        raw = base64.urlsafe_b64decode(token)
        key, _, data = raw.partition(b"|")
        if key != self._key:
            raise ValueError("bad key")
        return data


def _parse(argv: list[str]):
    parser = build_parser("0.0-test", lambda value: value)
    return parser.parse_args(argv)


class TestCredsCliSmoke(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_home = tempfile.TemporaryDirectory()
        self.proj = self._tmp.name
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=Path(self._tmp_home.name)),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._tmp.cleanup()
        self._tmp_home.cleanup()

    def _run(self, argv: list[str]) -> str:
        args = _parse(argv)
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_creds(args)
        return out.getvalue()

    def _entries(self, base: str) -> dict:
        cfg = Path(base) / ".c3" / "config.json"
        if not cfg.exists():
            return {}
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return data.get("credentials", {}).get("entries", {})

    def test_bare_creds_prints_usage(self):
        out = self._run(["creds"])
        self.assertIn("Usage: c3 creds", out)

    def test_set_and_list_project_scope(self):
        out = self._run(["creds", "set", "API_KEY", "--value", "sec",
                         "--desc", "test key", "--path", self.proj])
        self.assertIn("[OK] Stored credential 'API_KEY'", out)
        self.assertIn("API_KEY", self._entries(self.proj))
        listing = self._run(["creds", "list", "--path", self.proj])
        self.assertIn("API_KEY", listing)
        self.assertNotIn("sec", listing.replace("test key", ""))

    def test_set_global_scope(self):
        out = self._run(["creds", "set", "SHARED", "--value", "gv", "--global",
                         "--path", self.proj])
        self.assertIn("scope=global", out)
        self.assertEqual(self._stub.get_password("c3-creds", "global|SHARED"), "gv")
        self.assertIn("SHARED", self._entries(self._tmp_home.name))
        self.assertNotIn("SHARED", self._entries(self.proj))

    def test_agent_readable_warns(self):
        out = self._run(["creds", "set", "OPEN", "--value", "v",
                         "--agent-readable", "--path", self.proj])
        self.assertIn("[warn] agent_readable=true", out)

    def test_get_masked_and_show(self):
        self._run(["creds", "set", "K", "--value", "hidden-val", "--path", self.proj])
        masked = self._run(["creds", "get", "K", "--path", self.proj])
        self.assertIn("fingerprint=", masked)
        self.assertNotIn("hidden-val", masked)
        shown = self._run(["creds", "get", "K", "--show", "--path", self.proj])
        self.assertIn("hidden-val", shown)

    def test_rm_requires_global_flag_for_global_entry(self):
        self._run(["creds", "set", "G", "--value", "v", "--global", "--path", self.proj])
        out = self._run(["creds", "rm", "G", "--path", self.proj])
        self.assertIn("re-run with --global", out)
        out = self._run(["creds", "rm", "G", "--global", "--path", self.proj])
        self.assertIn("[OK] Removed credential 'G'", out)

    def test_import_env_file(self):
        env_file = Path(self.proj) / "test.env"
        env_file.write_text("FOO=bar\n# comment\nBAZ='q'\n", encoding="utf-8")
        out = self._run(["creds", "import", str(env_file), "--path", self.proj])
        self.assertIn("Imported 2 credential(s)", out)
        self.assertEqual(
            cs.get_value("FOO", project_path=self.proj), "bar"
        )


    def test_import_dry_run_writes_nothing(self):
        env_file = Path(self.proj) / "dry.env"
        env_file.write_text("D1=a\nD2=b\n", encoding="utf-8")
        out = self._run(["creds", "import", str(env_file), "--dry-run",
                         "--path", self.proj])
        self.assertIn("[dry-run]", out)
        self.assertIn("2 would be imported", out)
        self.assertIsNone(cs.get_value("D1", project_path=self.proj))

    def test_import_only_restricts_what_lands(self):
        env_file = Path(self.proj) / "only.env"
        env_file.write_text("O1=a\nO2=b\n", encoding="utf-8")
        self._run(["creds", "import", str(env_file), "--only", "O1",
                   "--path", self.proj])
        self.assertEqual(cs.get_value("O1", project_path=self.proj), "a")
        self.assertIsNone(cs.get_value("O2", project_path=self.proj))

    def test_import_reports_reasons_not_a_flat_list(self):
        env_file = Path(self.proj) / "why.env"
        env_file.write_text("W=1\n", encoding="utf-8")
        self._run(["creds", "import", str(env_file), "--path", self.proj])
        out = self._run(["creds", "import", str(env_file), "--path", self.proj])
        self.assertIn("already exists", out)

    def test_import_of_a_multiline_value_round_trips(self):
        pem = "-----BEGIN KEY-----\nAAAA\nBBBB\n-----END KEY-----"
        env_file = Path(self.proj) / "pem.env"
        env_file.write_text('PEM="%s"\n' % pem, encoding="utf-8")
        self._run(["creds", "import", str(env_file), "--path", self.proj])
        self.assertEqual(cs.get_value("PEM", project_path=self.proj), pem)

    def test_import_of_a_cp1252_file_does_not_traceback(self):
        """read_text(encoding='utf-8') raised past the except clause."""
        env_file = Path(self.proj) / "latin.env"
        env_file.write_bytes("CAFE=caf\u00e9\n".encode("cp1252"))
        out = self._run(["creds", "import", str(env_file), "--path", self.proj])
        self.assertIn("[OK]", out)
        self.assertEqual(cs.get_value("CAFE", project_path=self.proj), "caf\u00e9")


if __name__ == "__main__":
    unittest.main()
