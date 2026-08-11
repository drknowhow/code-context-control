"""
Tests for the c3 init large-project fixes: pruned filesystem scanner,
index cap honesty, EmbeddingIndex.probe() (lazy-init ready gate), and
compression-dictionary reuse of the in-memory code index.
"""
import json
import tempfile
import unittest
from pathlib import Path

from services.scanner import gitignore_dir_names, iter_files


class _Tree(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def touch(self, rel, content="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p


class TestNestedCheckoutPruning(_Tree):
    """A linked git worktree is another checkout, not more of this project.

    SKIP_DIRS matches directory NAMES, and a worktree is named whatever
    somebody called it. Its only marker is a ``.git`` FILE (``gitdir: …``)
    rather than a ``.git`` directory — so pruning the name '.git' skips a
    file that was never a directory, and the walk descends into a whole
    second copy of the repository.

    Measured on the Yep project, 2026-08-11: 112 worktrees, 88,015 of the
    index's 107,729 tracked files (81.7%) were worktree copies, and a
    cross-project exact search took 147.6s against a 120s transport ceiling
    — unreachable, not merely slow.
    """

    def _worktree(self, rel, gitdir="/somewhere/.git/worktrees/wt1"):
        """A directory carrying the linked-worktree marker."""
        (self.root / rel).mkdir(parents=True, exist_ok=True)
        (self.root / rel / ".git").write_text(
            f"gitdir: {gitdir}\n", encoding="utf-8")

    def test_a_linked_worktree_is_not_walked(self):
        self.touch("src/real.py")
        self._worktree(".claude/worktrees/wt1")
        self.touch(".claude/worktrees/wt1/src/copy.py")
        found = {p.name for p in iter_files(self.root)}
        self.assertIn("real.py", found)
        self.assertNotIn("copy.py", found)

    def test_a_worktree_at_any_depth_or_name_is_pruned(self):
        """The name carries no signal — only the marker does."""
        self.touch("src/real.py")
        for rel in (".wt-feature", "vendor/thing", "deep/a/b/c/scratch"):
            self._worktree(rel)
            self.touch(f"{rel}/src/copy.py")
        found = [p for p in iter_files(self.root)]
        self.assertEqual({p.name for p in found} & {"copy.py"}, set())
        self.assertIn("real.py", {p.name for p in found})

    def test_a_submodule_style_git_dir_is_also_pruned(self):
        """A nested checkout with a real .git DIRECTORY is equally not ours."""
        self.touch("src/real.py")
        (self.root / "vendor/lib/.git").mkdir(parents=True, exist_ok=True)
        self.touch("vendor/lib/src/copy.py")
        found = {p.name for p in iter_files(self.root)}
        self.assertNotIn("copy.py", found)
        self.assertIn("real.py", found)

    def test_the_project_root_is_never_self_pruned(self):
        """The root's own .git must not make the project prune itself — only
        CHILD directories are tested for the marker."""
        (self.root / ".git").mkdir(parents=True, exist_ok=True)
        self.touch("src/real.py")
        self.touch("top.py")
        found = {p.name for p in iter_files(self.root)}
        self.assertIn("real.py", found)
        self.assertIn("top.py", found)

    def test_an_ordinary_directory_is_untouched(self):
        """The cost of the new stat is a prune only when the marker is there."""
        self.touch("src/real.py")
        self.touch("docs/guide.md")
        self.touch("tests/test_x.py")
        found = {p.name for p in iter_files(self.root)}
        self.assertEqual(found, {"real.py", "guide.md", "test_x.py"})

    def test_the_predicate_is_directly_testable(self):
        from services.scanner import is_nested_checkout

        self._worktree("wt")
        self.assertTrue(is_nested_checkout(self.root / "wt"))
        (self.root / "plain").mkdir()
        self.assertFalse(is_nested_checkout(self.root / "plain"))
        self.assertFalse(is_nested_checkout(self.root / "does-not-exist"))


class TestScannerPruning(_Tree):
    def test_skip_dirs_pruned(self):
        self.touch("src/a.py")
        self.touch("node_modules/dep/b.js")
        self.touch(".git/objects/ab/cdef")
        found = {p.name for p in iter_files(self.root)}
        self.assertIn("a.py", found)
        self.assertNotIn("b.js", found)
        self.assertNotIn("cdef", found)

    def test_pruned_dirs_never_descended(self):
        for i in range(50):
            self.touch(f"node_modules/dep{i}/mod.js")
        self.touch("src/a.py")
        self.touch("b.py")
        last = {}
        list(iter_files(self.root,
                        on_progress=lambda seen, y: last.update(seen=seen)))
        # If node_modules were descended, entries would exceed 100.
        self.assertLess(last["seen"], 10)

    def test_ext_filter(self):
        self.touch("a.py")
        self.touch("b.js")
        self.touch("c.txt")
        found = {p.name for p in iter_files(self.root, exts={".py", ".js"})}
        self.assertEqual(found, {"a.py", "b.js"})

    def test_max_files_early_exit(self):
        for i in range(9):
            self.touch(f"f{i}.py")
        found = list(iter_files(self.root, exts={".py"}, max_files=3))
        self.assertEqual(len(found), 3)

    def test_deterministic_order(self):
        for name in ("zeta.py", "alpha.py", "mid/beta.py"):
            self.touch(name)
        one = list(iter_files(self.root, exts={".py"}))
        two = list(iter_files(self.root, exts={".py"}))
        self.assertEqual(one, two)

    def test_exclude_parts_prunes_directories(self):
        self.touch("api/child.py")
        self.touch("main.py")

        def excl(parts):
            return bool(parts) and parts[0].lower() == "api"

        found = {p.name for p in iter_files(self.root, exts={".py"},
                                            exclude_parts=excl)}
        self.assertEqual(found, {"main.py"})


class TestGitignorePruning(_Tree):
    def test_literal_names_only(self):
        self.touch(".gitignore",
                   "logs/\n*.tmp\ndata\n!keep\nnested/deep\n# comment\n")
        self.assertEqual(gitignore_dir_names(self.root), {"logs", "data"})

    def test_glob_patterns_extracted(self):
        from services.scanner import gitignore_dir_patterns
        self.touch(".gitignore",
                   "*.egg-info/\nlogs/\n!never.egg-info\nsub/*.egg-info\n")
        names, patterns = gitignore_dir_patterns(self.root)
        self.assertEqual(names, {"logs"})
        self.assertEqual(patterns, ["*.egg-info"])

    def test_glob_dirs_pruned_in_iter_files(self):
        self.touch(".gitignore", "*.egg-info/\n")
        self.touch("pkg.egg-info/SOURCES.txt")
        self.touch("src/a.py")
        found = {p.name for p in iter_files(self.root)}
        self.assertIn("a.py", found)
        self.assertNotIn("SOURCES.txt", found)

    def test_make_dir_pruner(self):
        from services.scanner import make_dir_pruner
        self.touch(".gitignore", "*.egg-info/\nlogs/\n")
        pruned = make_dir_pruner(self.root, extra_skip=(".claude",))
        for name in ("node_modules", ".pytest_cache", ".ruff_cache",
                     "logs", "pkg.egg-info", ".claude"):
            self.assertTrue(pruned(name), name)
        for name in ("src", "docs", ".github"):
            self.assertFalse(pruned(name), name)

    def test_gitignored_dirs_pruned(self):
        self.touch(".gitignore", "logs/\n")
        self.touch("logs/big.md")
        self.touch("docs/keep.md")
        found = {p.name for p in iter_files(self.root, exts={".md"})}
        self.assertEqual(found, {"keep.md"})
        found_all = {p.name for p in iter_files(self.root, exts={".md"},
                                                respect_gitignore=False)}
        self.assertEqual(found_all, {"keep.md", "big.md"})


class TestIndexCapHonesty(_Tree):
    def test_capped_run_reports_leftovers(self):
        from services.indexer import CodeIndex
        for i in range(5):
            self.touch(f"m{i}.py", f"def f{i}():\n    return {i}\n")
        res = CodeIndex(str(self.root)).build_index(max_files=2)
        self.assertEqual(res["files_indexed"], 2)
        self.assertEqual(res["files_capped"], 3)
        self.assertEqual(res["max_files"], 2)

    def test_config_cap_used_when_unset(self):
        from services.indexer import CodeIndex
        self.touch("a.py", "def f():\n    pass\n")
        cfg = self.root / ".c3" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"index_max_files": 7}), encoding="utf-8")
        res = CodeIndex(str(self.root)).build_index()
        self.assertEqual(res["max_files"], 7)
        self.assertEqual(res["files_capped"], 0)

    def test_progress_callback_fires(self):
        from services.indexer import CodeIndex
        self.touch("a.py", "def f():\n    pass\n")
        calls = []
        CodeIndex(str(self.root)).build_index(
            on_progress=lambda entries, files, chunks:
                calls.append((entries, files, chunks)))
        self.assertTrue(calls)
        self.assertEqual(calls[-1][1], 1)


