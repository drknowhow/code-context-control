"""Access Guard T2 wiring tests — enforcement surfaces (docs/access-guard.md §3).

The evaluator itself is pinned by tests/test_access_guard.py (T1). This file
drives the wired surfaces: compressor/read/edit refusals, batch read
per-member behavior, search deny-ENUMERATE + the S4 footer, validate/filter/
impact read verdicts, ArtifactStore.restore write verdicts (and their tool-
boundary conversion), and the Oracle c3_bridge bypass path.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli.tools.artifacts import handle_artifacts  # noqa: E402
from cli.tools.compress import handle_compress  # noqa: E402
from cli.tools.edit import handle_edit  # noqa: E402
from cli.tools.filter import handle_filter  # noqa: E402
from cli.tools.impact import handle_impact  # noqa: E402
from cli.tools.read import handle_read  # noqa: E402
from cli.tools.search import handle_search  # noqa: E402
from cli.tools.validate import handle_validate  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services.artifact_store import ArtifactStore  # noqa: E402
from services.compressor import CodeCompressor  # noqa: E402

DENIED = "[c3-access:denied]"
READ_ONLY = "[c3-access:read_only]"
LIMITED = "[c3-access:limited]"


def _finalize(name, args, resp, summ="", **kw):
    return resp


def _no_facts(*_a, **_kw):
    return ""


class WiringBase(unittest.TestCase):
    """Mirrors GuardBase (temp project + .c3) plus the parity-test svc stub."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        # Isolate from the developer's real ~/.c3 access rules.
        self._home = patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.svc = self._make_svc()

    def tearDown(self):
        self._home.stop()
        self._tmp.cleanup()

    def _make_svc(self):
        svc = MagicMock()
        svc.project_path = str(self.proj)
        svc.hybrid_config = {}
        svc.session_mgr = None
        svc.edit_ledger = None
        svc.activity_log = None
        svc.validation_cache = None
        svc.memory = None
        return svc

    def _write_access(self, section):
        cfg = self.proj / ".c3" / "config.json"
        cfg.write_text(json.dumps({"access": section}), encoding="utf-8")

    def _write(self, rel, text="data\n"):
        p = self.proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class TestReadWiring(WiringBase):
    def test_denied_read_refused(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.txt", "TOPSECRET\n")
        resp = handle_read("secrets/k.txt", svc=self.svc, finalize=_finalize)
        self.assertIn(DENIED, resp)
        self.assertNotIn("TOPSECRET", resp)

    def test_probe_gets_same_refusal_when_target_missing(self):
        self._write_access({"deny": ["secrets/**"]})
        resp = handle_read("secrets/ghost.txt", svc=self.svc, finalize=_finalize)
        self.assertIn(DENIED, resp)
        self.assertNotIn("not found", resp.lower())

    def test_batch_read_serves_allowed_refuses_denied(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("ok.py", "x = 1\n")
        self._write("secrets/k.txt", "TOPSECRET\n")
        resp = handle_read("ok.py,secrets/k.txt", lines=[1, 1],
                           svc=self.svc, finalize=_finalize)
        self.assertIn("x = 1", resp)
        self.assertIn(DENIED, resp)
        self.assertNotIn("TOPSECRET", resp)


class TestEditWiring(WiringBase):
    def test_denied_write_refused(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.txt", "data\n")
        resp = handle_edit("secrets/k.txt", "data", "x", "", "", False,
                           self.svc, _finalize)
        self.assertIn(DENIED, resp)
        self.assertEqual((self.proj / "secrets/k.txt").read_text(encoding="utf-8"),
                         "data\n")

    def test_denied_create_refused(self):
        self._write_access({"deny": ["secrets/**"]})
        resp = handle_edit("secrets/new.txt", "", "content", "", "", False,
                           self.svc, _finalize)
        self.assertIn(DENIED, resp)
        self.assertFalse((self.proj / "secrets/new.txt").exists())

    def test_read_only_write_refused(self):
        self._write_access({"read_only": ["docs/**"]})
        self._write("docs/a.md", "hello\n")
        resp = handle_edit("docs/a.md", "hello", "world", "", "", False,
                           self.svc, _finalize)
        self.assertIn(READ_ONLY, resp)
        self.assertEqual((self.proj / "docs/a.md").read_text(encoding="utf-8"),
                         "hello\n")

    def test_allowed_edit_still_works(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("ok.txt", "hello\n")
        resp = handle_edit("ok.txt", "hello", "world", "", "", False,
                           self.svc, _finalize)
        self.assertNotIn(DENIED, resp)
        self.assertEqual((self.proj / "ok.txt").read_text(encoding="utf-8"),
                         "world\n")


class TestCompressWiring(WiringBase):
    def _compressor(self):
        return CodeCompressor(cache_dir=str(self.proj / ".c3" / "cache"),
                              project_root=str(self.proj))

    def test_compressor_raises_access_denied(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.py", "TOPSECRET = 1\n")
        comp = self._compressor()
        with self.assertRaises(ag.AccessDenied) as ctx:
            comp.compress_file(str(self.proj / "secrets/k.py"), "smart")
        self.assertIn(DENIED, ctx.exception.message)

    def test_handle_compress_refuses(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.py", "TOPSECRET = 1\n")
        self.svc.compressor = self._compressor()
        resp = handle_compress("secrets/k.py", "smart", self.svc,
                               _finalize, _no_facts)
        self.assertIn(DENIED, resp)
        self.assertNotIn("TOPSECRET", resp)

    def test_handle_compress_map_mode_refuses(self):
        # map/dense_map never reach compressor.compress_file — the handler
        # check must cover them too.
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.py", "TOPSECRET = 1\n")
        resp = handle_compress("secrets/k.py", "map", self.svc,
                               _finalize, _no_facts)
        self.assertIn(DENIED, resp)


class TestSearchWiring(WiringBase):
    _RESULTS = [
        {"file": "secrets/k.py", "lines": "1-2", "name": "", "type": "code",
         "content": "TOPSECRET", "tokens": 2, "score": 1.0, "file_tokens": 2},
        {"file": "ok.py", "lines": "1-2", "name": "", "type": "code",
         "content": "x = 1", "tokens": 2, "score": 0.9, "file_tokens": 2},
    ]

    def test_code_search_hides_denied_and_carries_footer(self):
        self._write_access({"deny": ["secrets/**"], "read_only": ["docs/**"]})
        self.svc.indexer.search = MagicMock(
            return_value=[dict(r) for r in self._RESULTS])
        resp = handle_search("k", "code", 3, 1200, self.svc,
                             _finalize, _no_facts)
        self.assertNotIn("secrets", resp)
        self.assertNotIn("TOPSECRET", resp)
        self.assertIn("ok.py", resp)
        self.assertIn(LIMITED, resp)

    def test_single_user_rule_still_carries_footer(self):
        # Regression: has_active_rules off-by-one hid S4 when exactly one
        # user rule existed in a dev checkout.
        self._write_access({"deny": ["secrets/**"]})
        self.svc.indexer.search = MagicMock(
            return_value=[dict(r) for r in self._RESULTS])
        resp = handle_search("k", "code", 3, 1200, self.svc,
                             _finalize, _no_facts)
        self.assertIn(LIMITED, resp)

    def test_files_search_hides_denied(self):
        self._write_access({"deny": ["secrets/**"]})
        self.svc.indexer.search = MagicMock(return_value=[
            {"file": "secrets/k.py", "lines": "1", "name": "", "type": "file"}])
        resp = handle_search("k", "files", 3, 1200, self.svc,
                             _finalize, _no_facts)
        self.assertNotIn("secrets/k.py", resp)
        self.assertIn(LIMITED, resp)

    def test_exact_search_hides_denied(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("ok.py", "needle = 1\n")
        self._write("secrets/k.py", "needle = 2\n")
        self.svc.file_memory.list_tracked = MagicMock(
            return_value=["ok.py", "secrets/k.py"])
        resp = handle_search("needle", "exact", 3, 1200, self.svc,
                             _finalize, _no_facts)
        self.assertIn("ok.py", resp)
        self.assertNotIn("secrets/k.py", resp)
        self.assertIn(LIMITED, resp)

    def test_no_rules_no_footer(self):
        self.svc.indexer.search = MagicMock(return_value=[
            dict(self._RESULTS[1])])
        resp = handle_search("k", "code", 3, 1200, self.svc,
                             _finalize, _no_facts)
        self.assertNotIn(LIMITED, resp)


class TestValidateWiring(WiringBase):
    def test_single_refused(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.py", "x = 1\n")
        resp = asyncio.run(handle_validate("secrets/k.py", self.svc, _finalize))
        self.assertIn(DENIED, resp)

    def test_batch_serves_allowed_refuses_denied(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("ok.py", "x = 1\n")
        self._write("secrets/k.py", "x = 1\n")
        resp = asyncio.run(
            handle_validate("ok.py,secrets/k.py", self.svc, _finalize))
        self.assertIn(DENIED, resp)
        self.assertIn("PASS", resp)


class TestFilterWiring(WiringBase):
    def test_file_mode_refused(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.txt", "TOPSECRET\n")
        resp = handle_filter("secrets/k.txt", "", "", 500, "smart", True,
                             self.svc, _finalize)
        self.assertIn(DENIED, resp)
        self.assertNotIn("TOPSECRET", resp)


class TestImpactWiring(WiringBase):
    def test_denied_reference_files_hidden(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("lib.py", "target_sym = 1\n")
        self._write("secrets/hidden.py", "target_sym = 2\n")
        resp = handle_impact("target_sym", "", "", self.svc, _finalize)
        self.assertIn("lib.py", resp)
        self.assertNotIn("secrets", resp)

    def test_denied_file_path_arg_refused(self):
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/hidden.py", "target_sym = 2\n")
        resp = handle_impact("target_sym", "secrets/hidden.py", "",
                             self.svc, _finalize)
        self.assertIn(DENIED, resp)


class TestArtifactRestoreWiring(WiringBase):
    def _two_versions(self, rel, v1, v2):
        store = ArtifactStore(str(self.proj))
        self._write(rel, v1)
        store.scan()
        self._write(rel, v2)
        store.scan()
        return store

    def test_restore_refused_by_user_rule(self):
        store = self._two_versions("CLAUDE.md", "v1\n", "v2\n")
        self._write_access({"read_only": ["CLAUDE.md"]})
        with self.assertRaises(ag.AccessDenied) as ctx:
            store.restore("instructions:CLAUDE.md", 1)
        self.assertIn("CLAUDE.md", ctx.exception.message)
        # Abort before any write: live file untouched.
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"),
                         "v2\n")

    def test_restore_settings_exempt_from_builtin_write_deny(self):
        # The builtin agent-surface write-deny on **/.claude/settings*.json
        # must NOT break restore — restore is the audited surface for exactly
        # those files. User rules (above) still enforce.
        store = self._two_versions(".claude/settings.local.json",
                                   '{"a": 1}', '{"a": 2}')
        res = store.restore("settings:.claude/settings.local.json", 1)
        self.assertTrue(res.get("restored"))

    def test_tool_boundary_converts_to_refusal_string(self):
        store = self._two_versions("CLAUDE.md", "v1\n", "v2\n")
        self._write_access({"read_only": ["CLAUDE.md"]})
        self.svc.artifact_store = store
        resp = handle_artifacts("restore", self.svc, _finalize,
                                artifact="instructions:CLAUDE.md", version=1)
        self.assertIn(READ_ONLY, resp)


class TestOracleBridgeWiring(WiringBase):
    def test_bridge_read_path_refused(self):
        from oracle.services.c3_bridge import C3Bridge
        self._write_access({"deny": ["secrets/**"]})
        self._write("secrets/k.txt", "TOPSECRET\n")
        bridge = C3Bridge.__new__(C3Bridge)  # skip heavy runtime-cache init
        bridge.get_runtime = lambda p: self.svc
        res = bridge.c3_read(str(self.proj), "secrets/k.txt")
        self.assertIn(DENIED, res["result"])
        self.assertNotIn("TOPSECRET", res["result"])


if __name__ == "__main__":
    unittest.main()
