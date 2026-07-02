"""Tests for services/artifact_defs.py + services/artifact_store.py —
agent-artifact classification, scanning, version history, diff, restore."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import services.artifact_store as artifact_store_mod
from services.artifact_defs import classify_path, discover_units, note_pending_write
from services.artifact_store import ArtifactStore


class TestClassifyPath(unittest.TestCase):
    CASES = {
        # instructions — one per IDE
        "CLAUDE.md": ("instructions", "instructions:CLAUDE.md", "claude-code"),
        "AGENTS.md": ("instructions", "instructions:AGENTS.md", "codex"),
        "GEMINI.md": ("instructions", "instructions:GEMINI.md", "gemini"),
        ".cursorrules": ("instructions", "instructions:.cursorrules", "cursor"),
        ".github/copilot-instructions.md":
            ("instructions", "instructions:.github/copilot-instructions.md", "vscode"),
        # settings
        ".claude/settings.local.json":
            ("settings", "settings:.claude/settings.local.json", "claude-code"),
        ".claude/settings.json":
            ("settings", "settings:.claude/settings.json", "claude-code"),
        # mcp configs
        ".mcp.json": ("mcp", "mcp:.mcp.json", "claude-code"),
        ".vscode/mcp.json": ("mcp", "mcp:.vscode/mcp.json", "vscode"),
        ".cursor/mcp.json": ("mcp", "mcp:.cursor/mcp.json", "cursor"),
        ".codex/config.toml": ("mcp", "mcp:.codex/config.toml", "codex"),
        # claude extensions
        ".claude/skills/browcontrol/SKILL.md":
            ("skill", "skill:browcontrol", "claude-code"),
        ".claude/skills/browcontrol/assets/helper.py":
            ("skill", "skill:browcontrol", "claude-code"),
        ".claude/agents/code-reviewer.md":
            ("agent", "agent:code-reviewer", "claude-code"),
        ".claude/agents/deep/agent.md": ("agent", "agent:deep", "claude-code"),
        ".claude/commands/deploy.md": ("command", "command:deploy", "claude-code"),
        ".claude/commands/ops/restart.md":
            ("command", "command:ops/restart", "claude-code"),
        ".claude/plugins/figma/manifest.json":
            ("plugin", "plugin:figma", "claude-code"),
    }

    def test_classification_table(self):
        for path, (cls, aid, provider) in self.CASES.items():
            ref = classify_path(path)
            self.assertIsNotNone(ref, f"{path} should classify")
            self.assertEqual((ref.cls, ref.id, ref.provider), (cls, aid, provider), path)

    def test_gemini_settings_doubles_as_mcp(self):
        ref = classify_path(".gemini/settings.json")
        self.assertEqual(ref.cls, "mcp")
        self.assertIn("settings", ref.roles)

    def test_non_artifacts(self):
        for path in ("src/foo.py", "README.md", ".claude/skills/stray.txt",
                     ".claude/plugins/loose.md", ".claude/agents/notes.txt",
                     ".claude/commands/raw.py", "", ".c3/config.json"):
            self.assertIsNone(classify_path(path), path)

    def test_windows_paths_normalize(self):
        ref = classify_path(".claude\\skills\\browcontrol\\SKILL.md")
        self.assertEqual(ref.id, "skill:browcontrol")
        self.assertEqual(classify_path("./CLAUDE.md").id, "instructions:CLAUDE.md")


class ArtifactStoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self.store = ArtifactStore(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, content, encoding="utf-8"):
        fp = self.root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            fp.write_bytes(content)
        else:
            fp.write_text(content, encoding=encoding)
        return fp


class TestDiscoverAndScan(ArtifactStoreBase):
    def test_discover_units(self):
        self.write("CLAUDE.md", "# hi")
        self.write(".mcp.json", "{}")
        self.write(".claude/skills/demo/SKILL.md", "skill")
        self.write(".claude/skills/demo/ref.md", "ref")
        self.write(".claude/commands/deploy.md", "cmd")
        units = {u.id: u for u in discover_units(self.root)}
        self.assertIn("instructions:CLAUDE.md", units)
        self.assertIn("mcp:.mcp.json", units)
        self.assertIn("command:deploy", units)
        self.assertEqual(sorted(units["skill:demo"].members),
                         [".claude/skills/demo/SKILL.md", ".claude/skills/demo/ref.md"])

    def test_baseline_then_idempotent_rescan(self):
        self.write("CLAUDE.md", "# v1")
        res = self.store.scan()
        self.assertEqual(res["added"], ["instructions:CLAUDE.md"])
        again = self.store.scan()
        self.assertEqual(again["events"], [])
        self.assertEqual(again["unchanged"], 1)

    def test_modify_bumps_version_and_revert_dedups_blob(self):
        self.write("CLAUDE.md", "# v1")
        self.store.scan()
        self.write("CLAUDE.md", "# v2")
        res = self.store.scan()
        self.assertEqual(res["modified"], ["instructions:CLAUDE.md"])
        self.write("CLAUDE.md", "# v1")  # revert to original bytes
        self.store.scan()
        arts = self.store.list_artifacts()
        self.assertEqual(arts[0]["version"], 3)
        blobs = list(self.store.blob_dir.glob("*.gz"))
        self.assertEqual(len(blobs), 2)  # v1 and v3 share one blob

    def test_directory_unit_member_add_remove(self):
        self.write(".claude/skills/demo/SKILL.md", "a")
        self.store.scan()
        self.write(".claude/skills/demo/extra.md", "b")
        res = self.store.scan()
        changed = res["events"][0]["changed"]
        self.assertEqual([c["change"] for c in changed], ["added"])
        (self.root / ".claude/skills/demo/extra.md").unlink()
        res = self.store.scan()
        changed = res["events"][0]["changed"]
        self.assertEqual([c["change"] for c in changed], ["removed"])

    def test_delete_and_resurrect(self):
        self.write(".claude/commands/deploy.md", "run it")
        self.store.scan()
        (self.root / ".claude/commands/deploy.md").unlink()
        res = self.store.scan()
        self.assertEqual(res["deleted"], ["command:deploy"])
        art = self.store.list_artifacts()[0]
        self.assertFalse(art["exists"])
        restored = self.store.restore("command:deploy", 1)
        self.assertTrue(restored["restored"])
        self.assertEqual((self.root / ".claude/commands/deploy.md").read_text(
            encoding="utf-8"), "run it")
        self.assertTrue(self.store.list_artifacts()[0]["exists"])

    def test_targeted_scan_only_touches_named_path(self):
        self.write("CLAUDE.md", "one")
        self.write("AGENTS.md", "two")
        res = self.store.scan(paths=["CLAUDE.md"])
        self.assertEqual(res["added"], ["instructions:CLAUDE.md"])
        self.assertEqual(self.store.list_artifacts()[0]["id"], "instructions:CLAUDE.md")
        self.assertEqual(len(self.store.list_artifacts()), 1)


class TestHistoryAndDiff(ArtifactStoreBase):
    def test_history_attribution_and_filter(self):
        self.write("CLAUDE.md", "# v1")
        self.write(".mcp.json", "{}")
        self.store.scan()
        self.write("CLAUDE.md", "# v2")
        self.store.note_write("CLAUDE.md", "c3_edit", session_id="s1",
                              summary="tweak heading")
        events = self.store.get_history(artifact="CLAUDE.md")
        self.assertEqual(events[0]["source"], "c3_edit")
        self.assertEqual(events[0]["summary"], "tweak heading")
        self.assertEqual(events[0]["session_id"], "s1")
        self.assertTrue(all(e["artifact_id"] == "instructions:CLAUDE.md"
                            for e in events))

    def test_diff_versions_and_live(self):
        self.write("CLAUDE.md", "alpha\nbeta\n")
        self.store.scan()
        self.write("CLAUDE.md", "alpha\ngamma\n")
        self.store.scan()
        d = self.store.diff("instructions:CLAUDE.md", 1, 2)
        self.assertIn("-beta", d["diff"])
        self.assertIn("+gamma", d["diff"])
        self.write("CLAUDE.md", "alpha\ndelta\n")  # un-scanned live change
        live = self.store.diff("instructions:CLAUDE.md", 2)
        self.assertIn("+delta", live["diff"])
        self.assertEqual(live["to"], "live")

    def test_get_version_content(self):
        self.write(".mcp.json", '{"a": 1}')
        self.store.scan()
        got = self.store.get_version(".mcp.json", 1)
        self.assertEqual(got["members"][0]["text"], '{"a": 1}')

    def test_resolve_by_prefix_and_path(self):
        self.write("GEMINI.md", "g")
        self.store.scan()
        self.assertIsNotNone(self.store.resolve("instructions:GEM"))
        self.assertIsNotNone(self.store.resolve("GEMINI.md"))
        self.assertIsNone(self.store.resolve("nope"))


class TestRestore(ArtifactStoreBase):
    def test_exact_bytes_round_trip_crlf_and_bom(self):
        raw = b"\xef\xbb\xbfline one\r\nline two\r\n"
        self.write("AGENTS.md", raw)
        self.store.scan()
        self.write("AGENTS.md", b"changed\n")
        self.store.scan()
        res = self.store.restore("instructions:AGENTS.md", 1)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), raw)
        self.assertEqual(res["new_version"], 3)
        events = self.store.get_history(artifact="AGENTS.md")
        self.assertEqual(events[0]["event"], "restored")
        self.assertEqual(events[0]["restored_from"], 1)

    def test_restore_removes_members_added_later(self):
        self.write(".claude/skills/demo/SKILL.md", "a")
        self.store.scan()
        self.write(".claude/skills/demo/later.md", "b")
        self.store.scan()
        res = self.store.restore("skill:demo", 1)
        self.assertIn(".claude/skills/demo/later.md", res["files_removed"])
        self.assertFalse((self.root / ".claude/skills/demo/later.md").exists())

    def test_restore_warnings(self):
        self.write(".claude/settings.local.json", '{"hooks": 1}')
        self.write("CLAUDE.md", "<!-- C3:BEGIN -->\nmanaged\n<!-- C3:END -->")
        self.store.scan()
        self.write(".claude/settings.local.json", '{"hooks": 2}')
        self.write("CLAUDE.md", "<!-- C3:BEGIN -->\nmanaged v2\n<!-- C3:END -->")
        self.store.scan()
        res = self.store.restore("settings:.claude/settings.local.json", 1)
        self.assertTrue(any("live agent session" in w for w in res["warnings"]))
        res = self.store.restore("instructions:CLAUDE.md", 1)
        self.assertTrue(any("C3-managed block" in w for w in res["warnings"]))

    def test_missing_version_errors(self):
        self.write("CLAUDE.md", "x")
        self.store.scan()
        self.assertIn("error", self.store.restore("instructions:CLAUDE.md", 9))
        self.assertIn("error", self.store.restore("ghost:none", 1))

    def test_oversized_member_not_restorable(self):
        with patch.object(artifact_store_mod, "MAX_MEMBER_BYTES", 10):
            self.write("CLAUDE.md", "this is more than ten bytes long")
            self.store.scan()
            self.write("CLAUDE.md", "short")
            self.store.scan()
            res = self.store.restore("instructions:CLAUDE.md", 1)
            self.assertTrue(any("not restorable" in w for w in res["warnings"]))
            self.assertEqual(res["files_written"], [])


class TestPendingAndStatus(ArtifactStoreBase):
    def test_pending_signal_attribution(self):
        self.write("CLAUDE.md", "v1")
        self.store.scan()
        self.write("CLAUDE.md", "v2")
        note_pending_write(self.root, "CLAUDE.md", "hook", session_id="sX",
                           tool="Edit")
        res = self.store.consume_pending()
        self.assertEqual(res["consumed"], 1)
        self.assertEqual(res["events"][0]["source"], "hook")
        self.assertFalse(self.store.pending_file.exists())

    def test_unchanged_pending_signal_drops(self):
        self.write("CLAUDE.md", "v1")
        self.store.scan()
        note_pending_write(self.root, "CLAUDE.md", "hook")
        res = self.store.consume_pending()
        self.assertEqual(res["consumed"], 1)
        self.assertEqual(res["events"], [])

    def test_status_shape(self):
        self.write("CLAUDE.md", "x")
        self.write(".mcp.json", "{}")
        self.store.scan()
        st = self.store.status()
        self.assertEqual(st["tracked"], 2)
        self.assertEqual(st["by_class"], {"instructions": 1, "mcp": 1})
        self.assertTrue(st["last_scan"])


class TestPruneAndRecovery(ArtifactStoreBase):
    def test_version_cap_and_orphan_gc(self):
        self.write("CLAUDE.md", "0")
        self.store.scan()
        for i in range(1, 25):
            self.write("CLAUDE.md", f"content {i}")
            self.store.scan()
        entry = self.store.resolve("instructions:CLAUDE.md")
        self.assertLessEqual(len(entry["versions"]), 20)
        pruned = self.store.prune(max_versions=3, blob_orphan_days=0)
        entry = self.store.resolve("instructions:CLAUDE.md")
        self.assertEqual(len(entry["versions"]), 3)
        self.assertGreater(pruned["blobs_deleted"], 0)
        # referenced blobs survive
        got = self.store.get_version("instructions:CLAUDE.md",
                                     entry["versions"][0]["v"])
        self.assertIsNotNone(got["members"][0]["text"])

    def test_fresh_orphan_blob_survives_gc(self):
        self.write("CLAUDE.md", "a")
        self.store.scan()
        orphan = self.store._write_blob(b"unreferenced")
        self.store.prune(max_versions=20, blob_orphan_days=7)
        self.assertTrue((self.store.blob_dir / f"{orphan}.gz").exists())

    def test_corrupt_manifest_recovery(self):
        self.write("CLAUDE.md", "x")
        self.store.scan()
        self.store.manifest_file.write_text("{not json", encoding="utf-8")
        res = self.store.scan()  # re-baselines instead of crashing
        self.assertEqual(res["added"], ["instructions:CLAUDE.md"])
        self.assertTrue(self.store.manifest_file.with_name(
            "manifest.json.corrupt-1").exists())


if __name__ == "__main__":
    unittest.main()
