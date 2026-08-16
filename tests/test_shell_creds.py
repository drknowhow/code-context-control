"""Tests for credential injection in cli/tools/shell.py.

Real subprocesses (python -c for portability), stubbed keyring. Verifies env
injection, {{cred:NAME}} expansion with the raw template kept in every log
surface, echo-back redaction, inert hostile project entries, and the
enable_creds=False proxy path used by c3_project.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.tools.shell import handle_shell
from services import credential_store as cs


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


class _Svc:
    def __init__(self, project_path):
        self.project_path = project_path


class TestShellCreds(unittest.TestCase):
    VALUE = "tok-abc-123-xyz"

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
        cs.set_credential("MY_TOKEN", self.VALUE, project_path=self.svc.project_path)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._tmp_proj.cleanup()
        self._tmp_home.cleanup()

    def _run(self, cmd, **kw):
        return asyncio.run(handle_shell(
            cmd, "", 60, False, False, self.svc,
            lambda *a, **k: a[2], **kw,
        ))

    def test_env_injection_and_echo_redaction(self):
        out = self._run(
            'python -c "import os; print(\'v=\' + os.environ.get(\'MY_TOKEN\', \'ABSENT\'))"',
            env_creds="MY_TOKEN",
        )
        self.assertIn("v=[cred:MY_TOKEN]", out)
        self.assertNotIn(self.VALUE, out)
        self.assertIn("--- creds ---", out)
        self.assertIn("injected: MY_TOKEN", out)

    def test_env_var_alias_honored(self):
        cs.update_metadata("MY_TOKEN", scope="project",
                          project_path=self.svc.project_path, env_var="ALIAS_VAR")
        out = self._run(
            'python -c "import os; print(\'v=\' + os.environ.get(\'ALIAS_VAR\', \'ABSENT\'))"',
            env_creds="MY_TOKEN",
        )
        self.assertIn("v=[cred:MY_TOKEN]", out)

    def test_template_expansion_keeps_raw_cmd_in_logs(self):
        out = self._run('python -c "print(\'x{{cred:MY_TOKEN}}x\')"')
        # child saw the decoded value (echoed output was scrubbed back)…
        self.assertIn("x[cred:MY_TOKEN]x", out)
        # …but the echoed $-line keeps the template form and the value
        # appears nowhere in the response body.
        self.assertIn("{{cred:MY_TOKEN}}", out.split("--- stdout ---")[0])
        self.assertNotIn(self.VALUE, out)

    def test_missing_requested_cred_fails_fast(self):
        out = self._run("python -c \"print('never runs')\"", env_creds="GHOST")
        self.assertIn("[c3_shell:error] unresolvable credential ref(s):", out)
        self.assertIn("GHOST: unknown credential", out)
        self.assertNotIn("never runs", out)

    def test_missing_template_cred_fails_fast(self):
        out = self._run('python -c "print(\'{{cred:GHOST}}\')"')
        self.assertIn("[c3_shell:error] unresolvable credential ref(s):", out)
        self.assertIn("GHOST: unknown credential", out)

    def test_inject_flag_auto_injects(self):
        cs.update_metadata("MY_TOKEN", scope="project",
                          project_path=self.svc.project_path, inject=True)
        out = self._run(
            'python -c "import os; print(\'v=\' + os.environ.get(\'MY_TOKEN\', \'ABSENT\'))"'
        )
        self.assertIn("v=[cred:MY_TOKEN]", out)

    def test_hostile_project_registry_entry_is_inert(self):
        # Global secret + a project config that registers the same name with
        # inject=true but holds no project-realm value: nothing must inject.
        cs.set_credential("GLOB_SECRET", "glob-value-987", scope="global",
                          project_path=self.svc.project_path)
        cfg_path = Path(self.svc.project_path) / ".c3" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.setdefault("credentials", {}).setdefault("entries", {})["GLOB_SECRET"] = {
            "type": "env", "storage": "keyring", "inject": True,
            "agent_readable": True, "env_var": "GLOB_SECRET",
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        out = self._run(
            'python -c "import os; print(\'v=\' + os.environ.get(\'GLOB_SECRET\', \'ABSENT\'))"'
        )
        self.assertIn("v=ABSENT", out)
        self.assertNotIn("glob-value-987", out)

    # ── structured kinds ──────────────────────────────────

    PAN = "4242424242424242"

    def _make_card(self):
        cs.set_credential("VISA", json.dumps(
            {"cardholder": "D T", "number": self.PAN, "expiry": "12/27"}),
            project_path=self.svc.project_path, ctype="card")

    def test_dotted_env_injection_and_redaction(self):
        self._make_card()
        out = self._run(
            'python -c "import os; print(\'n=\' + os.environ.get(\'VISA_NUMBER\', \'ABSENT\'))"',
            env_creds="VISA.number",
        )
        self.assertIn("n=[cred:VISA.number]", out)
        self.assertNotIn(self.PAN, out)
        self.assertIn("injected: VISA.number", out)

    def test_dotted_template_keeps_raw_form(self):
        self._make_card()
        out = self._run('python -c "print(\'x{{cred:VISA.number}}x\')"')
        self.assertIn("x[cred:VISA.number]x", out)
        self.assertIn("{{cred:VISA.number}}", out.split("--- stdout ---")[0])
        self.assertNotIn(self.PAN, out)

    def test_bare_structured_ref_fails_listing_fields(self):
        self._make_card()
        out = self._run("python -c \"print('never runs')\"", env_creds="VISA")
        self.assertIn("[c3_shell:error] unresolvable credential ref(s):", out)
        self.assertIn("structured (card)", out)
        self.assertIn("fields:", out)
        self.assertNotIn(self.PAN, out)
        self.assertNotIn("never runs", out)

    def test_hostile_inject_flip_on_structured_is_inert(self):
        self._make_card()
        cfg_path = Path(self.svc.project_path) / ".c3" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["credentials"]["entries"]["VISA"]["inject"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        out = self._run(
            'python -c "import os; '
            "print('v=' + os.environ.get('VISA', 'ABSENT')); "
            "print('n=' + os.environ.get('VISA_NUMBER', 'ABSENT'))\""
        )
        self.assertIn("v=ABSENT", out)
        self.assertIn("n=ABSENT", out)
        self.assertNotIn(self.PAN, out)

    def test_env_name_collision_is_a_hard_error(self):
        self._make_card()
        cs.set_credential("VISA_NUMBER", "plain-collider",
                          project_path=self.svc.project_path)
        out = self._run(
            "python -c \"print('never runs')\"",
            env_creds="VISA_NUMBER,VISA.number",
        )
        self.assertIn("[c3_shell:error] env-var collision", out)
        self.assertIn("VISA.number", out)
        self.assertIn("VISA_NUMBER", out)
        self.assertNotIn("never runs", out)

    def test_usage_events_recorded_per_ref_without_values(self):
        self._make_card()
        self._run('python -c "print(\'{{cred:VISA.number}}\')"',
                  env_creds="MY_TOKEN")
        log = Path(self.svc.project_path) / ".c3" / "cred_usage.jsonl"
        text = log.read_text(encoding="utf-8")
        # telemetry hygiene: decoded values never in the usage log
        self.assertNotIn(self.PAN, text)
        self.assertNotIn(self.VALUE, text)
        events = [json.loads(x) for x in text.splitlines()]
        shapes = {(e["name"], e["field"], e["action"]) for e in events}
        self.assertIn(("VISA", "number", "template"), shapes)
        self.assertIn(("MY_TOKEN", "", "inject_env"), shapes)
        for e in events:
            self.assertIn("{{cred:VISA.number}}", e["cmd"])  # raw template form
            self.assertEqual(e["surface"], "shell")
            self.assertEqual(e["exit"], 0)

    def test_enable_creds_false_disables_expansion_and_injection(self):
        out = self._run(
            'python -c "import os; print(os.environ.get(\'MY_TOKEN\', \'ABSENT\'))"',
            env_creds="MY_TOKEN", enable_creds=False,
        )
        self.assertIn("ABSENT", out)
        out2 = self._run("echo '{{cred:MY_TOKEN}}'", enable_creds=False)
        self.assertNotIn("[c3_shell:error]", out2)
        self.assertNotIn(self.VALUE, out2)


if __name__ == "__main__":
    unittest.main()
