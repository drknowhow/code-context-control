"""Index freshness and the exact-search fast path (2.159.2).

Pins: a server start re-checks the stored index against the files, so a
change made while no server ran (a git pull, a branch switch) is searchable
and files whose mtime still matches are not read; a search first re-reads
the files the watcher reported, never waiting out a full build; refresh
swaps in new dicts instead of editing the ones a running search holds; the
watcher keeps the index's changed paths apart from the agents' change list
and counts structural events; ``exact`` reuses its file walk while that
count, the rules and the root .gitignore hold, and reads files without a
per-file verdict only when deny rules settle every read.
"""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

from watchdog.events import DirCreatedEvent, FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent

from cli.tools import search as search_tool
from services import access_guard as ag
from services.index_store import IndexStore
from services.indexer import CodeIndex
from services.watcher import CodeWatcher, _ChangeHandler


class _Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
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

    def write(self, rel, text, mtime=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def build(self):
        idx = CodeIndex(str(self.root))
        idx.build_index()
        return idx

    def later(self, idx, rel):
        """A whole-second mtime clearly after the stored one (exact as a float)."""
        return float(int(idx._file_mtimes[rel]) + 30)

    @staticmethod
    def files(hits):
        return [h["file"].replace("\\", "/") for h in hits]

    @staticmethod
    def doc_ids(idx):
        return {d.replace("\\", "/") for d in idx.documents}

    @staticmethod
    def read_names(spy):
        return sorted(Path(c.args[0]).name for c in spy.call_args_list)


class TestRefreshStale(_Project):
    def test_changes_made_while_no_server_ran_are_picked_up(self):
        built = self.build()
        rel = str(Path("src/auth.py"))
        self.write("src/auth.py", "def hmac_sign(data):\n    return data\n", mtime=self.later(built, rel))
        self.write("src/cache.py", "class LruCache:\n    pass\n")
        (self.root / "src" / "routes.py").unlink()

        fresh = CodeIndex(str(self.root))  # the next server start
        result = fresh.refresh_stale()

        self.assertEqual((result["files_changed"], result["files_added"], result["files_removed"]), (1, 1, 1))
        self.assertEqual(self.files(fresh.search("hmac_sign", top_k=1, max_tokens=800)), ["src/auth.py"])
        self.assertEqual(fresh.search("sha256_digest", top_k=1, max_tokens=800), [])
        self.assertEqual(fresh.search("register_routes", top_k=1, max_tokens=800), [])
        self.assertIn("src/cache.py", self.doc_ids(fresh))
        reloaded = CodeIndex(str(self.root))
        reloaded._load_index()
        self.assertEqual(self.doc_ids(reloaded), self.doc_ids(fresh))

    def test_files_whose_mtime_matches_are_not_read(self):
        built = self.build()
        rel = str(Path("src/store.py"))
        self.write("src/store.py", "class Store:\n    def migrate_v3(self):\n        return 3\n",
                   mtime=self.later(built, rel))
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        with mock.patch.object(CodeIndex, "_read_indexable", wraps=fresh._read_indexable) as spy:
            result = fresh.refresh_stale()
        self.assertEqual(self.read_names(spy), ["store.py"])
        self.assertEqual(result["files_unchanged"], 3)
        self.assertIn("migrate_v3", fresh._symbol_tail)

    def test_a_touched_file_is_read_once_then_trusted_again(self):
        built = self.build()
        rel = str(Path("src/routes.py"))
        new_mtime = self.later(built, rel)
        os.utime(self.root / rel, (new_mtime, new_mtime))  # a checkout that kept the content

        first = CodeIndex(str(self.root))
        first._load_index()
        with mock.patch.object(IndexStore, "apply") as apply:
            result = first.refresh_stale()
        apply.assert_not_called()
        self.assertEqual(result["files_unchanged"], 4)
        self.assertEqual(first._file_mtimes[rel], new_mtime)

        second = CodeIndex(str(self.root))
        second._load_index()
        self.assertEqual(second._file_mtimes[rel], new_mtime, "the new mtime reached the store")
        with mock.patch.object(CodeIndex, "_read_indexable", wraps=second._read_indexable) as spy:
            second.refresh_stale()
        self.assertEqual(spy.call_count, 0)

    def test_mtimes_are_not_trusted_while_mask_rules_exist(self):
        self.build()
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        with mock.patch.object(ag, "has_mask_rules", return_value=True), \
                mock.patch.object(CodeIndex, "_read_indexable", wraps=fresh._read_indexable) as spy:
            fresh.refresh_stale()
        self.assertEqual(spy.call_count, 4, "a mask rule change alters the view, not the mtime")

    def test_no_stored_index_means_nothing_to_check(self):
        idx = CodeIndex(str(self.root))
        with mock.patch.object(CodeIndex, "build_index") as build:
            self.assertIsNone(idx.refresh_stale())
        build.assert_not_called()


class TestSyncPending(_Project):
    def test_a_search_first_reindexes_what_the_watcher_reported(self):
        idx = self.build()
        self.write("src/routes.py", "def mount_routes(app):\n    return app\n")
        pending = mock.Mock(side_effect=[[str(self.root / "src" / "routes.py")], []])
        idx.pending_changes = pending
        self.assertEqual(self.files(idx.search("mount_routes", top_k=1, max_tokens=800)), ["src/routes.py"])
        self.assertNotIn("register_routes", idx._symbol_tail)
        idx.search("migrate_v2", top_k=1, max_tokens=800)
        self.assertEqual(pending.call_count, 2, "every search asks the watcher")

    def test_nothing_pending_is_a_no_op(self):
        idx = self.build()
        idx.pending_changes = mock.Mock(return_value=[])
        with mock.patch.object(CodeIndex, "refresh") as refresh:
            self.assertIsNone(idx.sync_pending())
        refresh.assert_not_called()

    def test_a_long_write_is_never_waited_for(self):
        idx = self.build()
        idx.pending_changes = mock.Mock(return_value=["src/auth.py"])
        idx._long_write = True  # a full build, or a refresh of many files
        self.assertIsNone(idx.sync_pending())
        idx.pending_changes.assert_not_called()

    def test_a_build_marks_itself_long_and_clears_the_mark(self):
        idx = CodeIndex(str(self.root))
        seen = []
        idx.build_index(on_progress=lambda *_a: seen.append(idx._long_write))
        self.assertTrue(seen and all(seen))
        self.assertFalse(idx._long_write)

    def test_many_pending_files_are_reindexed_behind_the_search(self):
        idx = self.build()
        n = CodeIndex._SYNC_INLINE_MAX + 5
        paths = [str(self.write(f"gen/mod_{i}.py", f"def generated_{i}():\n    return {i}\n")) for i in range(n)]
        idx.pending_changes = mock.Mock(side_effect=[paths, []])
        with mock.patch.object(threading, "Thread", wraps=threading.Thread) as spawn:
            self.assertIsNone(idx.sync_pending(), "the search does not wait for the bulk")
        self.assertEqual([c.kwargs.get("name") for c in spawn.call_args_list], ["c3-index-sync"])
        for t in threading.enumerate():
            if t.name == "c3-index-sync":
                t.join(30)
        self.assertEqual(len([d for d in self.doc_ids(idx) if d.startswith("gen/")]), n)
        self.assertFalse(idx._long_write)

    def test_a_refresh_of_many_files_is_marked_long_while_it_writes(self):
        idx = self.build()
        n = CodeIndex._SYNC_INLINE_MAX + 1
        for i in range(n):
            self.write(f"gen/mod_{i}.py", f"def generated_{i}():\n    return {i}\n")
        seen = []
        idx.refresh(on_progress=lambda *_a: seen.append(idx._long_write))
        self.assertTrue(seen and all(seen))
        self.assertFalse(idx._long_write, "restored when the refresh ends")
        seen.clear()
        self.write("src/auth.py", "def sha256_digest(data):\n    return 'v3'\n")
        idx.refresh(on_progress=lambda *_a: seen.append(idx._long_write))
        self.assertEqual(seen, [False], "a small refresh is waited for")

    def test_a_busy_writer_is_waited_for_only_briefly(self):
        idx = self.build()
        idx.pending_changes = mock.Mock(return_value=["src/auth.py"])
        held, release = threading.Event(), threading.Event()

        def writer():
            with idx._write_lock:
                held.set()
                release.wait(5)

        t = threading.Thread(target=writer)
        t.start()
        try:
            held.wait(5)
            self.assertIsNone(idx.sync_pending(wait_s=0.05))
        finally:
            release.set()
            t.join(5)
        idx.pending_changes.assert_not_called()  # the paths stay queued for the next search

    def test_a_wait_ends_when_the_write_turns_long(self):
        idx = self.build()
        idx.pending_changes = mock.Mock(return_value=["src/auth.py"])
        held, release = threading.Event(), threading.Event()

        def writer():
            with idx._write_lock:
                held.set()
                time.sleep(0.2)
                idx._long_write = True  # the startup check found a big pull
                release.wait(10)

        t = threading.Thread(target=writer)
        t.start()
        try:
            held.wait(5)
            t0 = time.monotonic()
            self.assertIsNone(idx.sync_pending(wait_s=8.0))
            waited = time.monotonic() - t0
        finally:
            release.set()
            t.join(5)
            idx._long_write = False
        self.assertLess(waited, 3.0, "the search stopped waiting once the write turned long")
        idx.pending_changes.assert_not_called()

    def test_without_a_watcher_nothing_is_synced(self):
        idx = self.build()
        self.assertIsNone(idx.pending_changes)
        self.assertIsNone(idx.sync_pending())


class TestRefreshSwapsInCopies(_Project):
    def test_a_running_search_keeps_the_dicts_it_started_with(self):
        idx = self.build()
        old_chunks, old_docs = idx.chunks, idx.documents
        before = dict(old_chunks)
        self.write("src/auth.py", "def sha256_digest(data):\n    return 'v2'\n")
        idx.refresh(paths=["src/auth.py"])
        self.assertIsNot(idx.chunks, old_chunks)
        self.assertIsNot(idx.documents, old_docs)
        self.assertEqual(old_chunks, before)
        self.assertTrue(any("v2" in c["content"] for c in idx.chunks.values()))

    def test_a_path_named_twice_is_ingested_once(self):
        idx = self.build()
        self.write("src/auth.py", "def sha256_digest(data):\n    return 'v2'\n\ndef hmac_sign(d):\n    return d\n")
        result = idx.refresh(paths=["src/auth.py", str(self.root / "src" / "auth.py")])
        self.assertEqual(result["files_changed"], 1)
        reloaded = CodeIndex(str(self.root))
        reloaded._load_index()
        self.assertEqual(set(idx.chunks), set(reloaded.chunks), "no orphan chunks in memory")

    def test_a_chunk_swapped_out_mid_search_is_skipped(self):
        idx = self.build()
        idx._lexical_ok = True
        real = next(cid for cid, c in idx.chunks.items() if c.get("name") == "sha256_digest")
        with mock.patch.object(CodeIndex, "_rank_lexical", return_value=[("gone::chunk", 2.0), (real, 1.0)]):
            hits = idx.search("sha256_digest", top_k=2, max_tokens=800)
        self.assertEqual([h["chunk_id"] for h in hits], [real])

    def test_results_from_before_a_swap_stay_out_of_the_new_cache(self):
        idx = self.build()
        rank = idx._rank_lexical

        def swap_then_rank(*args, **kwargs):
            idx._search_cache = OrderedDict()  # what a concurrent refresh does
            return rank(*args, **kwargs)

        idx._lexical_ok = True
        with mock.patch.object(CodeIndex, "_rank_lexical", side_effect=swap_then_rank):
            idx.search("sha256_digest", top_k=1, max_tokens=800)
        self.assertEqual(len(idx._search_cache), 0)


class TestWatcherIndexPaths(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name).resolve()
        self.h = _ChangeHandler(None, root=str(self.root))

    def tearDown(self):
        self._td.cleanup()

    def path(self, rel):
        return str(self.root / rel)

    def test_the_index_takes_its_paths_without_emptying_the_agents_list(self):
        self.h.on_modified(FileModifiedEvent(self.path("src/a.py")))
        self.h.on_modified(FileModifiedEvent(self.path("src/a.py")))
        self.assertEqual(self.h.take_index_pending(), [self.path("src/a.py")])
        self.assertEqual(self.h.take_index_pending(), [])
        self.assertEqual(self.h.change_count, 2, "change_count drives the staleness agents")

    def test_an_atomic_save_queues_the_destination(self):
        self.h.on_moved(FileMovedEvent(self.path("src/a.py.tmp"), self.path("src/a.py")))
        self.assertEqual(self.h.take_index_pending(), [self.path("src/a.py")])

    def test_structural_events_bump_the_generation(self):
        self.h.on_modified(FileModifiedEvent(self.path("src/a.py")))
        self.assertEqual(self.h.structure_generation, 0, "an edit does not change the file list")
        self.h.on_created(FileCreatedEvent(self.path("src/b.py")))
        self.h.on_deleted(FileDeletedEvent(self.path("notes.txt")))
        self.h.on_created(DirCreatedEvent(self.path("pkg")))
        self.assertEqual(self.h.structure_generation, 3)
        self.h.on_moved(FileMovedEvent(self.path("src/b.py"), self.path("src/c.py")))
        self.assertEqual(self.h.structure_generation, 5)

    def test_pruned_directories_do_not_bump_it(self):
        self.h.on_created(FileCreatedEvent(self.path(".git/objects/ab/cdef")))
        self.h.on_created(DirCreatedEvent(self.path("node_modules")))
        self.h.on_created(FileCreatedEvent(self.path(".c3/index/index.sqlite-wal")))
        self.assertEqual(self.h.structure_generation, 0)

    def test_code_watcher_exposes_both(self):
        w = CodeWatcher(str(self.root))
        self.assertFalse(w.running)
        w._handler.on_created(FileCreatedEvent(self.path("src/new.py")))
        self.assertEqual(w.structure_generation, 1)
        self.assertEqual(w.take_index_changes(), [self.path("src/new.py")])


class _FakeWatcher:
    def __init__(self, running=True, generation=0):
        self.running = running
        self.structure_generation = generation


class TestManifestCache(_Project):
    def setUp(self):
        super().setUp()
        search_tool._manifest_cache.clear()
        self.watcher = _FakeWatcher()
        self.svc = types.SimpleNamespace(project_path=str(self.root), indexer=None, watcher=self.watcher)
        self.walks = mock.patch.object(search_tool, "iter_files", wraps=search_tool.iter_files)
        self.spy = self.walks.start()

    def tearDown(self):
        self.walks.stop()
        search_tool._manifest_cache.clear()
        super().tearDown()

    def manifest(self):
        return sorted(search_tool._search_manifest(self.svc))

    def test_the_walk_is_reused_until_the_watcher_sees_a_structural_change(self):
        first = self.manifest()
        self.assertEqual(self.manifest(), first)
        self.assertEqual(self.spy.call_count, 1)
        self.write("src/new.py", "x = 1\n")
        self.watcher.structure_generation += 1
        self.assertIn("src/new.py", self.manifest())
        self.assertEqual(self.spy.call_count, 2)

    def test_no_running_watcher_means_a_walk_per_call(self):
        self.watcher.running = False
        self.manifest()
        self.manifest()
        self.assertEqual(self.spy.call_count, 2)
        self.svc.watcher = None
        self.manifest()
        self.assertEqual(self.spy.call_count, 3)

    def test_a_rule_change_walks_again(self):
        self.write("secrets/key.py", "TOKEN = 1\n")
        self.assertIn("secrets/key.py", self.manifest())
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"access": {"deny": ["secrets/**"]}}), encoding="utf-8")
        self.assertNotIn("secrets/key.py", self.manifest())
        self.assertEqual(self.spy.call_count, 2)

    def test_a_gitignore_change_walks_again(self):
        self.manifest()
        self.write(".gitignore", "docs/\n")
        self.assertNotIn("docs/guide.md", self.manifest())
        self.assertEqual(self.spy.call_count, 2)

    def test_the_ttl_bounds_a_missed_event(self):
        self.manifest()
        with mock.patch.object(search_tool, "_MANIFEST_TTL_S", 0.0):
            self.manifest()
        self.assertEqual(self.spy.call_count, 2)


