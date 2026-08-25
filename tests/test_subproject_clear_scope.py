"""``c3 sub remove --clear`` must not deregister C3 from the whole machine.

2.89.1 fixed exactly this for the Hub's Merge action: ``cleanup='clear'``
called ``_uninstall_mcp_all(src)`` — the MACHINE uninstall — which walks
``Path.home()`` and strips C3 from ``~/.codex/config.toml`` and Antigravity's
``mcp_config.json``. Those files are shared by every C3 project on the box, so
cleaning up ONE project silently broke all of them. ``merge_projects`` was
given ``include_global=False``; ``SubprojectManager.remove(mode='clear')``
was not, and kept the bug until 2.96.1.

Why no test caught it: ``SubprojectBase`` in ``test_subprojects.py`` patches
``cli.c3._uninstall_mcp_all`` with a lambda that swallows every argument, so
the one argument that mattered could not be observed. These tests deliberately
run the REAL helper against a fake home, and assert on the files rather than on
the call — a stub that ignores its kwargs cannot fail the way this bug did.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import project_manager as pm_mod  # noqa: E402
from services import subprojects as sp  # noqa: E402

CODEX_BODY = '[mcp_servers.c3]\ncommand = "c3-mcp"\n'
AG_BODY = json.dumps({"mcpServers": {"c3": {"command": "c3-mcp"}}})


def _seed_c3_project(path: Path, name: str) -> Path:
    (path / ".c3").mkdir(parents=True, exist_ok=True)
    (path / ".c3" / "config.json").write_text(
        json.dumps({"meta": {"name": name}}), encoding="utf-8")
    return path


class TestSubprojectClearScope(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

        self.parent = _seed_c3_project(self.base / "parent", "parent")
        # Deliberately OUTSIDE the parent: the external-link path is the one
        # 2.96.0 made reachable, so it is the one that must be regression-proof.
        self.child = _seed_c3_project(self.base / "elsewhere" / "child", "child")

        self.reg_file = self.base / "projects.json"
        self.reg_file.write_text(json.dumps({"projects": []}), encoding="utf-8")

        self.home = self.base / "fakehome"
        self.codex_cfg = self.home / ".codex" / "config.toml"
        self.codex_cfg.parent.mkdir(parents=True)
        self.codex_cfg.write_text(CODEX_BODY, encoding="utf-8")
        self.ag_cfg = self.home / ".gemini" / "antigravity" / "mcp_config.json"
        self.ag_cfg.parent.mkdir(parents=True)
        self.ag_cfg.write_text(AG_BODY, encoding="utf-8")

        self._patches = [
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
            mock.patch("cli.c3._do_init", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _link_child(self):
        mgr = sp.SubprojectManager(str(self.parent))
        res = mgr.add(str(self.child), run_init=False, reindex_parent=False)
        self.assertTrue(res.get("added"), msg=res)
        return mgr

    def test_clear_leaves_machine_wide_mcp_configs_alone(self):
        mgr = self._link_child()
        with mock.patch("pathlib.Path.home", return_value=self.home):
            res = mgr.remove("child", mode="clear", reindex_parent=False)
        self.assertTrue(res.get("removed"), msg=res)

        self.assertEqual(
            self.codex_cfg.read_text(encoding="utf-8"), CODEX_BODY,
            msg="~/.codex/config.toml serves every C3 project on the box; "
                "removing one sub-project must not touch it")
        self.assertTrue(
            self.ag_cfg.exists(),
            msg="Antigravity mcp_config.json must survive a sub-project clear")
        self.assertEqual(self.ag_cfg.read_text(encoding="utf-8"), AG_BODY)

    def test_clear_still_removes_the_childs_own_files(self):
        """The narrowed scope must not turn --clear into a no-op."""
        mgr = self._link_child()
        (self.child / "CLAUDE.md").write_text("x", encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=self.home):
            res = mgr.remove("child", mode="clear", reindex_parent=False)
        self.assertTrue(res.get("removed"), msg=res)
        self.assertFalse((self.child / ".c3").exists(),
                         msg="--clear must still wipe the child's own .c3")
        self.assertFalse((self.child / "CLAUDE.md").exists(),
                         msg="--clear must still remove the child's instruction docs")

    def test_unlink_never_reaches_the_uninstall_helper_at_all(self):
        mgr = self._link_child()
        with mock.patch("cli.c3._uninstall_mcp_all") as spy:
            res = mgr.remove("child", mode="unlink", reindex_parent=False)
        self.assertTrue(res.get("removed"), msg=res)
        spy.assert_not_called()
        self.assertTrue((self.child / ".c3").exists(),
                        msg="unlink keeps the child's .c3")


if __name__ == "__main__":
    unittest.main()
