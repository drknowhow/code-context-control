"""Vault write-guard hotfix (v2.61.2) — closes the agent_readable escalation.

Attack: the agent edits .c3/config.json (c3_edit, native Write, or shell),
flips a credential's agent_readable flag, then calls reveal. Three guards:
  1. c3_edit refuses vault files (registry + sidecar state), all modes.
  2. The PreToolUse hook denies native Edit/Write/MultiEdit on vault files
     even when a warm c3 signal or sticky unlock would otherwise allow them.
  3. reveal requires a keyring attestation of agent_readable written only by
     the credentials API — a registry edited outside the API fails closed.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

# hook_pretool_enforce imports plain `_hook_utils`; alias so both spellings
# resolve to the same module instance (mirrors cli/hook_dispatch.py).
sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hook_pretool_enforce as hook  # noqa: E402
from cli.tools.credentials import handle_credentials  # noqa: E402
from cli.tools.edit import handle_edit  # noqa: E402
from services import credential_store as cs  # noqa: E402


class _StubKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

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


class _StubLedger:
    def __init__(self):
        self.edits: list[dict] = []

    def log_edit(self, **kw):
        self.edits.append(kw)


class _StubActivity:
    def __init__(self):
        self.events: list[tuple] = []

    def log(self, kind, data):
        self.events.append((kind, data))


class _Svc:
    def __init__(self, project_path):
        self.project_path = project_path
        self.edit_ledger = _StubLedger()
        self.activity_log = _StubActivity()


def _finalize(tool, args, resp, summary, **kw):
    return resp


class TestParity(unittest.TestCase):
    def test_hook_set_matches_store_set(self):
        self.assertEqual(hook._VAULT_FILES, cs.VAULT_PROTECTED_FILES)


class TestEditVaultGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        (self.proj / ".c3" / "config.json").write_text("{}", encoding="utf-8")
        self.svc = _Svc(str(self.proj))

    def tearDown(self):
        self._tmp.cleanup()

    def _edit(self, rel, old, new, edits=""):
        return handle_edit(rel, old, new, summary="", tags="",
                           replace_all=False, svc=self.svc,
                           finalize=_finalize, edits=edits)

    def test_edit_config_json_refused(self):
        resp = self._edit(".c3/config.json", "{}", '{"x": 1}')
        self.assertIn("[c3:vault-protected]", resp)
        text = (self.proj / ".c3" / "config.json").read_text(encoding="utf-8")
        self.assertEqual(text, "{}")

    def test_create_mode_refused_for_missing_vault_file(self):
        resp = self._edit(".c3/cred_state.json", "", '{"pwned": true}')
        self.assertIn("[c3:vault-protected]", resp)
        self.assertFalse((self.proj / ".c3" / "cred_state.json").exists())

    def test_batch_mode_refused(self):
        edits = json.dumps([{"old_string": "{}", "new_string": "{}"}])
        resp = self._edit(".c3/config.json", "", "", edits=edits)
        self.assertIn("[c3:vault-protected]", resp)

    def test_secrets_enc_refused(self):
        resp = self._edit(".c3/secrets.enc", "", "AAAA")
        self.assertIn("[c3:vault-protected]", resp)

    def test_absolute_path_refused(self):
        resp = self._edit(str(self.proj / ".c3" / "config.json"), "{}", "{ }")
        self.assertIn("[c3:vault-protected]", resp)

    def test_case_insensitive_match(self):
        resp = self._edit(".C3/Config.JSON", "{}", "{ }")
        self.assertIn("[c3:vault-protected]", resp)

    def test_normal_file_still_editable(self):
        (self.proj / "a.txt").write_text("hello", encoding="utf-8")
        resp = self._edit("a.txt", "hello", "world")
        self.assertIn("✓", resp)
        self.assertEqual((self.proj / "a.txt").read_text(encoding="utf-8"), "world")

    def test_config_json_outside_c3_dir_not_guarded(self):
        (self.proj / "config.json").write_text("{}", encoding="utf-8")
        resp = self._edit("config.json", "{}", '{"ok": 1}')
        self.assertIn("✓", resp)


class TestHookVaultGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, tool_name, file_path):
        payload = {"tool_name": tool_name, "tool_input": {"file_path": file_path}}
        return hook.run(payload, project_path=self.proj)

    def _assert_vault_deny(self, out):
        self.assertIsNotNone(out)
        decision = out.get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        self.assertIn("[c3:vault-protected]",
                      decision.get("permissionDecisionReason", ""))

    def test_write_tools_denied_on_vault_files(self):
        vault = str(self.proj / ".c3" / "config.json")
        for tool in ("Edit", "Write", "MultiEdit"):
            self._assert_vault_deny(self._run(tool, vault))

    def test_denied_even_when_c3_prereq_satisfied(self):
        # Force the unlock check to allow — the vault guard must fire first.
        with mock.patch.object(hook, "_check_c3_used",
                               return_value=(True, "signal")) as checked:
            out = self._run("Write", str(self.proj / ".c3" / "config.json"))
            self._assert_vault_deny(out)
            checked.assert_not_called()

    def test_read_of_vault_file_not_vault_denied(self):
        out = self._run("Read", str(self.proj / ".c3" / "config.json"))
        reason = (out or {}).get("hookSpecificOutput", {}).get(
            "permissionDecisionReason", "")
        self.assertNotIn("[c3:vault-protected]", reason)

    def test_non_vault_write_gets_normal_enforcement(self):
        out = self._run("Write", str(self.proj / "notes.txt"))
        decision = (out or {}).get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        self.assertIn("[c3:enforce]", decision.get("permissionDecisionReason", ""))


class TestRevealIntegrity(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._tmp_proj = tempfile.TemporaryDirectory()
        self._tmp_home = tempfile.TemporaryDirectory()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=Path(self._tmp_home.name)),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()
        self.svc = _Svc(self._tmp_proj.name)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._tmp_proj.cleanup()
        self._tmp_home.cleanup()

    def _creds(self, action, **kwargs):
        return handle_credentials(action, self.svc, _finalize, **kwargs)

    def _tamper_flag(self, name, value=True):
        cfg_path = Path(self.svc.project_path) / ".c3" / "config.json"
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
        config["credentials"]["entries"][name]["agent_readable"] = value
        cfg_path.write_text(json.dumps(config), encoding="utf-8")

    def test_tampered_registry_flag_fails_closed(self):
        self._creds("set", name="API_KEY", value="s3cret")
        self.assertIn("[creds:not-readable]", self._creds("reveal", name="API_KEY"))
        self._tamper_flag("API_KEY")
        resp = self._creds("reveal", name="API_KEY")
        self.assertIn("[creds:integrity]", resp)
        self.assertNotIn("s3cret", resp)

    def test_creation_time_agent_readable_reveals(self):
        self._creds("set", name="MINE", value="agent-made", agent_readable=True)
        resp = self._creds("reveal", name="MINE")
        self.assertIn("[creds:reveal]", resp)
        self.assertIn("agent-made", resp)

    def test_user_toggle_via_update_metadata_reveals(self):
        self._creds("set", name="TOKEN", value="tok-1")
        cs.update_metadata("TOKEN", scope="project",
                           project_path=self.svc.project_path,
                           agent_readable=True)
        resp = self._creds("reveal", name="TOKEN")
        self.assertIn("[creds:reveal]", resp)
        self.assertIn("tok-1", resp)

    def test_missing_attestation_fails_closed(self):
        # Legacy entry: registry says readable but no keyring attestation.
        self._creds("set", name="OLD", value="v", agent_readable=True)
        flag_keys = [k for k in self._stub.store if k[1].endswith("::agent_readable")]
        for k in flag_keys:
            del self._stub.store[k]
        self.assertIn("[creds:integrity]", self._creds("reveal", name="OLD"))

    def test_delete_removes_attestation(self):
        self._creds("set", name="GONE", value="v", agent_readable=True)
        self.assertTrue(
            any(k[1].endswith("::agent_readable") for k in self._stub.store))
        self._creds("delete", name="GONE", scope="project")
        self.assertFalse(
            any(k[1].endswith("::agent_readable") for k in self._stub.store))

    def test_verify_false_on_keyring_failure(self):
        self._creds("set", name="KR", value="v", agent_readable=True)
        with mock.patch.object(cs, "_keyring_module",
                               side_effect=RuntimeError("keyring down")):
            self.assertFalse(cs.verify_agent_readable(
                "KR", scope="project", project_path=self.svc.project_path))


if __name__ == "__main__":
    unittest.main()
