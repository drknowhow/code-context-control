import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli import c3
from services.indexer import CodeIndex


class TestInitIndexRefresh(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
        with mock.patch.object(c3, "_sync_project_instruction_docs"):
            c3._do_init(str(self.root), ide_name="claude", no_embed=True)

    def tearDown(self):
        self._td.cleanup()

    def _init(self, **kwargs):
        with mock.patch.object(c3, "_sync_project_instruction_docs"), \
                mock.patch.object(CodeIndex, "build_index", autospec=True,
                                  side_effect=CodeIndex.build_index) as build:
            c3._do_init(str(self.root), ide_name="claude", no_embed=True, **kwargs)
        return build

    def test_force_reinit_of_same_version_does_not_rebuild_the_index(self):
        build = self._init(rebuild_index=False)
        build.assert_not_called()

    def test_incremental_init_picks_up_an_edited_and_a_new_file(self):
        (self.root / "a.py").write_text("def alpha():\n    return 11\n\ndef gamma():\n    return 3\n",
                                        encoding="utf-8")
        (self.root / "c.py").write_text("def delta():\n    return 4\n", encoding="utf-8")
        self._init(rebuild_index=False)
        idx = CodeIndex(str(self.root))
        idx._load_index()
        names = {c.get("name") for c in idx.chunks.values()}
        self.assertTrue({"gamma", "delta"} <= names)

    def test_incremental_init_drops_a_deleted_file(self):
        (self.root / "b.py").unlink()
        self._init(rebuild_index=False)
        idx = CodeIndex(str(self.root))
        idx._load_index()
        self.assertNotIn("b.py", idx.documents)

    def test_rebuild_flag_forces_a_full_build(self):
        build = self._init(rebuild_index=True)
        build.assert_called_once()

    def test_index_from_another_version_is_rebuilt(self):
        with mock.patch.object(c3, "__version__", "0.0.1"):
            self._init(rebuild_index=True)
        build = self._init(rebuild_index=False)
        build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
