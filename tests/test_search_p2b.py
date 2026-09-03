"""P2b: the SQLite index store and incremental refresh (docs/search-eval.md).

Pins: index.json is no longer written and a legacy one is migrated; a fresh
CodeIndex loads documents, chunks, mtimes and symbols from the store;
refresh() re-chunks only files whose content hash changed, adds new files,
drops deleted ones, and leaves unchanged files' chunks untouched; the watcher
hands its changed paths to refresh; the TF-IDF fallback still works on a
store-loaded index (vectors are built on demand).
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from services import access_guard as ag
from services import lexical_index as lx
from services.index_store import DB_NAME, IndexStore
from services.indexer import CodeIndex


class _Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self._home = mock.patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.write("src/auth.py", "def sha256_digest(data):\n    return data\n")
        self.write("src/store.py", "class Store:\n    def migrate_v2(self):\n        return 2\n")
        self.write("src/routes.py", "def register_routes(app):\n    return app\n")
        self.write("docs/guide.md", "# Guide\n\nintro\n")

    def tearDown(self):
        self._home.stop()
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def config(self, cfg):
        (self.root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def build(self):
        idx = CodeIndex(str(self.root))
        idx.build_index()
        return idx

    @staticmethod
    def files(hits):
        return [h["file"].replace("\\", "/") for h in hits]

    @staticmethod
    def doc_ids(idx):
        return {d.replace("\\", "/") for d in idx.documents}


class TestStore(_Project):
    def test_build_writes_the_store_and_no_json(self):
        idx = self.build()
        self.assertTrue((idx.index_dir / DB_NAME).exists())
        self.assertFalse((idx.index_dir / "index.json").exists())
        self.assertEqual(IndexStore(idx.index_dir).counts(), (4, len(idx.chunks)))
        self.assertTrue(idx.index_exists())
        self.assertFalse(idx.needs_migration())

    def test_fresh_instance_loads_everything_from_the_store(self):
        built = self.build()
        fresh = CodeIndex(str(self.root))
        self.assertTrue(fresh._load_index())
        self.assertEqual(set(fresh.chunks), set(built.chunks))
        for cid, chunk in fresh.chunks.items():
            self.assertEqual(chunk["content"], built.chunks[cid]["content"])
            self.assertEqual((chunk["line_start"], chunk["line_end"]),
                             (built.chunks[cid]["line_start"], built.chunks[cid]["line_end"]))
        self.assertEqual(fresh.documents, built.documents)
        self.assertEqual(fresh._file_mtimes, built._file_mtimes)
        self.assertEqual(fresh._hashes, built._hashes)
        self.assertEqual(fresh.symbols, built.symbols)
        self.assertEqual(fresh.lexical_engine, built.lexical_engine)
        self.assertEqual(self.files(fresh.search("sha256_digest", top_k=1, max_tokens=800))[0], "src/auth.py")

    def test_legacy_files_are_migrated_and_removed(self):
        idx = self.build()
        legacy_json = idx.index_dir / "index.json"
        legacy_json.write_text("{}", encoding="utf-8")
        (idx.index_dir / "lexical.sqlite").write_bytes(b"")
        (idx.index_dir / DB_NAME).unlink()
        fresh = CodeIndex(str(self.root))
        self.assertTrue(fresh.needs_migration())
        self.assertTrue(fresh._load_index())
        self.assertFalse(legacy_json.exists())
        self.assertFalse((idx.index_dir / "lexical.sqlite").exists())
        self.assertTrue(fresh.index_exists())

    def test_tfidf_fallback_builds_vectors_on_demand_after_load(self):
        self.config({"search_engine": "tfidf"})
        self.build()
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        self.assertEqual(fresh.lexical_engine, "tfidf")
        self.assertEqual(fresh.chunk_tfidf, {}, "vectors are not persisted")
        self.assertEqual(self.files(fresh.search("migrate_v2", top_k=1, max_tokens=800))[0], "src/store.py")
        self.assertTrue(fresh.chunk_tfidf, "built lazily by the first fallback query")

    def test_stats_report_store_size_and_engine(self):
        stats = self.build().get_stats()
        self.assertGreater(stats["index_size_kb"], 0)
        self.assertIn(stats["lexical_engine"], ("fts5", "tfidf"))


class TestRefresh(_Project):
    def test_only_changed_files_are_rechunked(self):
        idx = self.build()
        before = {cid: dict(c) for cid, c in idx.chunks.items()}
        self.write("src/auth.py", "def sha256_digest(data):\n    return data\n\ndef hmac_sign(data):\n    return data\n")
        with mock.patch.object(CodeIndex, "_chunk_file", wraps=idx._chunk_file) as spy:
            result = idx.refresh()
        self.assertEqual(result["mode"], "incremental")
        self.assertEqual((result["files_changed"], result["files_added"], result["files_removed"]), (1, 0, 0))
        self.assertEqual(result["files_unchanged"], 3)
        self.assertEqual(spy.call_count, 1, "unchanged files must not be re-chunked")
        # Untouched files keep their exact chunks; the changed file gained a symbol.
        for cid, chunk in before.items():
            if not cid.replace("\\", "/").startswith("src/auth.py"):
                self.assertEqual(idx.chunks[cid]["content"], chunk["content"])
        self.assertIn("hmac_sign", idx.symbols)
        self.assertEqual(self.files(idx.search("hmac_sign", top_k=1, max_tokens=800))[0], "src/auth.py")
        # And the store agrees.
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        self.assertIn("hmac_sign", fresh.symbols)
        self.assertEqual(self.files(fresh.search("hmac_sign", top_k=1, max_tokens=800))[0], "src/auth.py")

    def test_added_and_removed_files(self):
        idx = self.build()
        self.write("src/cache.py", "class LruCache:\n    def evict(self):\n        return 0\n")
        (self.root / "src" / "routes.py").unlink()
        result = idx.refresh()
        self.assertEqual((result["files_added"], result["files_removed"]), (1, 1))
        self.assertIn("src/cache.py", self.doc_ids(idx))
        self.assertNotIn("src/routes.py", self.doc_ids(idx))
        self.assertNotIn("register_routes", idx.symbols)
        self.assertEqual(idx.search("register_routes", top_k=1, max_tokens=800), [])
        self.assertEqual(self.files(idx.search("LruCache", top_k=1, max_tokens=800))[0], "src/cache.py")
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        self.assertEqual(self.doc_ids(fresh), self.doc_ids(idx))
        self.assertEqual(IndexStore(idx.index_dir).counts()[0], 4)

    def test_refresh_with_explicit_paths(self):
        idx = self.build()
        self.write("src/store.py", "class Store:\n    def migrate_v3(self):\n        return 3\n")
        self.write("src/extra.py", "def extra():\n    return 1\n")
        (self.root / "docs" / "guide.md").unlink()
        result = idx.refresh(paths=[str(self.root / "src" / "store.py"), "src/extra.py", "docs/guide.md",
                                    "src/ignored.txt"])
        self.assertEqual((result["files_changed"], result["files_added"], result["files_removed"]), (1, 1, 1))
        # Method symbols are keyed by their dotted name; the tail map holds the bare one.
        self.assertIn("store.migrate_v3", idx.symbols)
        self.assertIn("migrate_v3", idx._symbol_tail)
        self.assertNotIn("migrate_v2", idx._symbol_tail)
        self.assertIn("src/extra.py", self.doc_ids(idx))
        self.assertNotIn("docs/guide.md", self.doc_ids(idx))

    def test_no_changes_is_a_cheap_no_op(self):
        idx = self.build()
        with mock.patch.object(IndexStore, "apply") as apply:
            result = idx.refresh()
        apply.assert_not_called()
        self.assertEqual(result["files_changed"] + result["files_added"] + result["files_removed"], 0)
        self.assertEqual(result["files_unchanged"], 4)

    def test_refresh_without_a_store_builds(self):
        idx = CodeIndex(str(self.root))
        result = idx.refresh()
        self.assertEqual(result["mode"], "full")
        self.assertTrue(idx.index_exists())

    def test_refresh_falls_back_to_full_build_when_the_write_fails(self):
        idx = self.build()
        self.write("src/auth.py", "def sha256_digest(data):\n    return 'changed'\n")
        with mock.patch.object(IndexStore, "apply", side_effect=RuntimeError("disk")):
            result = idx.refresh()
        self.assertEqual(result["mode"], "full")
        self.assertIn("changed", idx.chunks[next(c for c in idx.chunks if "sha256_digest" in c)]["content"])

    def test_refresh_under_tfidf_rebuilds_vectors(self):
        self.config({"search_engine": "tfidf"})
        idx = self.build()
        self.write("src/auth.py", "def sha256_digest(data):\n    return data\n\ndef brand_new(x):\n    return x\n")
        idx.refresh()
        self.assertEqual(self.files(idx.search("brand_new", top_k=1, max_tokens=800))[0], "src/auth.py")


class TestWatcherWiring(unittest.TestCase):
    def test_rebuild_if_needed_hands_changed_paths_to_refresh(self):
        from services.watcher import CodeWatcher

        watcher = CodeWatcher.__new__(CodeWatcher)
        watcher._handler = types.SimpleNamespace(change_count=3)
        watcher.get_changes = lambda: [{"path": "/p/a.py", "type": "modified"}, {"path": "/p/b.py", "type": "deleted"}]
        indexer = mock.Mock()
        indexer.refresh.return_value = {"mode": "incremental"}
        result = watcher.rebuild_if_needed(indexer, threshold=2)
        indexer.refresh.assert_called_once_with(paths=["/p/a.py", "/p/b.py"])
        indexer.build_index.assert_not_called()
        self.assertEqual(result["triggered_by_changes"], 2)

    def test_stub_indexer_without_refresh_still_rebuilds(self):
        from services.watcher import CodeWatcher

        watcher = CodeWatcher.__new__(CodeWatcher)
        watcher._handler = types.SimpleNamespace(change_count=1)
        watcher.get_changes = lambda: [{"path": "/p/a.py", "type": "modified"}]
        indexer = types.SimpleNamespace(build_index=lambda: {"mode": "full"})
        self.assertEqual(watcher.rebuild_if_needed(indexer, threshold=1)["mode"], "full")


@unittest.skipUnless(lx.fts5_available(), "SQLite built without FTS5")
class TestStoreApply(unittest.TestCase):
    def test_apply_upserts_and_deletes_in_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = IndexStore(Path(tmp))
            # Two-letter names: the tokenizer drops single letters on purpose.
            chunk = {"doc_id": "a.py", "name": "fa", "type": "function", "content": "def fa(): pass",
                     "line_start": 0, "line_end": 0, "tokens": 5}
            store.write_all({"a.py": {"path": "a.py", "lines": 1, "tokens": 5}}, {"a.py::fa": chunk},
                            {"a.py": 1.0}, {"a.py": "h1"})
            new_chunk = dict(chunk, doc_id="b.py", name="gb", content="def gb(): pass")
            store.apply({"b.py": ({"path": "b.py", "lines": 1, "tokens": 5}, [("b.py::gb", new_chunk)])},
                        ["a.py"], {"b.py": 2.0}, {"b.py": "h2"})
            self.assertEqual(store.counts(), (1, 1))
            self.assertEqual(store.doc_hashes(), {"b.py": "h2"})
            self.assertEqual([cid for cid, _ in store.search(["gb"])], ["b.py::gb"])
            self.assertEqual(store.search(["fa"]), [])


if __name__ == "__main__":
    unittest.main()