class TestExactDirectReads(_Project):
    def setUp(self):
        super().setUp()
        self.write("secrets/key.py", "def sha256_digest_secret():\n    return 1\n")
        (self.root / ".c3" / "config.json").write_text(
            json.dumps({"access": {"deny": ["secrets/**"]}}), encoding="utf-8")
        self.svc = types.SimpleNamespace(project_path=str(self.root), indexer=None, hybrid_config={})

    def exact(self, query):
        def finalize(_tool, _args, resp, _tag, **_kw):
            return resp
        with mock.patch.object(search_tool, "_ripgrep_path", return_value=None):
            return search_tool._exact_search(query, 10, 2000, self.svc, finalize)

    def test_direct_reads_give_the_verdict_paths_answer(self):
        self.assertTrue(ag.deny_rules_decide_reads(str(self.root)))
        direct = self.exact("sha256_digest")
        with mock.patch.object(search_tool, "_deny_rules_only", return_value=False):
            verdicted = self.exact("sha256_digest")
        self.assertEqual(direct, verdicted)
        self.assertIn("src/auth.py", direct)
        self.assertNotIn("secrets/key.py", direct)

    def test_a_denied_file_is_never_opened(self):
        opened = []
        real = Path.read_text

        def spy(path, *args, **kwargs):
            opened.append(Path(path).as_posix())
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", spy):
            self.exact("sha256_digest")
        self.assertTrue(any(p.endswith("src/auth.py") for p in opened))
        self.assertFalse(any("/secrets/" in p for p in opened))

    def test_a_mask_rule_keeps_the_verdict_path(self):
        with mock.patch.object(ag, "deny_rules_decide_reads", return_value=False), \
                mock.patch("services.indexer._masked_content", return_value="masked view\n") as masked:
            out = self.exact("masked view")
        self.assertGreater(masked.call_count, 0)
        self.assertIn("masked view", out)


