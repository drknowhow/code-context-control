"""c3_edit pre/post images and c3_edits(action='revert')."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from cli.tools.edit import handle_edit
from cli.tools.edits import handle_edits
from services import edit_blobs
from services.edit_ledger import EditLedger


class _Svc:
    def __init__(self, project_path):
        self.project_path = str(project_path)
        self.edit_ledger = EditLedger(str(project_path))
        self.activity_log = None
        self.session_mgr = None
        self.artifact_store = None


def _finalize(name, args, resp, summ, **kw):
    return resp


class RevertBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.svc = _Svc(self.root)
        self.file = self.root / "mod.py"
        self.original = b"def f():\r\n    return 1\r\n\xff\n"
        self.file.write_bytes(self.original)

    def edit(self, old, new, **kw):
        return handle_edit(str(self.file), old, new, "", "", False, self.svc,
                           _finalize, **kw)

    def revert(self, edit_id):
        return handle_edits("revert", "", "", "", "", "", 0, "", edit_id, "",
                            self.svc, _finalize)

    def rows(self):
        return self.svc.edit_ledger.get_history(limit=1000)

    def last_id(self):
        return self.rows()[-1]["id"]

    def write_config(self, data):
        cfg = self.root / ".c3" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data), encoding="utf-8")


class TestImagesRecorded(RevertBase):
    def test_modify_row_carries_both_hashes_and_stores_pre_image(self):
        self.edit("return 1", "return 2")
        d = self.rows()[-1]["detail"]
        self.assertEqual(d["pre_sha256"], edit_blobs.sha256(self.original))
        self.assertEqual(d["post_sha256"],
                         edit_blobs.sha256(self.file.read_bytes()))
        self.assertEqual(d["blob"], "stored")
        self.assertEqual(edit_blobs.get(self.root, d["pre_sha256"]),
                         self.original)

    def test_batch_row_carries_hashes(self):
        self.edit("", "", edits=json.dumps(
            [{"old_string": "return 1", "new_string": "return 3"}]))
        d = self.rows()[-1]["detail"]
        self.assertEqual(d["pre_sha256"], edit_blobs.sha256(self.original))
        self.assertIn("patches", d)

    def test_file_over_size_cap_records_hashes_but_no_blob(self):
        self.write_config({"edit": {"blob_max_file_mb": 0.00001}})
        self.edit("return 1", "return 2")
        d = self.rows()[-1]["detail"]
        self.assertEqual(d["blob"], "skipped:too-large")
        self.assertEqual(d["pre_sha256"], edit_blobs.sha256(self.original))
        self.assertIsNone(edit_blobs.get(self.root, d["pre_sha256"]))

    def test_read_denied_path_is_not_blobbed(self):
        self.write_config({"access": {"deny": ["secret*.txt"]}})
        target = self.root / "secret_notes.txt"
        d = edit_blobs.record(self.root, target, b"hunter2", b"hunter3")
        self.assertEqual(d["blob"], "skipped:read-denied")
        self.assertIsNone(edit_blobs.get(self.root, edit_blobs.sha256(b"hunter2")))
        self.assertFalse(edit_blobs.store_dir(self.root).exists())

    def test_blob_store_is_read_denied_to_agents(self):
        from services import access_guard
        self.edit("return 1", "return 2")
        pre = self.rows()[-1]["detail"]["pre_sha256"]
        blob = edit_blobs.store_dir(self.root) / pre[:2] / pre
        self.assertTrue(blob.exists())
        self.assertIsNotNone(access_guard.check(str(blob), "read", str(self.root)))


class TestRevert(RevertBase):
    def test_revert_restores_exact_bytes(self):
        self.edit("return 1", "return 2")
        out = self.revert(self.last_id())
        self.assertIn("restored", out)
        self.assertEqual(self.file.read_bytes(), self.original)
        row = self.rows()[-1]
        self.assertEqual(row["change_type"], "reverted")

    def test_revert_refused_when_file_changed_after(self):
        self.edit("return 1", "return 2")
        first = self.last_id()
        self.edit("return 2", "return 5")
        second = self.last_id()
        after_second = self.file.read_bytes()
        out = self.revert(first)
        self.assertIn("[c3-revert:changed]", out)
        self.assertIn(second, out)
        self.assertEqual(self.file.read_bytes(), after_second)

    def test_revert_of_revert_reapplies_the_edit(self):
        self.edit("return 1", "return 2")
        edited = self.file.read_bytes()
        self.revert(self.last_id())
        out = self.revert(self.last_id())
        self.assertIn("restored", out)
        self.assertEqual(self.file.read_bytes(), edited)

    def test_revert_of_create_deletes_the_file(self):
        new = self.root / "pkg" / "new.py"
        handle_edit(str(new), "", "x = 1\n", "", "", False, self.svc, _finalize)
        self.assertIsNone(self.rows()[-1]["detail"]["pre_sha256"])
        out = self.revert(self.last_id())
        self.assertIn("deleted", out)
        self.assertFalse(new.exists())
        self.revert(self.last_id())
        self.assertEqual(new.read_bytes(), b"x = 1\n")

    def test_revert_of_create_refused_when_file_changed(self):
        new = self.root / "new.py"
        handle_edit(str(new), "", "x = 1\n", "", "", False, self.svc, _finalize)
        new.write_bytes(b"x = 2\n")
        out = self.revert(self.last_id())
        self.assertIn("[c3-revert:changed]", out)
        self.assertIn("outside C3", out)
        self.assertTrue(new.exists())

    def test_row_without_images_is_refused(self):
        entry = self.svc.edit_ledger.log_edit(file="mod.py", change_type="modified",
                                              summary="native", include_git=False)
        out = self.revert(entry["id"])
        self.assertIn("[c3-revert:no-image]", out)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_skipped_blob_is_refused_with_reason(self):
        self.write_config({"edit": {"blob_max_file_mb": 0.00001}})
        self.edit("return 1", "return 2")
        out = self.revert(self.last_id())
        self.assertIn("skipped:too-large", out)

    def test_revert_goes_through_the_access_guard(self):
        self.edit("return 1", "return 2")
        edited = self.file.read_bytes()
        self.write_config({"access": {"read_only": ["mod.py"]}})
        out = self.revert(self.last_id())
        self.assertIn("[c3-access:", out)
        self.assertEqual(self.file.read_bytes(), edited)


class TestEviction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_oldest_blobs_evicted_under_a_tiny_cap(self):
        cfg = self.root / ".c3" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"edit": {"blob_cap_mb": 0.0015}}),
                       encoding="utf-8")
        shas = []
        for i in range(4):
            sha = edit_blobs.put(self.root, os.urandom(600))
            p = edit_blobs.store_dir(self.root) / sha[:2] / sha
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
            shas.append(sha)
        removed = edit_blobs.sweep(self.root)
        self.assertGreater(removed, 0)
        self.assertIsNone(edit_blobs.get(self.root, shas[0]))
        self.assertIsNotNone(edit_blobs.get(self.root, shas[-1]))


if __name__ == "__main__":
    unittest.main()
