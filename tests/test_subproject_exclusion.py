"""Sub-project exclusion across the four scan sites (index/doc/dict/watcher)."""
import json
import tempfile
import unittest
from pathlib import Path

from services.doc_index import DocIndex
from services.indexer import CodeIndex
from services.protocol import CompressionProtocol
from services.watcher import _ChangeHandler
from services.subprojects import make_excluder


class ExclusionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "main.py").write_text("def parent_entry():\n    return 1\n")
        (self.root / "README.md").write_text("# Parent\n\nparent docs\n")
        (self.root / "services").mkdir()
        (self.root / "services" / "core.py").write_text("def parent_service():\n    return 2\n")
        # Designated sub-project at services/api
        sub = self.root / "services" / "api"
        sub.mkdir()
        (sub / "child.py").write_text(
            "def child_function_name():\n    return childidentifier\n" * 6)
        (sub / "CHILD.md").write_text("# Child docs\n")
        # Same-named sibling at root — must NOT be excluded (prefix vs name).
        (self.root / "api").mkdir()
        (self.root / "api" / "sibling.py").write_text("def sibling_fn():\n    return 4\n")
        (self.root / ".c3").mkdir()
        (self.root / ".c3" / "config.json").write_text(json.dumps({
            "subprojects": [{"name": "api", "rel_path": "services/api", "added_at": "x"}]
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class TestCodeIndexExclusion(ExclusionBase):
    def test_child_excluded_sibling_kept(self):
        idx = CodeIndex(str(self.root))
        idx.build_index()
        docs = list(idx.documents.keys())
        self.assertFalse(any("child.py" in d for d in docs), docs)
        self.assertTrue(any("sibling.py" in d for d in docs), docs)
        self.assertTrue(any("core.py" in d for d in docs), docs)

    def test_no_subprojects_indexes_everything(self):
        (self.root / ".c3" / "config.json").write_text("{}", encoding="utf-8")
        idx = CodeIndex(str(self.root))
        idx.build_index()
        self.assertTrue(any("child.py" in d for d in idx.documents))


class TestDocIndexExclusion(ExclusionBase):
    def test_child_docs_excluded(self):
        di = DocIndex(str(self.root))
        rels = [rel for rel, _ in di._discover_files()]
        self.assertFalse(any("CHILD.md" in r for r in rels), rels)
        self.assertTrue(any("README.md" in r for r in rels), rels)


class TestDictionaryExclusion(ExclusionBase):
    def test_child_identifiers_excluded(self):
        terms = CompressionProtocol(str(self.root)).build_project_dictionary()
        self.assertNotIn("childidentifier", terms)
        self.assertNotIn("child_function_name", terms)


class TestWatcherExclusion(ExclusionBase):
    def test_handler_ignores_child_paths(self):
        handler = _ChangeHandler(make_excluder(str(self.root)))
        self.assertFalse(handler._should_track(str(self.root / "services" / "api" / "child.py")))
        self.assertTrue(handler._should_track(str(self.root / "api" / "sibling.py")))
        self.assertTrue(handler._should_track(str(self.root / "main.py")))

    def test_handler_without_excluder_tracks_all(self):
        handler = _ChangeHandler()
        self.assertTrue(handler._should_track(str(self.root / "services" / "api" / "child.py")))


if __name__ == "__main__":
    unittest.main()
