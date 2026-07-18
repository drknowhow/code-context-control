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