class _StubOllama:
    def is_available(self, timeout=2):
        return False

    def has_model(self, name):
        return False


class TestEmbeddingProbe(_Tree):
    def _stub_ei(self):
        from services.embedding_index import EmbeddingIndex
        return EmbeddingIndex(str(self.root), _StubOllama())

    def test_ready_false_on_fresh_instance(self):
        # Unchanged contract: status reporters never trigger init.
        ei = self._stub_ei()
        self.assertFalse(ei.ready)
        self.assertFalse(ei._initialized)

    def test_probe_initializes_once_and_reports(self):
        ei = self._stub_ei()
        calls = []

        def fake_init():
            calls.append("init")
            ei._available = True
            ei._ollama_up = True
            ei._model_ok = True
            ei._ollama_ok = True

        ei._init_backends = fake_init
        ei._load_hashes = lambda: None
        status = ei.probe()
        self.assertTrue(status["ready"])
        self.assertTrue(ei.ready)
        ei.probe()
        self.assertEqual(calls, ["init"])

    def test_unavailable_reason_precision(self):
        ei = self._stub_ei()
        ei._initialized = True
        ei._available = False
        self.assertIn("chromadb", ei.unavailable_reason())
        ei._available = True
        ei._ollama_up = False
        self.assertIn("Ollama", ei.unavailable_reason())
        ei._ollama_up = True
        ei._model_ok = False
        self.assertIn("not pulled", ei.unavailable_reason())


