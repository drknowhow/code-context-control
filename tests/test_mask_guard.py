"""Mask Guard — evaluator, presets, mirror, activation, provenance.

Mirrors docs/mask-guard.md. The load-bearing cases are the fail-closed ones:
a masked path must never fall back to raw bytes, on any surface, for any
reason — including "the transform errored" and "the config is broken".
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import access_guard as ag
from services import mask_activation, mask_mirror, mask_presets
from services.memory import MemoryStore, normalize_source_paths


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class MaskTestCase(unittest.TestCase):
    """Project sandbox with a redirected HOME so the mirror is disposable."""

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

    def set_config(self, access: dict, scope: str = "project"):
        base = self.project if scope == "project" else self.home
        (base / ".c3" / "config.json").write_text(
            json.dumps({"access": access}), encoding="utf-8")

    def mask(self, glob, preset, params=None):
        self.set_config({"mask": [{"glob": glob, "preset": preset,
                                   "params": params or {}}]})


# ── P1: evaluator ───────────────────────────────────────────────────────────

class TestMaskVerdict(MaskTestCase):

    def test_masked_read_and_write_deny(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,x\n2,y\n")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        target = str(self.project / "data" / "a.csv")

        v = ag.verdict(target, "read", str(self.project))
        self.assertEqual(v.kind, "masked")
        self.assertEqual(v.mask_rule.preset, "sample_rows")

        for op in ("write", "create", "delete"):
            self.assertEqual(
                ag.verdict(target, op, str(self.project)).kind, "denied",
                f"masked path must deny {op} — masked content is read-only")

    def test_check_fails_closed_on_masked_read(self):
        """An un-migrated surface must refuse, never serve raw bytes."""
        _write(self.project / "data" / "a.csv", "id,v\n1,x\n")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        denial = ag.check(str(self.project / "data" / "a.csv"), "read",
                          str(self.project))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "mask")

    def test_deny_outranks_mask(self):
        _write(self.project / "secret" / "a.csv", "id\n1\n")
        self.set_config({
            "deny": ["secret/**"],
            "mask": [{"glob": "**/*.csv", "preset": "sample_rows",
                      "params": {"count": 1, "strategy": "first"}}],
        })
        v = ag.verdict(str(self.project / "secret" / "a.csv"), "read",
                       str(self.project))
        self.assertEqual(v.kind, "denied")
        self.assertEqual(v.denial.kind, "deny")

    def test_mask_outranks_read_only(self):
        _write(self.project / "a.csv", "id\n1\n")
        self.set_config({
            "read_only": ["*.csv"],
            "mask": [{"glob": "*.csv", "preset": "sample_rows",
                      "params": {"count": 1, "strategy": "first"}}],
        })
        self.assertEqual(
            ag.verdict(str(self.project / "a.csv"), "read",
                       str(self.project)).kind, "masked")

    def test_conflicting_overlap_fails_closed(self):
        """Two rules, same path, different presets => refuse, not a coin flip."""
        _write(self.project / "a.csv", "id\n1\n")
        self.set_config({"mask": [
            {"glob": "*.csv", "preset": "sample_rows",
             "params": {"count": 1, "strategy": "first"}},
            {"glob": "a.*", "preset": "redact_secrets", "params": {}},
        ]})
        v = ag.verdict(str(self.project / "a.csv"), "read", str(self.project))
        self.assertEqual(v.kind, "denied")
        self.assertIn("overlapping", v.denial.reason)

    def test_identical_overlap_is_fine(self):
        _write(self.project / "a.csv", "id\n1\n")
        self.set_config({"mask": [
            {"glob": "*.csv", "preset": "redact_secrets", "params": {}},
            {"glob": "a.*", "preset": "redact_secrets", "params": {}},
        ]})
        self.assertEqual(
            ag.verdict(str(self.project / "a.csv"), "read",
                       str(self.project)).kind, "masked")

    def test_invalid_preset_makes_scope_corrupt(self):
        _write(self.project / "a.csv", "id\n1\n")
        self.set_config({"mask": [{"glob": "*.csv", "preset": "summarize",
                                   "params": {}}]})
        v = ag.verdict(str(self.project / "a.csv"), "read", str(self.project))
        self.assertEqual(v.kind, "denied")
        self.assertEqual(v.denial.rule, "<corrupt-config>")

    def test_missing_required_param_is_corrupt(self):
        self.set_config({"mask": [{"glob": "*.csv", "preset": "sample_rows",
                                   "params": {"count": 5}}]})
        _write(self.project / "a.csv", "id\n1\n")
        self.assertEqual(
            ag.verdict(str(self.project / "a.csv"), "read",
                       str(self.project)).kind, "denied")

    def test_validate_mask_entry_messages(self):
        self.assertEqual(validate := ag.validate_mask_entry(
            {"glob": "*.csv", "preset": "redact_secrets", "params": {}}), "")
        self.assertIn("unknown mask preset",
                      ag.validate_mask_entry({"glob": "a", "preset": "nope"}))
        self.assertIn("requires param", ag.validate_mask_entry(
            {"glob": "a", "preset": "sample_rows", "params": {}}))
        self.assertIn("must be >= 1", ag.validate_mask_entry(
            {"glob": "a", "preset": "sample_rows",
             "params": {"count": 0, "strategy": "first"}}))
        self.assertIn("strategy", ag.validate_mask_entry(
            {"glob": "a", "preset": "sample_rows",
             "params": {"count": 2, "strategy": "random"}}))

    def test_refusals_carry_stable_tags(self):
        _write(self.project / "a.csv", "id\n1\n")
        self.mask("*.csv", "redact_secrets")
        target = str(self.project / "a.csv")
        write_msg = ag.refusal(
            ag.verdict(target, "write", str(self.project)).denial,
            target, "write")
        self.assertIn(ag.TAG_MASKED, write_msg)
        self.assertIn("read-only", write_msg)
        read_msg = ag.refusal(ag.check(target, "read", str(self.project)),
                              target, "read")
        self.assertIn(ag.TAG_MASK_UNSUPPORTED, read_msg)

    def test_mask_rules_activate_search_footers(self):
        self.mask("*.csv", "redact_secrets")
        self.assertTrue(ag.has_mask_rules(str(self.project)))
        self.assertTrue(ag.has_active_rules(str(self.project)))
        self.assertIn(ag.TAG_MASK_LIMITED, ag.mask_footer(str(self.project)))

    def test_rule_write_surface_replaces_rather_than_conflicts(self):
        ag.set_mask_rule("*.csv", "redact_secrets", {}, "project",
                         str(self.project))
        second = ag.set_mask_rule("*.csv", "sample_rows",
                                  {"count": 3, "strategy": "first"},
                                  "project", str(self.project))
        self.assertTrue(second["replaced"])
        rules, _ = ag.load_mask_rules(str(self.project))
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].preset, "sample_rows")
        self.assertTrue(ag.remove_mask_rule("*.csv", "project",
                                            str(self.project))["removed"])

    def test_set_rule_rejects_mask_kind(self):
        with self.assertRaises(ValueError):
            ag.set_rule("*.csv", "mask", "project", str(self.project))


# ── P3: presets + Protected Mode ────────────────────────────────────────────

class TestPresets(unittest.TestCase):

    def test_sample_rows_keeps_header_and_reports_withheld(self):
        out = mask_presets.render("id,v\n1,a\n2,b\n3,c\n", "sample_rows",
                                  {"count": 2, "strategy": "first"})
        self.assertTrue(out.text.startswith("id,v"))
        self.assertIn("1,a", out.text)
        self.assertNotIn("3,c", out.text)
        self.assertIn("withheld by mask policy", out.text)
        self.assertEqual(out.stats["rows_total"], 3)

    def test_redact_secrets_kills_known_shapes(self):
        raw = ("AWS=AKIAIOSFODNN7EXAMPLE\n"
               "API_TOKEN = 'sk-proj-abcdefghijklmnopqrstuvwxyz012345'\n"
               "url = postgres://user:hunter2@db.internal:5432/app\n"
               "keep = 42\n")
        out = mask_presets.render(raw, "redact_secrets", {})
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out.text)
        self.assertNotIn("hunter2", out.text)
        self.assertIn("keep = 42", out.text)
        self.assertIn("«c3:redacted:", out.text)

    def test_placeholder_is_syntactically_inert(self):
        """A placeholder must be impossible to commit silently (§5)."""
        self.assertNotIn(mask_presets.PLACEHOLDER.format(kind="x")[0],
                         "abcdefghijklmnopqrstuvwxyz0123456789_\"'`")

    def test_redact_columns_is_stable_and_referentially_consistent(self):
        raw = "id,name\n1,Ann\n2,Bob\n3,Ann\n"
        first = mask_presets.render(raw, "redact_columns",
                                    {"columns": ["name"]}, salt="s").text
        second = mask_presets.render(raw, "redact_columns",
                                     {"columns": ["name"]}, salt="s").text
        self.assertEqual(first, second, "renders must be deterministic")
        rows = [ln.split(",")[1] for ln in first.splitlines()[1:] if ln]
        self.assertEqual(rows[0], rows[2], "same value -> same pseudonym")
        self.assertNotEqual(rows[0], rows[1])
        self.assertNotIn("Ann", first)

    def test_salt_changes_pseudonyms(self):
        raw = "id,name\n1,Ann\n"
        a = mask_presets.render(raw, "redact_columns", {"columns": ["name"]},
                                salt="one").text
        b = mask_presets.render(raw, "redact_columns", {"columns": ["name"]},
                                salt="two").text
        self.assertNotEqual(a, b)

    def test_redact_columns_unknown_column_fails_closed(self):
        with self.assertRaises(mask_presets.MaskRenderError):
            mask_presets.render("id,name\n1,Ann\n", "redact_columns",
                                {"columns": ["emial"]})

    def test_binary_input_refuses(self):
        with self.assertRaises(mask_presets.MaskRenderError):
            mask_presets.render("abc\x00def", "redact_secrets", {})

    def test_protected_mode_blocks_residual_secret(self):
        """If a preset leaves a secret behind, refuse — do not serve."""
        # Patch the ENGINES entry, not the module attribute: render() looks
        # the engine up in the dict, which binds the original function early.
        with mock.patch.dict(
                mask_presets._ENGINES,
                {"redact_secrets": lambda t, p, s: mask_presets.RenderResult(
                    t, "redact_secrets", {})}):
            with self.assertRaises(mask_presets.MaskRenderError) as ctx:
                mask_presets.render("k=AKIAIOSFODNN7EXAMPLE",
                                    "redact_secrets", {})
        self.assertIn("Protected Mode", str(ctx.exception))

    def test_residual_scan_accepts_a_clean_render(self):
        out = mask_presets.render("token=AKIAIOSFODNN7EXAMPLE",
                                  "redact_secrets", {})
        self.assertEqual(mask_presets.residual_secrets(out.text), [])


# ── P2: mirror ──────────────────────────────────────────────────────────────

class TestMirror(MaskTestCase):

    def _rule(self, preset="sample_rows", params=None):
        self.mask("data/*.csv", preset,
                  params or {"count": 1, "strategy": "first"})
        rules, _ = ag.load_mask_rules(str(self.project))
        return rules[0]

    def test_view_hash_covers_params_and_transformer_version(self):
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        r1, _ = ag.load_mask_rules(str(self.project))
        self.mask("data/*.csv", "sample_rows", {"count": 2, "strategy": "first"})
        r2, _ = ag.load_mask_rules(str(self.project))
        raw = b"id,v\n1,a\n2,b\n"
        self.assertNotEqual(mask_mirror.view_hash(raw, r1[0]),
                            mask_mirror.view_hash(raw, r2[0]))
        with mock.patch.object(mask_mirror, "TRANSFORMER_VERSION", 99):
            bumped = mask_mirror.view_hash(raw, r1[0])
        self.assertNotEqual(mask_mirror.view_hash(raw, r1[0]), bumped)

    def test_view_is_materialized_and_reused(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        rule = self._rule()
        view = mask_mirror.build_view("data/a.csv", rule, self.project)
        artifact = mask_mirror.mirror_root(self.project) / "views" / view.view_hash
        self.assertTrue(artifact.is_file())
        again = mask_mirror.get_view("data/a.csv", rule, self.project)
        self.assertEqual(again.view_hash, view.view_hash)
        self.assertEqual(again.text, view.text)

    def test_source_change_rebuilds_rather_than_serving_stale(self):
        src = _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        rule = self._rule()
        first = mask_mirror.get_view("data/a.csv", rule, self.project)
        _write(src, "id,v\n9,zzz\n8,yyy\n")
        second = mask_mirror.get_view("data/a.csv", rule, self.project)
        self.assertNotEqual(first.view_hash, second.view_hash)
        self.assertIn("9,zzz", second.text)

    def test_unrenderable_source_refuses(self):
        _write(self.project / "data" / "a.csv", "")
        rule = self._rule("redact_columns", {"columns": ["name"]})
        with self.assertRaises(mask_mirror.MaskUnavailable):
            mask_mirror.build_view("data/a.csv", rule, self.project)

    def test_non_utf8_source_refuses(self):
        path = self.project / "data" / "a.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00binary")
        rule = self._rule()
        with self.assertRaises(mask_mirror.MaskUnavailable) as ctx:
            mask_mirror.build_view("data/a.csv", rule, self.project)
        self.assertEqual(ctx.exception.reason, "binary")

    def test_render_for_path_returns_none_when_unmasked(self):
        _write(self.project / "notes.txt", "hello")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        self.assertIsNone(mask_mirror.render_for_path(
            self.project / "notes.txt", self.project))

    def test_render_for_path_raises_on_denied(self):
        _write(self.project / "secret.txt", "x")
        self.set_config({"deny": ["secret.txt"]})
        with self.assertRaises(ag.AccessDenied):
            mask_mirror.render_for_path(self.project / "secret.txt",
                                        self.project)

    def test_header_is_attached_and_names_the_view_class(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        rule = self._rule()
        text = mask_mirror.get_view("data/a.csv", rule,
                                    self.project).with_header("data/a.csv")
        self.assertTrue(text.startswith(ag.TAG_MASKED))
        self.assertIn("view=sampled", text)
        self.assertIn("read-only", text)

    def test_gc_removes_unreferenced_views(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        rule = self._rule()
        mask_mirror.build_view("data/a.csv", rule, self.project)
        mask_mirror.clear(self.project)
        views = mask_mirror.mirror_root(self.project) / "views"
        self.assertEqual([p for p in views.iterdir() if p.is_file()], [])


# ── P0: fact provenance ─────────────────────────────────────────────────────

class TestFactProvenance(MaskTestCase):

    def _store(self):
        return MemoryStore(str(self.project))

    def test_normalize_preserves_the_unknown_state(self):
        self.assertIsNone(normalize_source_paths(None))
        self.assertEqual(normalize_source_paths([]), [])
        self.assertEqual(normalize_source_paths("a\\b.py"), ["a/b.py"])

    def test_three_states_round_trip(self):
        store = self._store()
        store.remember("legacy shaped fact with enough length here")
        store.remember("empty provenance fact, deliberately long enough",
                       source_paths=[])
        store.remember("derived provenance fact, long enough text here",
                       source_paths=["src/a.py"])
        reloaded = MemoryStore(str(self.project))
        states = {f["fact"].split()[0]: f["source_paths"]
                  for f in reloaded.facts}
        self.assertIsNone(states["legacy"], "absent key must stay unknown")
        self.assertEqual(states["empty"], [])
        self.assertEqual(states["derived"], ["src/a.py"])
        self.assertEqual(reloaded.unknown_provenance_count(), 1)

    def test_purge_by_source_targets_only_matching_facts(self):
        store = self._store()
        store.remember("derived from alpha file content here", "auto:structure",
                       source_paths=["src/alpha.py"])
        store.remember("derived from beta file content here", "auto:structure",
                       source_paths=["src/beta.py"])
        report = store.purge_by_source(["src/alpha.py"])
        self.assertEqual(report["purged"], 1)
        self.assertEqual(len(store.facts), 1)
        self.assertIn("beta", store.facts[0]["fact"])

    def test_purge_include_unknown_sweeps_legacy_facts(self):
        store = self._store()
        store.remember("a legacy fact with no provenance recorded at all")
        store.remember("a clean fact with empty provenance recorded",
                       source_paths=[])
        self.assertEqual(store.purge_by_source([], include_unknown=False)
                         ["purged"], 0)
        report = store.purge_by_source([], include_unknown=True)
        self.assertEqual(report["unknown_purged"], 1)
        self.assertEqual(len(store.facts), 1)

    def test_auto_memory_records_file_provenance(self):
        from services import auto_memory
        learnings = auto_memory._extract_edit(
            {"file_path": "src/a.py",
             "summary": "rewrote the retry loop to be bounded"}, "", "")
        self.assertEqual(len(learnings), 1)
        self.assertEqual(learnings[0][2], ["src/a.py"])

    def test_agent_extractor_declares_unknown_provenance(self):
        from services import auto_memory
        learnings = auto_memory._extract_agent(
            {"workflow": "audit"}, "", "x" * 100 + "\nfinding line here")
        self.assertTrue(all(len(item) == 2 for item in learnings),
                        "workflow facts must stay unknown-provenance")


# ── P5: activation ──────────────────────────────────────────────────────────

class TestActivation(MaskTestCase):

    def test_status_reports_stale_until_activated(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        before = mask_activation.status(self.project)
        self.assertTrue(before["stale"])
        self.assertIn("NOT activated", mask_activation.summary_line(self.project))

        report = mask_activation.activate(self.project)
        self.assertTrue(report["ok"])
        self.assertEqual(report["views_built"], 1)
        after = mask_activation.status(self.project)
        self.assertFalse(after["stale"])
        self.assertEqual(after["status"], "active")

    def test_rule_change_makes_activation_stale_again(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        mask_activation.activate(self.project)
        self.mask("data/*.csv", "sample_rows", {"count": 2, "strategy": "first"})
        self.assertTrue(mask_activation.status(self.project)["stale"])

    def test_activation_purges_facts_derived_from_masked_files(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n2,b\n")
        store = MemoryStore(str(self.project))
        store.remember("structure of the customer csv, derived pre-mask",
                       "auto:structure", source_paths=["data/a.csv"])
        store.remember("unrelated fact about the build system here",
                       "auto:structure", source_paths=["build.py"])
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})

        report = mask_activation.activate(self.project, memory_store=store)
        self.assertGreaterEqual(report["facts"]["purged"], 1)
        remaining = [f["fact"] for f in store.facts]
        self.assertTrue(all("customer csv" not in f for f in remaining))

    def test_first_activation_sweeps_unknown_provenance(self):
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n")
        store = MemoryStore(str(self.project))
        store.remember("a pre-v2.63.0 fact with unknown provenance here")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        report = mask_activation.activate(self.project, memory_store=store)
        self.assertEqual(report["facts"]["unknown_purged"], 1)
        self.assertEqual(len(store.facts), 0)

    def test_activation_reports_incomplete_when_a_view_fails(self):
        _write(self.project / "data" / "a.csv", "")
        self.mask("data/*.csv", "redact_columns", {"columns": ["email"]})
        report = mask_activation.activate(self.project)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["failures"]), 1)
        self.assertIn("INCOMPLETE", mask_activation.summary_line(self.project))

    def test_activation_drops_file_memory_for_masked_paths(self):
        from services.file_memory import FileMemoryStore
        _write(self.project / "data" / "a.csv", "id,v\n1,a\n")
        store = FileMemoryStore(str(self.project))
        store.update("data/a.csv")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        mask_activation.activate(self.project)
        self.assertIsNone(FileMemoryStore(str(self.project)).get("data/a.csv"))

    def test_masked_files_enumeration(self):
        _write(self.project / "data" / "a.csv", "id\n1\n")
        _write(self.project / "data" / "b.csv", "id\n1\n")
        _write(self.project / "notes.md", "hi")
        self.mask("data/*.csv", "sample_rows", {"count": 1, "strategy": "first"})
        self.assertEqual(mask_activation.masked_files(self.project),
                         ["data/a.csv", "data/b.csv"])


if __name__ == "__main__":
    unittest.main()
