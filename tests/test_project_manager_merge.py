"""Unit tests for ProjectManager.merge_projects and add_project idempotency.

Sandboxes ~/.c3/projects.json by monkey-patching the module-level constants
so the tests never touch the user's real registry.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import services.project_manager as pm_mod  # noqa: E402
from services.project_manager import ProjectManager  # noqa: E402


def _seed_project(root: Path, name: str, *, with_facts=True, with_ledger=True, with_conv=True) -> Path:
    """Create a fake C3-initialized project at <root>/<name> with sample data."""
    proj = root / name
    (proj / ".c3").mkdir(parents=True, exist_ok=True)
    # Minimal config.json so _read_project_config doesn't barf.
    (proj / ".c3" / "config.json").write_text(json.dumps({"ide": "claude-code"}), encoding="utf-8")

    if with_facts:
        facts_dir = proj / ".c3" / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)
        (facts_dir / "facts.json").write_text(json.dumps([
            {"id": f"{name}-f1", "fact": f"hello from {name}", "category": "test"},
            {"id": f"{name}-f2", "fact": f"second fact in {name}", "category": "test"},
        ]), encoding="utf-8")

    if with_ledger:
        (proj / ".c3" / "edit_ledger.jsonl").write_text(
            json.dumps({"id": f"edit_{name}_001", "file": "x.py", "summary": f"edit in {name}", "tags": []}) + "\n"
            + json.dumps({"id": f"edit_{name}_002", "file": "y.py", "summary": f"another edit in {name}", "tags": ["existing"]}) + "\n",
            encoding="utf-8",
        )

    if with_conv:
        conv = proj / ".c3" / "conversations"
        conv.mkdir(parents=True, exist_ok=True)
        sid_a = f"sess-{name}-aaa"
        sid_b = f"sess-{name}-bbb"
        (conv / "sessions.json").write_text(json.dumps([
            {"session_id": sid_a, "title": f"{name} sess A", "source": "claude"},
            {"session_id": sid_b, "title": f"{name} sess B", "source": "claude"},
        ]), encoding="utf-8")
        (conv / f"{sid_a}.jsonl").write_text(
            json.dumps({"id": "t1", "role": "user", "text": f"hi from {name} A"}) + "\n",
            encoding="utf-8",
        )
        (conv / f"{sid_b}.jsonl").write_text(
            json.dumps({"id": "t1", "role": "user", "text": f"hi from {name} B"}) + "\n",
            encoding="utf-8",
        )

    return proj


class _SandboxedRegistry(unittest.TestCase):
    """Base class that points _PROJECTS_FILE at a temp dir for the test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self._orig_global = pm_mod._GLOBAL_C3_DIR
        self._orig_projects = pm_mod._PROJECTS_FILE
        self._orig_registry = pm_mod._REGISTRY_FILE
        sandbox = self.tmp_root / "_home_c3"
        sandbox.mkdir(parents=True, exist_ok=True)
        pm_mod._GLOBAL_C3_DIR = sandbox
        pm_mod._PROJECTS_FILE = sandbox / "projects.json"
        pm_mod._REGISTRY_FILE = sandbox / "registry.json"

    def tearDown(self):
        pm_mod._GLOBAL_C3_DIR = self._orig_global
        pm_mod._PROJECTS_FILE = self._orig_projects
        pm_mod._REGISTRY_FILE = self._orig_registry
        self._tmp.cleanup()


class TestAddProjectIdempotency(_SandboxedRegistry):
    def test_add_project_is_idempotent(self):
        pm = ProjectManager()
        proj = _seed_project(self.tmp_root, "alpha", with_facts=False, with_ledger=False, with_conv=False)
        e1 = pm.add_project(str(proj))
        e2 = pm.add_project(str(proj))
        self.assertEqual(e1["path"], e2["path"])
        # Only one entry in the registry file
        data = json.loads(pm_mod._PROJECTS_FILE.read_text(encoding="utf-8"))
        same = [p for p in data["projects"] if p["path"] == str(proj.resolve())]
        self.assertEqual(len(same), 1)


