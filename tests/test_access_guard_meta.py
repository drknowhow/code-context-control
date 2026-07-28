"""T5 meta-tests — structural guards against Access Guard coverage drift.

The board's top residual risk: correctness depends on every current AND
future file-touching call site routing through the guarded service layer.
These tests make drift a CI failure instead of a silent hole:

1. Every tool-handler module under cli/tools/ that performs raw file I/O
   must import services.access_guard (or be explicitly allowlisted WITH a
   reason). A new tool that opens files without consulting the guard fails
   here first.
2. Enforcement-adjacent modules must not call Path.resolve() directly —
   path identity flows through ONE canonicalizer (access_guard.canonicalize
   / _hook_utils.canonical_key). The first drive-by resolve() reintroduces
   the case/prefix mismatch bypasses the shipped regressions can't see.
3. Denial-storm regression: a burst of non-tool_call activity entries after
   a c3 signal must not evict hook_pretool_enforce's evidence window.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)


_RAW_IO = re.compile(
    r"(?<![\w.])open\(|\.write_text\(|\.read_text\(|\.write_bytes\(|"
    r"\.read_bytes\(|shutil\.copy|os\.remove|os\.replace|\.unlink\("
)

# cli/tools modules allowed to do raw I/O WITHOUT importing access_guard.
# Every entry needs a reason. Adding a file here is a reviewed decision.
_TOOLS_IO_ALLOWLIST = {
    "credentials.py",   # vault API — its own privileged store, guarded by
                        # VAULT_PROTECTED_FILES + builtin .c3 write-denies
    "session.py",       # session snapshots — .c3-internal state, not
                        # user-project files (builtin covers .c3 writes)
    "memory.py",        # memory store — .c3-internal state
    "tasks.py",         # task/pm store — .c3-internal state
    "federate.py",      # federation surface — .c3-internal state
    "_helpers.py",      # shared plumbing for the handlers above
    "status.py",        # health probes — reads .c3-internal state
    "edits.py",         # edit-ledger reader — .c3-internal state
    "agent.py",         # delegates to guarded handlers for file access
    "bitbucket.py",     # remote API surface, no local user-file I/O paths
    "jira.py",          # remote API surface
    "artifacts.py",     # ArtifactStore raises AccessDenied at service layer
}

# Enforcement-adjacent files: direct Path.resolve() banned outside the
# single canonicalizer implementations.
_RESOLVE_BAN = {
    "cli/hook_access_guard.py": set(),
    "cli/hook_pretool_enforce.py": set(),
    # canonical_key IS the canonicalizer here; nothing else may resolve.
    "cli/_hook_utils.py": {"canonical_key"},
    # canonicalize + module-level root computations own resolution.
    "services/access_guard.py": {"canonicalize", "_install_dir_rule",
                                 # load_all owns scope-base resolution for
                                 # both load_rules and load_mask_rules.
                                 "load_all", "load_rules", "check",
                                 # config-store plumbing (scope dirs), not
                                 # user-path evaluation:
                                 "_scope_config_path"},
}


class TestToolIoRoutesThroughGuard(unittest.TestCase):
    def test_every_io_tool_imports_access_guard_or_is_allowlisted(self):
        offenders = []
        for py in sorted((REPO_ROOT / "cli" / "tools").glob("*.py")):
            if py.name == "__init__.py" or py.name in _TOOLS_IO_ALLOWLIST:
                continue
            src = py.read_text(encoding="utf-8")
            if _RAW_IO.search(src) and "access_guard" not in src:
                offenders.append(py.name)
        self.assertEqual(
            offenders, [],
            f"cli/tools modules with raw file I/O but no access_guard "
            f"import: {offenders}. Wire the guard or allowlist WITH a "
            f"reason in test_access_guard_meta.py.")

    def test_allowlist_entries_still_exist(self):
        stale = [n for n in _TOOLS_IO_ALLOWLIST
                 if not (REPO_ROOT / "cli" / "tools" / n).is_file()]
        self.assertEqual(stale, [], f"stale allowlist entries: {stale}")

    def test_guarded_services_import_access_guard(self):
        for rel in ("services/compressor.py", "services/artifact_store.py",
                    "services/scanner.py"):
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("access_guard", src,
                          f"{rel} lost its access_guard wiring")


class TestResolveBan(unittest.TestCase):
    def _functions_using_resolve(self, src: str):
        """Map each .resolve() occurrence to its enclosing def name."""
        hits = []
        current = "<module>"
        for line in src.splitlines():
            m = re.match(r"\s*def\s+(\w+)", line)
            if m:
                current = m.group(1)
            if ".resolve()" in line:
                hits.append(current)
        return hits

    def test_no_direct_resolve_outside_canonicalizers(self):
        offenders = {}
        for rel, allowed in _RESOLVE_BAN.items():
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            bad = [fn for fn in self._functions_using_resolve(src)
                   if fn not in allowed and fn != "<module>"]
            if bad:
                offenders[rel] = sorted(set(bad))
        self.assertEqual(
            offenders, {},
            f"direct Path.resolve() in enforcement-adjacent code: "
            f"{offenders}. Route through canonical_key / "
            f"access_guard.canonicalize instead.")


class TestDenialStormRegression(unittest.TestCase):
    def test_30_foreign_entries_do_not_evict_evidence(self):
        import cli.hook_pretool_enforce as enforce
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / ".c3").mkdir()
            log = base / ".c3" / "activity_log.jsonl"
            lines = [json.dumps({"type": "tool_call", "tool": "c3_search"})]
            lines += [json.dumps({"type": "access_denied",
                                  "rule": "secrets/**", "n": i})
                      for i in range(30)]
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            state = {"session_id": "", "last_c3_call": None,
                     "unlocked_files": {}}
            allowed, via = enforce._check_c3_used(
                base, state, "Read", {"file_path": "x.py"})
            self.assertTrue(
                allowed,
                "30 foreign activity entries evicted the c3_search evidence "
                "— the tail scan must count tool_call entries only")
            self.assertEqual(via, "activity")


if __name__ == "__main__":
    unittest.main()
