"""services/credential_backup.py: a keychain wipe is recoverable with the passphrase.

The keyring is an in-memory stub; wiping it is ``store.clear()``, which is what
Windows did to Credential Manager. Crypto is real (cryptography), with a small
scrypt cost so the suite stays fast.
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

from services import credential_backup as cb  # noqa: E402
from services import credential_store as cs  # noqa: E402

PASS = "correct horse battery"
CANARY = "canary-7f3a9c-secret"
CARD = json.dumps({"cardholder": "D T", "number": "4539578763621486",
                   "expiry": "12/27"})


class _StubKeyring:
    def __init__(self):
        self.store: dict = {}

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


class _BackupBase(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._home = tempfile.TemporaryDirectory()
        self._proj = tempfile.TemporaryDirectory()
        self.home = Path(self._home.name)
        self.proj = self._proj.name
        (Path(self.proj) / ".c3").mkdir()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_global_base", return_value=self.home),
            mock.patch.object(cb, "SCRYPT_N", 2 ** 10),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._home.cleanup()
        self._proj.cleanup()

    def wipe_keychain(self):
        self._stub.store.clear()


class TestCredentialBackup(_BackupBase):
    def test_set_reports_off_until_init(self):
        entry = cs.set_credential("TOK", "v", scope="global", project_path=self.proj)
        self.assertEqual(entry["backup"], "off")
        self.assertFalse(cb.backup_path().exists())

    def test_keychain_wipe_is_restored(self):
        cb.init(PASS)
        big = "x" * (cs.FILE_STORAGE_THRESHOLD + 50)
        self.assertEqual(cs.set_credential(
            "GTOK", CANARY, scope="global", project_path=self.proj,
            env_var="gtok_env")["backup"], "saved")
        cs.set_credential("PTOK", "proj-v", scope="project", project_path=self.proj)
        cs.set_credential("BIG", big, scope="global", project_path=self.proj)
        cs.set_credential("CARD", CARD, scope="global", project_path=self.proj,
                          ctype="card")
        self.wipe_keychain()
        self.assertFalse(cs.is_resolvable("GTOK", project_path=self.proj))

        out = cb.restore(PASS)

        self.assertEqual(len(out["restored"]), 4)
        self.assertEqual(out["failed"], [])
        self.assertEqual(cs.get_value("GTOK", project_path=self.proj), CANARY)
        self.assertEqual(cs.get_value("PTOK", project_path=self.proj), "proj-v")
        self.assertEqual(cs.get_value("BIG", project_path=self.proj), big)
        self.assertEqual(cs.get_value("CARD", project_path=self.proj,
                                      field="expiry"), "12/27")
        self.assertEqual(cs.structured_type("CARD", project_path=self.proj), "card")
        self.assertEqual(cs.get_entry("GTOK", project_path=self.proj)["env_var"],
                         "gtok_env")

    def test_backup_file_holds_no_plaintext(self):
        cb.init(PASS)
        cs.set_credential("GTOK", CANARY, scope="global", project_path=self.proj)
        text = cb.backup_path().read_text(encoding="utf-8")
        self.assertNotIn(CANARY, text)
        self.assertNotIn(PASS, text)

    def test_wrong_passphrase_restores_nothing(self):
        cb.init(PASS)
        cs.set_credential("GTOK", CANARY, scope="global", project_path=self.proj)
        self.wipe_keychain()
        with self.assertRaisesRegex(cb.BackupError, "wrong passphrase"):
            cb.restore("not the passphrase")
        self.assertFalse(cs.is_resolvable("GTOK", project_path=self.proj))

    def test_restore_leaves_a_live_value_alone(self):
        cb.init(PASS)
        cs.set_credential("GTOK", "old", scope="global", project_path=self.proj)
        self._stub.set_password("c3-creds", "global|GTOK", "newer")
        out = cb.restore(PASS)
        self.assertEqual(out["present"], ["GTOK"])
        self.assertEqual(cs.get_value("GTOK", project_path=self.proj), "newer")

    def test_deleted_entry_stays_deleted(self):
        cb.init(PASS)
        cs.set_credential("GONE", "v", scope="global", project_path=self.proj)
        cs.delete_credential("GONE", scope="global", project_path=self.proj)
        self.assertNotIn("GONE", json.dumps(json.loads(
            cb.backup_path().read_text(encoding="utf-8"))["records"]))
        self.assertEqual(cb.restore(PASS)["restored"], [])
        self.assertEqual(cs.get_entry("GONE", project_path=self.proj), {})

    def test_sync_covers_values_set_before_init(self):
        cs.set_credential("EARLY", "v", scope="global", project_path=self.proj)
        cb.init(PASS)
        self.assertEqual(cb.status([self.proj])["not_backed_up"], ["EARLY"])
        self.assertEqual(cb.sync([self.proj])["saved"], 1)
        self.assertEqual(cb.status([self.proj])["not_backed_up"], [])
        self.wipe_keychain()
        self.assertEqual(cb.status([self.proj])["restorable"], ["EARLY"])
        self.assertEqual(cb.restore(PASS)["restored"], ["EARLY"])

    def test_status_names_values_lost_without_a_copy(self):
        cs.set_credential("EARLY", "v", scope="global", project_path=self.proj)
        cb.init(PASS)
        self.wipe_keychain()
        self.assertEqual(cb.status([self.proj])["lost"], ["EARLY"])

    def test_restored_reveal_entry_comes_back_injection_only(self):
        cb.init(PASS)
        cs.set_credential("OPEN", "v", scope="global", project_path=self.proj,
                          agent_readable=True)
        self.wipe_keychain()
        cb.restore(PASS)
        self.assertFalse(cs.get_entry("OPEN", project_path=self.proj)["agent_readable"])
        self.assertFalse(cs.verify_agent_readable(
            "OPEN", scope="global", project_path=self.proj))

    def test_change_passphrase(self):
        cb.init(PASS)
        cs.set_credential("GTOK", CANARY, scope="global", project_path=self.proj)
        cb.change_passphrase(PASS, "a different passphrase")
        self.wipe_keychain()
        with self.assertRaises(cb.BackupError):
            cb.restore(PASS)
        self.assertEqual(cb.restore("a different passphrase")["restored"], ["GTOK"])

    def test_init_refuses_short_passphrase_and_a_second_init(self):
        with self.assertRaisesRegex(cb.BackupError, "at least"):
            cb.init("short")
        cb.init(PASS)
        with self.assertRaisesRegex(cb.BackupError, "already exists"):
            cb.init(PASS)

    def test_record_cannot_be_moved_to_another_name(self):
        cb.init(PASS)
        cs.set_credential("A", "value-a", scope="global", project_path=self.proj)
        cs.set_credential("B", "value-b", scope="global", project_path=self.proj)
        data = json.loads(cb.backup_path().read_text(encoding="utf-8"))
        data["records"]["global"]["B"] = data["records"]["global"]["A"]
        cb.backup_path().write_text(json.dumps(data), encoding="utf-8")
        self.wipe_keychain()
        out = cb.restore(PASS)
        self.assertEqual(out["restored"], ["A"])
        self.assertEqual([f.split(":")[0] for f in out["failed"]], ["B"])


class TestAgentHint(_BackupBase):
    def _list(self):
        from cli.tools.credentials import handle_credentials
        svc = mock.Mock(project_path=self.proj, edit_ledger=None, activity_log=None)
        return handle_credentials("list", svc, lambda tool, args, resp, summary: resp)

    def test_list_points_to_restore_only_when_a_backup_exists(self):
        cs.set_credential("GTOK", CANARY, scope="global", project_path=self.proj)
        self.wipe_keychain()
        self.assertIn("No vault backup", self._list())
        cb.init(PASS)
        self.assertIn("c3 creds backup restore", self._list())


class TestBackupCli(_BackupBase):
    def _run(self, argv, *, tty=True, secrets=()):
        from cli.c3 import cmd_creds
        from cli.commands.parser import build_parser
        args = build_parser("0.0-test", lambda value: value).parse_args(
            argv + ["--path", self.proj])
        out = io.StringIO()
        with mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                mock.patch("getpass.getpass", side_effect=list(secrets)), \
                mock.patch("cli.c3._creds_known_projects", return_value=[self.proj]), \
                redirect_stdout(out):
            cmd_creds(args)
        return out.getvalue()

    def test_init_refuses_without_a_terminal(self):
        out = self._run(["creds", "backup", "init"], tty=False)
        self.assertIn("own terminal", out)
        self.assertFalse(cb.is_enabled())

    def test_init_then_restore_round_trip(self):
        cs.set_credential("GTOK", CANARY, scope="global", project_path=self.proj)
        out = self._run(["creds", "backup", "init"], secrets=[PASS, PASS])
        self.assertIn("1 value", out)
        self.wipe_keychain()
        self.assertIn("GTOK", self._run(["creds", "backup", "status"]))
        out = self._run(["creds", "backup", "restore"], secrets=[PASS])
        self.assertIn("Restored 1", out)
        self.assertNotIn(CANARY, out)
        self.assertEqual(cs.get_value("GTOK", project_path=self.proj), CANARY)

    def test_init_rejects_mismatched_repeat(self):
        out = self._run(["creds", "backup", "init"],
                        secrets=[PASS, PASS + "x"])
        self.assertIn("did not match", out)
        self.assertFalse(cb.is_enabled())


if __name__ == "__main__":
    unittest.main()