class TestDenyRulesDecideReads(unittest.TestCase):
    PROJECT = "."

    def decide(self, rules=(), masks=(), corrupt=()):
        with mock.patch.object(ag, "load_all", return_value=(list(rules), list(masks), list(corrupt))):
            return ag.deny_rules_decide_reads(self.PROJECT)

    def rule(self, kind, confirm_ops="write"):
        return types.SimpleNamespace(kind=kind, confirm_ops=confirm_ops)

    def test_deny_read_only_and_write_confirm_rules_decide_reads(self):
        self.assertTrue(self.decide())
        self.assertTrue(self.decide([self.rule("deny"), self.rule("read_only"), self.rule("confirm")]))

    def test_masks_corrupt_scopes_and_read_pausing_confirms_do_not(self):
        self.assertFalse(self.decide(masks=[object()]))
        self.assertFalse(self.decide(corrupt=["project"]))
        self.assertFalse(self.decide([self.rule("confirm", confirm_ops="all")]))

    def test_unreadable_rules_fail_closed(self):
        with mock.patch.object(ag, "load_all", side_effect=ValueError("bad path")):
            self.assertFalse(ag.deny_rules_decide_reads(self.PROJECT))


class TestSpecialTokenText(_Project):
    # Built at runtime: a released c3 whose token counter raises on the
    # literal would fail to index or read this file.
    MARKER = "<|" + "endoftext" + "|>"

    def test_count_tokens_treats_it_as_text(self):
        from core import count_tokens
        self.assertGreater(count_tokens(self.MARKER), 0)
        self.assertGreater(count_tokens("x = 1\n" * 10 + self.MARKER), 0)  # the cached path

    def test_a_file_quoting_it_is_built_and_refreshed(self):
        self.write("src/tokens.py", f"STOP = {self.MARKER!r}\n")
        idx = self.build()
        self.assertIn("src/tokens.py", self.doc_ids(idx))
        self.write("src/more.py", f"END = {self.MARKER!r}\n")
        self.assertEqual(idx.refresh_stale()["files_added"], 1)


if __name__ == "__main__":
    unittest.main()