class TestMergeKeep(_SandboxedRegistry):
    def test_merge_keep_combines_data_without_touching_source(self):
        pm = ProjectManager()
        src = _seed_project(self.tmp_root, "src_proj")
        tgt = _seed_project(self.tmp_root, "tgt_proj")
        # Tag the source to verify tag union.
        pm.add_project(str(src))
        pm.add_project(str(tgt))
        pm.update_project(str(src), tags=["alpha", "shared"], notes="src-notes")
        pm.update_project(str(tgt), tags=["shared", "beta"], notes="tgt-notes")

        result = pm.merge_projects(str(src), str(tgt), cleanup="keep")
        self.assertTrue(result.get("merged"), msg=result)
        self.assertEqual(result["cleanup"], "keep")
        stats = result["stats"]
        self.assertEqual(stats["facts"], 2)
        self.assertEqual(stats["ledger_entries"], 2)
        self.assertEqual(stats["sessions"], 2)

        # Target facts now contain merged entries with attribution.
        tgt_facts = json.loads((tgt / ".c3" / "facts" / "facts.json").read_text(encoding="utf-8"))
        merged = [f for f in tgt_facts if f.get("merged_from")]
        self.assertEqual(len(merged), 2)
        self.assertTrue(all(f["merged_from"] == "src_proj" for f in merged))
        # Original target facts still present.
        self.assertEqual(len([f for f in tgt_facts if not f.get("merged_from")]), 2)

        # Edit ledger appended with [merged from <name>] prefix and merge tag.
        ledger_lines = (tgt / ".c3" / "edit_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        merged_ledger = [json.loads(line) for line in ledger_lines if "[merged from src_proj]" in line]
        self.assertEqual(len(merged_ledger), 2)
        for entry in merged_ledger:
            self.assertIn("merged:src_proj", entry["tags"])
            self.assertEqual(entry["merged_from"], "src_proj")

        # Conversation sessions index merged.
        tgt_sessions = json.loads((tgt / ".c3" / "conversations" / "sessions.json").read_text(encoding="utf-8"))
        merged_sessions = [s for s in tgt_sessions if s.get("merged_from") == "src_proj"]
        self.assertEqual(len(merged_sessions), 2)
        # Per-session turn files copied over.
        copied = list((tgt / ".c3" / "conversations").glob("sess-src_proj-*.jsonl"))
        self.assertEqual(len(copied), 2)

        # Registry entry: tags unioned, notes appended with separator.
        projects = json.loads(pm_mod._PROJECTS_FILE.read_text(encoding="utf-8"))["projects"]
        tgt_entry = next(p for p in projects if p["path"] == str(tgt.resolve()))
        self.assertEqual(set(tgt_entry["tags"]), {"shared", "beta", "alpha"})
        self.assertIn("--- merged from src_proj ---", tgt_entry["notes"])
        self.assertIn("src-notes", tgt_entry["notes"])
        self.assertIn("tgt-notes", tgt_entry["notes"])

        # Source project: still registered, .c3/ untouched.
        src_paths = [p for p in projects if p["path"] == str(src.resolve())]
        self.assertEqual(len(src_paths), 1)
        self.assertTrue((src / ".c3" / "facts" / "facts.json").exists())
        self.assertTrue((src / ".c3" / "edit_ledger.jsonl").exists())


class TestMergeClear(_SandboxedRegistry):
    def test_merge_clear_wipes_source_c3_and_dropreg(self):
        pm = ProjectManager()
        src = _seed_project(self.tmp_root, "src2")
        tgt = _seed_project(self.tmp_root, "tgt2")
        pm.add_project(str(src))
        pm.add_project(str(tgt))
        # Drop a fake instruction doc + .mcp.json so the cleanup branch has work.
        (src / "CLAUDE.md").write_text("dummy", encoding="utf-8")
        (src / ".mcp.json").write_text(json.dumps({"mcpServers": {"c3": {"command": "x"}}}), encoding="utf-8")

        result = pm.merge_projects(str(src), str(tgt), cleanup="clear")
        self.assertTrue(result.get("merged"), msg=result)
        self.assertEqual(result["cleanup"], "clear")

        # Target gets the merged content.
        tgt_facts = json.loads((tgt / ".c3" / "facts" / "facts.json").read_text(encoding="utf-8"))
        self.assertTrue(any(f.get("merged_from") == "src2" for f in tgt_facts))

        # Source: .c3/ wiped, instruction doc removed, .mcp.json cleared, registry entry gone.
        self.assertFalse((src / ".c3").exists(), msg="source .c3 should be wiped")
        self.assertFalse((src / "CLAUDE.md").exists(), msg="CLAUDE.md should be removed")
        # .mcp.json either removed entirely or has c3 stripped.
        if (src / ".mcp.json").exists():
            cfg = json.loads((src / ".mcp.json").read_text(encoding="utf-8"))
            self.assertNotIn("c3", cfg.get("mcpServers", {}))

        projects = json.loads(pm_mod._PROJECTS_FILE.read_text(encoding="utf-8"))["projects"]
        self.assertFalse(any(p["path"] == str(src.resolve()) for p in projects),
                         msg="source registry entry should be dropped")
        # Source directory itself still exists.
        self.assertTrue(src.is_dir())

    def test_merge_clear_never_touches_machine_global_ide_configs(self):
        # Regression: cleanup='clear' once ran the full machine uninstall,
        # stripping C3 from the REAL ~/.codex/config.toml and deleting
        # Antigravity's mcp_config.json — configs shared by every project.
        fake_home = self.tmp_root / "home"
        codex_cfg = fake_home / ".codex" / "config.toml"
        codex_cfg.parent.mkdir(parents=True)
        codex_body = '[mcp_servers.c3]\ncommand = "c3-mcp"\nenabled = true\n'
        codex_cfg.write_text(codex_body, encoding="utf-8")
        ag_cfg = fake_home / ".gemini" / "antigravity" / "mcp_config.json"
        ag_cfg.parent.mkdir(parents=True)
        ag_body = json.dumps({"mcpServers": {"c3": {"command": "c3-mcp"}}})
        ag_cfg.write_text(ag_body, encoding="utf-8")

        pm = ProjectManager()
        src = _seed_project(self.tmp_root, "src3")
        tgt = _seed_project(self.tmp_root, "tgt3")
        pm.add_project(str(src))
        pm.add_project(str(tgt))

        with mock.patch("pathlib.Path.home", return_value=fake_home):
            result = pm.merge_projects(str(src), str(tgt), cleanup="clear")
        self.assertTrue(result.get("merged"), msg=result)
        self.assertEqual(codex_cfg.read_text(encoding="utf-8"), codex_body,
                         msg="~/.codex/config.toml must survive a project merge")
        self.assertTrue(ag_cfg.exists(),
                        msg="Antigravity mcp_config.json must survive a project merge")
        self.assertEqual(ag_cfg.read_text(encoding="utf-8"), ag_body)


class TestUninstallScope(_SandboxedRegistry):
    """_uninstall_mcp_all: include_global gates every Path.home() touch."""

    def _fixture(self):
        fake_home = self.tmp_root / "home2"
        codex_cfg = fake_home / ".codex" / "config.toml"
        codex_cfg.parent.mkdir(parents=True)
        codex_cfg.write_text('[mcp_servers.c3]\ncommand = "c3-mcp"\n',
                             encoding="utf-8")
        ag_cfg = fake_home / ".gemini" / "antigravity" / "mcp_config.json"
        ag_cfg.parent.mkdir(parents=True)
        ag_cfg.write_text(json.dumps({"mcpServers": {"c3": {"command": "x"}}}),
                          encoding="utf-8")
        proj = _seed_project(self.tmp_root, "scoped")
        return fake_home, codex_cfg, ag_cfg, proj

    def test_project_scope_leaves_home_configs_alone(self):
        from cli.c3 import _uninstall_mcp_all
        fake_home, codex_cfg, ag_cfg, proj = self._fixture()
        with mock.patch("pathlib.Path.home", return_value=fake_home):
            _uninstall_mcp_all(str(proj), include_global=False)
        self.assertIn("[mcp_servers.c3]", codex_cfg.read_text(encoding="utf-8"))
        self.assertTrue(ag_cfg.exists())

    def test_global_scope_still_cleans_home_configs(self):
        from cli.c3 import _uninstall_mcp_all
        fake_home, codex_cfg, ag_cfg, proj = self._fixture()
        with mock.patch("pathlib.Path.home", return_value=fake_home):
            _uninstall_mcp_all(str(proj), include_global=True)
        self.assertNotIn(
            "[mcp_servers.c3]",
            codex_cfg.read_text(encoding="utf-8") if codex_cfg.exists() else "",
        )
        if ag_cfg.exists():
            cfg = json.loads(ag_cfg.read_text(encoding="utf-8"))
            self.assertNotIn("c3", cfg.get("mcpServers", {}))


class TestMergeValidation(_SandboxedRegistry):
    def test_rejects_identical_paths(self):
        pm = ProjectManager()
        proj = _seed_project(self.tmp_root, "solo", with_facts=False, with_ledger=False, with_conv=False)
        pm.add_project(str(proj))
        result = pm.merge_projects(str(proj), str(proj), cleanup="keep")
        self.assertFalse(result["merged"])
        self.assertIn("identical", result["error"].lower())

    def test_rejects_unregistered_source(self):
        pm = ProjectManager()
        src = _seed_project(self.tmp_root, "ghost_src", with_facts=False, with_ledger=False, with_conv=False)
        tgt = _seed_project(self.tmp_root, "real_tgt", with_facts=False, with_ledger=False, with_conv=False)
        pm.add_project(str(tgt))
        # source intentionally NOT added
        result = pm.merge_projects(str(src), str(tgt), cleanup="keep")
        self.assertFalse(result["merged"])
        self.assertIn("not registered", result["error"].lower())

    def test_rejects_invalid_cleanup_value(self):
        pm = ProjectManager()
        src = _seed_project(self.tmp_root, "a", with_facts=False, with_ledger=False, with_conv=False)
        tgt = _seed_project(self.tmp_root, "b", with_facts=False, with_ledger=False, with_conv=False)
        pm.add_project(str(src))
        pm.add_project(str(tgt))
        result = pm.merge_projects(str(src), str(tgt), cleanup="zap")
        self.assertFalse(result["merged"])
        self.assertIn("cleanup", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