class TestDocTreePruning(_Tree):
    """Issue #1: generated instruction-doc trees must exclude gitignored
    build/cache dirs and reflect deletions on regeneration."""

    def _tree(self):
        from services.session_manager import SessionManager
        return SessionManager(str(self.root))._scan_project_structure()

    def test_junk_dirs_excluded_from_tree(self):
        self.touch(".gitignore", "*.egg-info/\n.vscode/\n")
        self.touch("src/app.py")
        self.touch(".pytest_cache/CACHEDIR.TAG")
        self.touch(".ruff_cache/0.15/x")
        self.touch(".vscode/settings.json")
        self.touch("pkg.egg-info/SOURCES.txt")
        tree = self._tree()
        self.assertIn("src/", tree)
        for junk in (".pytest_cache", ".ruff_cache", ".vscode", "pkg.egg-info"):
            self.assertNotIn(junk, tree)

    def test_deleted_dir_disappears_on_regen(self):
        import shutil
        self.touch("src/app.py")
        self.touch("scratch/tmp.py")
        self.assertIn("scratch/", self._tree())
        shutil.rmtree(self.root / "scratch")
        self.assertNotIn("scratch/", self._tree())


class TestDictionaryReuse(_Tree):
    def test_uses_code_index_chunks_without_disk_scan(self):
        from services.protocol import CompressionProtocol

        class _CI:
            chunks = {"c1": {"doc_id": "a.py",
                             "content": " ".join(["identifier_alpha"] * 6)}}

        terms = CompressionProtocol(str(self.root)).build_project_dictionary(
            code_index=_CI())
        self.assertIn("identifier_alpha", terms)


if __name__ == "__main__":
    unittest.main()
