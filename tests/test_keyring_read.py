"""keyring_read answers like keyring.get_password, reading WinVault directly only
when keyring would read WinVault itself.

Importing keyring costs a hook process about 250 ms (imports plus an
entry-point scan to pick a backend); the vault read it ends in is one or two
CredReadW calls. These tests pin when the direct read is allowed, the cached
answer to which packages register keyring backends, the lookup order the
read copies from WinVaultKeyring, and the fallback to keyring for
everything else.
"""
from __future__ import annotations

import importlib.metadata  # noqa: F401 — loaded before TestScanBackends patches them
import importlib.util  # noqa: F401
import json
import os
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from services import keyring_read as kr

WIN = sys.platform == "win32"


class _FakeKeyring:
    def __init__(self, value="from-keyring"):
        self.value = value
        self.calls = []

    def get_password(self, service, username):
        self.calls.append((service, username))
        return self.value


class TestWhenTheVaultIsKeyringsChoice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.appdata = Path(self._tmp.name)
        env = mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.appdata)})
        env.start()
        self.addCleanup(env.stop)
        mods = mock.patch.dict(sys.modules)
        mods.start()
        self.addCleanup(mods.stop)
        sys.modules.pop("keyring", None)
        self.own_only = mock.Mock(return_value=True)
        backends = mock.patch.object(kr, "_keyrings_own_backends_only", self.own_only)
        backends.start()
        self.addCleanup(backends.stop)

    @unittest.skipUnless(WIN, "WinVault is the Windows backend")
    def test_windows_with_no_backend_configured(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("PYTHON_KEYRING_BACKEND", None)
            self.assertTrue(kr._vault_is_keyrings_choice())

    @unittest.skipUnless(WIN, "WinVault is the Windows backend")
    def test_another_package_registers_a_backend(self):
        """keyring would chain it with the vault, so it may answer first."""
        self.own_only.return_value = False
        with mock.patch.dict("os.environ"):
            os.environ.pop("PYTHON_KEYRING_BACKEND", None)
            self.assertFalse(kr._vault_is_keyrings_choice())

    @unittest.skipUnless(WIN, "WinVault is the Windows backend")
    def test_winvault_named_in_the_environment(self):
        with mock.patch.dict("os.environ", {"PYTHON_KEYRING_BACKEND": kr._WINVAULT}):
            self.assertTrue(kr._vault_is_keyrings_choice())

    def test_another_backend_named_in_the_environment(self):
        with mock.patch.dict("os.environ",
                             {"PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring"}):
            self.assertFalse(kr._vault_is_keyrings_choice())

    def test_a_keyring_config_file(self):
        cfg = self.appdata / "Python Keyring" / "keyringrc.cfg"
        cfg.parent.mkdir()
        cfg.write_text("[backend]\ndefault-keyring=keyrings.alt.file.PlaintextKeyring\n",
                       encoding="utf-8")
        self.assertFalse(kr._vault_is_keyrings_choice())

    def test_keyring_already_imported(self):
        """It may have another backend set, and tests install fakes there."""
        sys.modules["keyring"] = _FakeKeyring()
        self.assertFalse(kr._vault_is_keyrings_choice())

    def test_other_systems(self):
        with mock.patch.object(kr.sys, "platform", "linux"):
            self.assertFalse(kr._vault_is_keyrings_choice())


class TestGetPassword(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeKeyring()
        mods = mock.patch.dict(sys.modules, {"keyring": self.fake})
        mods.start()
        self.addCleanup(mods.stop)

    def direct(self, **kwargs):
        return mock.patch.multiple(kr, _vault_is_keyrings_choice=mock.Mock(return_value=True),
                                   **kwargs)

    def test_a_vault_hit_skips_keyring(self):
        with self.direct(_vault_get=mock.Mock(return_value="secret")):
            self.assertEqual(kr.get_password("svc", "user"), "secret")
        self.assertEqual(self.fake.calls, [])

    def test_an_empty_secret_is_an_answer(self):
        with self.direct(_vault_get=mock.Mock(return_value="")):
            self.assertEqual(kr.get_password("svc", "user"), "")
        self.assertEqual(self.fake.calls, [])

    def test_a_vault_miss_is_an_answer(self):
        """With the vault as keyring's only backend, keyring would say None too."""
        with self.direct(_vault_get=mock.Mock(return_value=None)):
            self.assertIsNone(kr.get_password("svc", "user"))
        self.assertEqual(self.fake.calls, [])

    def test_a_ctypes_error_asks_keyring(self):
        with self.direct(_vault_get=mock.Mock(side_effect=OSError(5, "denied"))):
            self.assertEqual(kr.get_password("svc", "user"), "from-keyring")

    def test_keyring_errors_reach_the_caller(self):
        self.fake.get_password = mock.Mock(side_effect=RuntimeError("no backend"))
        with mock.patch.object(kr, "_vault_is_keyrings_choice", return_value=False):
            with self.assertRaises(RuntimeError):
                kr.get_password("svc", "user")


class TestBackendsCache(unittest.TestCase):
    """Which packages register keyring backends, cached against the mtime of
    every sys.path entry."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.dirs = [base / name for name in ("a", "b", "c")]
        for d in self.dirs:
            d.mkdir()
            self.age(d, 3600)
        self.scan = mock.Mock(return_value=True)
        for patcher in (
            mock.patch.dict("os.environ", {"C3_HOME": str(base / "home")}),
            mock.patch.object(kr.sys, "path", [str(d) for d in self.dirs[:2]]),
            mock.patch.object(kr, "_scan_backends", self.scan),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def age(path, seconds):
        then = time.time() - seconds
        os.utime(path, (then, then))

    def test_cached_until_a_sys_path_directory_changes(self):
        self.assertTrue(kr._keyrings_own_backends_only())
        self.assertTrue(kr._keyrings_own_backends_only())
        self.assertEqual(self.scan.call_count, 1)
        self.assertTrue(kr._backends_cache_file().is_file())
        self.age(self.dirs[1], 1800)  # a package was installed or removed there
        self.scan.return_value = False
        self.assertFalse(kr._keyrings_own_backends_only())
        self.assertEqual(self.scan.call_count, 2)

    def test_a_directory_changed_just_now_is_not_cached(self):
        os.utime(self.dirs[0])
        kr._keyrings_own_backends_only()
        kr._keyrings_own_backends_only()
        self.assertEqual(self.scan.call_count, 2)

    def test_each_sys_path_keeps_its_own_answer(self):
        kr._keyrings_own_backends_only()
        with mock.patch.object(kr.sys, "path", [str(self.dirs[2])]):
            kr._keyrings_own_backends_only()
            kr._keyrings_own_backends_only()
        kr._keyrings_own_backends_only()
        self.assertEqual(self.scan.call_count, 2)

    def test_the_oldest_sys_path_is_dropped_past_the_limit(self):
        with mock.patch.object(kr, "_BACKENDS_CACHE_MAX", 2):
            for d in self.dirs:
                with mock.patch.object(kr.sys, "path", [str(d)]):
                    kr._keyrings_own_backends_only()
            data = json.loads(kr._backends_cache_file().read_text(encoding="utf-8"))
            self.assertEqual(sorted(data), [str(self.dirs[1]), str(self.dirs[2])])
            with mock.patch.object(kr.sys, "path", [str(self.dirs[0])]):
                kr._keyrings_own_backends_only()
        self.assertEqual(self.scan.call_count, 4)

    def test_an_unreadable_cache_is_scanned_again(self):
        kr._keyrings_own_backends_only()
        kr._backends_cache_file().write_text("{not json", encoding="utf-8")
        self.assertTrue(kr._keyrings_own_backends_only())
        kr._keyrings_own_backends_only()
        self.assertEqual(self.scan.call_count, 2)

    def test_a_scan_error_hands_the_read_to_keyring(self):
        self.scan.side_effect = RuntimeError("unreadable metadata")
        self.assertFalse(kr._keyrings_own_backends_only())
        self.assertFalse(kr._backends_cache_file().exists())


class TestScanBackends(unittest.TestCase):
    """keyring's own entry points only, and a win32cred to read through."""

    OWN = ("keyring.backends.Windows", "keyring.backends.chainer",
           "keyring.backends.macOS")

    @staticmethod
    def ep(value, dist="keyring"):
        return types.SimpleNamespace(value=value, dist=types.SimpleNamespace(name=dist))

    def scan(self, eps, importable=("win32ctypes",)):
        with mock.patch("importlib.metadata.entry_points", return_value=eps), \
                mock.patch("importlib.util.find_spec",
                           side_effect=lambda name: object() if name in importable else None):
            return kr._scan_backends()

    def test_keyrings_own_backends(self):
        self.assertTrue(self.scan([self.ep(v) for v in self.OWN]))

    def test_pywin32_in_place_of_win32ctypes(self):
        self.assertTrue(self.scan([self.ep(v) for v in self.OWN], importable=("win32cred",)))

    def test_another_package_registers_a_backend(self):
        eps = [self.ep(v) for v in self.OWN] + [self.ep("keyrings.alt.Windows", "keyrings.alt")]
        self.assertFalse(self.scan(eps))

    def test_keyring_is_not_installed(self):
        self.assertFalse(self.scan([]))

    def test_no_win32cred_to_read_through(self):
        self.assertFalse(self.scan([self.ep(v) for v in self.OWN], importable=()))

    def test_the_real_scan_answers(self):
        self.assertIsInstance(kr._scan_backends(), bool)


class TestVaultLookupOrder(unittest.TestCase):
    """WinVaultKeyring reads the service target, then ``user@service``."""

    def read_from(self, creds):
        return mock.patch.object(kr, "_cred_read", side_effect=lambda target: creds.get(target))

    def test_the_service_target_for_its_user(self):
        with self.read_from({"svc": ("user", "s3".encode("utf-16"))}):
            self.assertEqual(kr._vault_get("svc", "user"), "s3")

    def test_the_compound_target_when_the_service_holds_another_user(self):
        creds = {"svc": ("other", "nope".encode("utf-16")),
                 "user@svc": ("user", "yes".encode("utf-16"))}
        with self.read_from(creds):
            self.assertEqual(kr._vault_get("svc", "user"), "yes")

    def test_the_compound_target_when_the_service_is_missing(self):
        with self.read_from({"user@svc": ("user", "yes".encode("utf-16"))}):
            self.assertEqual(kr._vault_get("svc", "user"), "yes")

    def test_nothing_found(self):
        with self.read_from({"svc": ("other", b"x\x00")}):
            self.assertIsNone(kr._vault_get("svc", "user"))

    def test_a_utf8_blob_decodes_like_keyring(self):
        with self.read_from({"svc": ("user", b"abc")}):  # odd length: not UTF-16
            self.assertEqual(kr._vault_get("svc", "user"), "abc")


@unittest.skipUnless(WIN, "CredReadW is Windows-only")
class TestCredRead(unittest.TestCase):
    def test_a_missing_target_reads_as_none(self):
        self.assertIsNone(kr._cred_read(f"c3-test-missing-{uuid.uuid4().hex}"))


if __name__ == "__main__":
    unittest.main()
