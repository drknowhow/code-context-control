"""Hub batch update: bounded parallel workers, skips, and the global CLAUDE.md refresh."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.c3 as c3  # noqa: E402
import cli.hub_server as hub_server  # noqa: E402
from services.claude_md import C3_BLOCK_BEGIN, C3_BLOCK_END  # noqa: E402


def _state():
    return {"cancelled": False, "results": [], "in_progress": [],
            "current": None, "current_index": 0}


class RunPooled(unittest.TestCase):
    def test_runs_items_concurrently_up_to_worker_limit(self):
        state, lock = _state(), threading.Lock()
        active, peak = [0], [0]
        counter_lock = threading.Lock()

        def run_one(item):
            with counter_lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with counter_lock:
                active[0] -= 1
            return {"path": item["path"], "success": True}

        items = [{"path": f"p{i}"} for i in range(7)]
        hub_server._run_pooled(items, run_one, state, lock, workers=3)

        self.assertEqual(peak[0], 3)
        self.assertEqual(sorted(r["path"] for r in state["results"]),
                         sorted(i["path"] for i in items))
        self.assertEqual(state["in_progress"], [])
        self.assertTrue(all("elapsed_ms" in r for r in state["results"]))

    def test_cancel_skips_items_not_yet_started(self):
        state, lock = _state(), threading.Lock()

        def run_one(item):
            with lock:
                state["cancelled"] = True
            return {"path": item["path"], "success": True}

        items = [{"path": f"p{i}"} for i in range(5)]
        hub_server._run_pooled(items, run_one, state, lock, workers=1)

        self.assertEqual([r["path"] for r in state["results"]], ["p0"])


class UpdateProjectRow(unittest.TestCase):
    def test_missing_folder_is_skipped_without_running_init(self):
        with mock.patch.object(hub_server, "_run_c3") as run:
            row = hub_server._update_project_row({"path": "Z:/no/such/dir", "name": "x"})
        run.assert_not_called()
        self.assertTrue(row["skipped"])
        self.assertFalse(row["success"])

    def test_init_gets_update_timeout_and_ide(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hub_server, "_run_c3",
                                  return_value={"success": True, "output": "ok",
                                                "returncode": 0}) as run:
            row = hub_server._update_project_row({"path": tmp, "ide": "codex"})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["init", tmp, "--force", "--ide", "codex"])
        self.assertEqual(kwargs["timeout"], hub_server._UPDATE_TIMEOUT_S)
        self.assertTrue(row["success"])

    def test_unknown_ide_is_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hub_server, "_run_c3",
                                  return_value={"success": True}) as run:
            hub_server._update_project_row({"path": tmp, "ide": "unknown"})
        self.assertEqual(run.call_args[0][0], ["init", tmp, "--force"])


class UpdateWorkers(unittest.TestCase):
    def test_reads_and_clamps_hub_config(self):
        for raw, want in ((None, 3), (5, 5), (0, 3), (50, 8), ("x", 3), (-2, 1)):
            with mock.patch.object(hub_server, "_read_hub_config",
                                   return_value={"update_workers": raw}):
                self.assertEqual(hub_server._update_workers(), want, raw)


class BatchRoute(unittest.TestCase):
    def setUp(self):
        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()
        hub_server._batch_state["running"] = False

    def _wait_done(self):
        for _ in range(200):
            st = self.client.get("/api/projects/run-init/batch/status").get_json()
            if st["done"]:
                return st
            time.sleep(0.02)
        self.fail("batch did not finish")

    def test_batch_dedupes_paths_and_reports_workers(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                mock.patch.object(hub_server, "_read_hub_config",
                                  return_value={"update_workers": 2}), \
                mock.patch.object(hub_server, "_run_c3",
                                  return_value={"success": True, "output": "",
                                                "returncode": 0}) as run:
            resp = self.client.post("/api/projects/run-init/batch", json={
                "projects": [{"path": a}, {"path": b}, {"path": a + "/"}],
            }).get_json()
            st = self._wait_done()

        self.assertEqual(resp["total"], 2)
        self.assertEqual(resp["workers"], 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(st["results"]), 2)
        self.assertEqual(st["in_progress"], [])
        self.assertFalse(st["running"])


class GlobalClaudeMd(unittest.TestCase):
    def _home(self, tmp, text):
        md = Path(tmp) / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True)
        md.write_text(text, encoding="utf-8")
        return md

    def _block(self):
        return f"{C3_BLOCK_BEGIN}\n{c3._GLOBAL_CLAUDE_MD_CONTENT.strip()}\n{C3_BLOCK_END}"

    def test_up_to_date_file_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = self._home(tmp, "# Mine\n\nkeep me\n\n" + self._block() + "\n")
            from services import atomic_json
            out = io.StringIO()
            with mock.patch.object(Path, "home", return_value=Path(tmp)), \
                    mock.patch.object(Path, "write_text") as plain_write, \
                    mock.patch.object(atomic_json, "write_text_atomic") as write, \
                    contextlib.redirect_stdout(out):
                c3._ensure_global_claude_md()
            write.assert_not_called()
            plain_write.assert_not_called()
            self.assertIn("Kept", out.getvalue())
            self.assertIn("keep me", md.read_text(encoding="utf-8"))

    def test_outdated_block_is_replaced_and_user_text_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = self._home(tmp, f"# Mine\n\nkeep me\n\n{C3_BLOCK_BEGIN}\nold\n{C3_BLOCK_END}\n")
            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                c3._ensure_global_claude_md()
            text = md.read_text(encoding="utf-8")
            self.assertIn("keep me", text)
            self.assertNotIn("\nold\n", text)
            self.assertIn(c3._GLOBAL_CLAUDE_MD_CONTENT.strip().splitlines()[0], text)


if __name__ == "__main__":
    unittest.main()
