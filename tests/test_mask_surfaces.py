"""Mask Guard surface wiring + structural drift guards.

docs/mask-guard.md §2 turns on one invariant: EVERY surface that turns a
project file's bytes into model-visible text must serve the same materialized
view. If one surface skips it, an agent recovers the original by differencing
surfaces — so these tests are load-bearing, not nice-to-have.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import access_guard as ag  # noqa: E402
from services import mask_activation  # noqa: E402
from services.compressor import CodeCompressor  # noqa: E402

SECRET = "AKIAIOSFODNN7EXAMPLE"


class _Svc:
    """Minimal runtime stand-in for the tool handlers under test."""

    def __init__(self, project_path):
        self.project_path = str(project_path)
        from services.file_memory import FileMemoryStore
        self.file_memory = FileMemoryStore(str(project_path))


class MaskSurfaceCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "proj"
        (self.project / ".c3").mkdir(parents=True)
        self.home = self.tmp / "home"
        (self.home / ".c3").mkdir(parents=True)
        self._home_patch = mock.patch.object(Path, "home",
                                             staticmethod(lambda: self.home))
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.rel = "conf/app.py"
        target = self.project / self.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'AWS_ACCESS_KEY = "{SECRET}"\n\n'
                          'def handler():\n    return 1\n', encoding="utf-8")
        (self.project / ".c3" / "config.json").write_text(json.dumps(
            {"access": {"mask": [{"glob": "conf/**", "preset":
                                  "redact_secrets", "params": {}}]}}),
            encoding="utf-8")
        self.svc = _Svc(self.project)

    def full(self):
        return str(self.project / self.rel)


class TestContentSurfacesServeTheView(MaskSurfaceCase):

    def test_compressor_serves_masked_content_with_banner(self):
        comp = CodeCompressor(cache_dir=str(self.project / ".c3" / "cache"),
                              project_root=str(self.project))
        result = comp.compress_file(self.full(), mode="structure")
        self.assertTrue(result.get("masked"))
        self.assertNotIn(SECRET, result["compressed"])
        self.assertIn(ag.TAG_MASKED, result["compressed"])

    def test_compressor_cache_hit_still_carries_the_banner(self):
        """A cached answer must not silently drop the disclosure (§5)."""
        comp = CodeCompressor(cache_dir=str(self.project / ".c3" / "cache"),
                              project_root=str(self.project))
        comp.compress_file(self.full(), mode="structure")
        second = comp.compress_file(self.full(), mode="structure")
        self.assertIn(ag.TAG_MASKED, second["compressed"])
        self.assertNotIn(SECRET, second["compressed"])

    def test_compress_cache_key_is_not_shared_with_the_unmasked_file(self):
        comp = CodeCompressor(cache_dir=str(self.project / ".c3" / "cache"),
                              project_root=str(self.project))
        masked = comp.compress_file(self.full(), mode="structure")
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"access": {}}), encoding="utf-8")
        plain = comp.compress_file(self.full(), mode="structure")
        self.assertNotIn(SECRET, masked["compressed"])
        self.assertIn(SECRET, plain["compressed"],
                      "removing the rule must stop serving the view")

    def test_c3_read_serves_the_view(self):
        from cli.tools.read import handle_read
        out = handle_read(self.rel, svc=self.svc, finalize=None)
        self.assertNotIn(SECRET, out)
        self.assertIn(ag.TAG_MASKED, out)

    def test_c3_read_refuses_to_slice_a_masked_view(self):
        """Line numbers in a view do not map to the original — say so."""
        from cli.tools.read import handle_read
        out = handle_read(self.rel, lines=[1, 2], svc=self.svc, finalize=None)
        self.assertIn("c3-mask:note", out)
        self.assertNotIn(SECRET, out)

    def test_indexer_indexes_the_view_not_the_source(self):
        from services.indexer import _masked_content
        text = _masked_content(self.project / self.rel, self.project)
        self.assertIsNotNone(text)
        self.assertNotIn(SECRET, text)

    def test_indexer_skips_unrenderable_masked_files(self):
        from services.indexer import _masked_content
        binary = self.project / "conf" / "blob.py"
        binary.write_bytes(b"\xff\xfe\x00\x01")
        self.assertIsNone(_masked_content(binary, self.project))

    def test_exact_search_scans_the_view(self):
        """A regex search that matched raw bytes would bypass the mask."""
        from services.indexer import _masked_content
        text = _masked_content(self.project / self.rel, self.project)
        self.assertFalse(re.search(SECRET, text))

    def test_search_keeps_masked_paths_discoverable(self):
        from cli.tools.search import _read_denied
        self.assertFalse(_read_denied(self.full(), self.svc),
                         "masking exposes a file in transformed form; it must "
                         "stay discoverable, unlike deny")

    def test_search_footer_announces_masking(self):
        from cli.tools.search import _with_access_footer
        out = _with_access_footer("some result", self.svc)
        self.assertIn(ag.TAG_MASK_LIMITED, out)


class TestSurfacesThatMustRefuse(MaskSurfaceCase):
    """Cod's rule: block, do not post-sanitize. Post-filtering stdout can
    strip a secret but cannot reconstruct a crop, so 'best effort' here would
    read as a guarantee it cannot make."""

    def test_edit_is_denied(self):
        v = ag.verdict(self.full(), "write", str(self.project))
        self.assertEqual(v.kind, "denied")
        self.assertIn("read-only",
                      ag.refusal(v.denial, self.full(), "write"))

    def test_non_mask_aware_surfaces_fail_closed(self):
        """filter / impact / validate / shell / hooks all go through check()."""
        denial = ag.check(self.full(), "read", str(self.project))
        self.assertIsNotNone(denial)
        msg = ag.refusal(denial, self.full(), "read")
        self.assertIn(ag.TAG_MASK_UNSUPPORTED, msg)
        self.assertNotIn(SECRET, msg)

    def test_hook_layer_denies_native_reads_of_masked_paths(self):
        from cli import hook_access_guard  # noqa: F401  (import must not fail)
        denial = ag.check(self.full(), "read", str(self.project))
        self.assertIsNotNone(denial)
        self.assertIn(ag.TAG_MASK_UNSUPPORTED,
                      ag.refusal(denial, self.full(), "read",
                                 surface="hook", tool="Read"))


class TestActivationSurfacing(MaskSurfaceCase):

    def test_status_line_warns_until_activated(self):
        line = mask_activation.summary_line(self.project)
        self.assertIn("NOT activated", line)
        mask_activation.activate(self.project)
        self.assertIn("active", mask_activation.summary_line(self.project))

    def test_coverage_matrix_states_the_residual_honestly(self):
        self.assertIn("real bytes stay on disk",
                      ag.COVERAGE_MATRIX.lower())
        self.assertIn("not containment", ag.COVERAGE_MATRIX.lower())


# ── Structural drift guards ─────────────────────────────────────────────────

_MASK_AWARE = re.compile(
    r"access_guard\.verdict\(|ag\.verdict\(|render_for_path\(|"
    r"_masked_content\(|mask_mirror\.")

# Modules that turn project-file bytes into model-visible text. Each MUST be
# mask-aware; adding to the exempt set is a reviewed decision with a reason.
_CONTENT_SURFACES = {
    "cli/tools/read.py",
    "cli/tools/compress.py",
    "cli/tools/search.py",
    "services/compressor.py",
    "services/indexer.py",
}

# Surfaces that legitimately only need the fail-closed check() — they refuse
# on masked paths rather than rendering (docs/mask-guard.md §6).
_REFUSE_ONLY = {
    "cli/tools/filter.py": "quotes file content; refuses on masked paths",
    "cli/tools/impact.py": "structural analysis needs real line numbers",
    "cli/tools/validate.py": "type checkers quote source lines verbatim",
    "cli/tools/edit.py": "masked implies read-only; write always denied",
    "cli/tools/shell.py": "stdout cannot be reconstructed into a crop",
    "cli/tools/delegate.py": "subprocess backends open the real file",
}


class TestMaskWiringDoesNotDrift(unittest.TestCase):

    def test_content_surfaces_are_mask_aware(self):
        offenders = []
        for rel in sorted(_CONTENT_SURFACES):
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if not _MASK_AWARE.search(src):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "these surfaces emit file content but never consult Mask Guard: "
            f"{offenders}. Route through access_guard.verdict() + "
            "mask_mirror.render_for_path(), or move them to _REFUSE_ONLY.")

    def test_refuse_only_surfaces_still_consult_the_guard(self):
        offenders = []
        for rel in sorted(_REFUSE_ONLY):
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if "access_guard" not in src:
                offenders.append(rel)
        self.assertEqual(offenders, [],
                         f"refuse-only surfaces must call the guard: {offenders}")

    def test_every_preset_has_a_view_class_label(self):
        """A view served without a coarse class would hide HOW it is wrong."""
        missing = sorted(set(ag.MASK_PRESETS) - set(ag._VIEW_CLASS))
        self.assertEqual(missing, [],
                         f"presets with no §5 view class: {missing}")

    def test_preset_names_match_the_engine_registry(self):
        from services import mask_presets
        self.assertEqual(sorted(ag.MASK_PRESETS),
                         sorted(mask_presets._ENGINES),
                         "the evaluator's preset list and the engine registry "
                         "must not drift — a name in one but not the other is "
                         "either a dead rule or an unvalidated transform")

    def test_placeholder_cannot_be_valid_source(self):
        from services import mask_presets
        placeholder = mask_presets.PLACEHOLDER.format(kind="k")
        for ch in ("«", "»"):
            self.assertIn(ch, placeholder)
        self.assertNotRegex(placeholder, r"^[\w\s\"'`.,;:()\[\]{}=-]+$")


if __name__ == "__main__":
    unittest.main()
