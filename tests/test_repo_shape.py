"""services.repo_shape — what kind of project is this, and what does that imply.

Field report 2026-08-22 (OBSERVATION-1): 636 files, ~95% Markdown + .docx,
near-zero source. Every symbol-aware C3 tool had nothing to act on while
strict discipline was paid on every turn. ``c3 init`` should recognise that
shape and default it to ``advisory`` — without ever overriding a choice
the user made.
"""
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import enforcement_policy as ep  # noqa: E402
from services import repo_shape  # noqa: E402


class _Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def touch(self, rel, n=1, content="x"):
        for i in range(n):
            p = self.root / rel.format(i=i)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")


class TestClassify(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(repo_shape.classify("a/b.py"), "source")
        self.assertEqual(repo_shape.classify("a/b.TS"), "source")
        self.assertEqual(repo_shape.classify("doc/x.md"), "prose")
        self.assertEqual(repo_shape.classify("doc/x.docx"), "prose")
        self.assertEqual(repo_shape.classify("cfg.json"), "other")
        self.assertEqual(repo_shape.classify("logo.png"), "other")
        self.assertEqual(repo_shape.classify("LICENSE"), "other")

    def test_markdown_and_config_are_not_source(self):
        # The indexer indexes .md/.json/.yaml; the shape question is what the
        # symbol tools can act on, and that is not Markdown.
        self.assertNotIn(".md", repo_shape.SOURCE_EXTS)
        self.assertNotIn(".json", repo_shape.SOURCE_EXTS)
        self.assertNotIn(".yaml", repo_shape.SOURCE_EXTS)


class TestKind(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(repo_shape.kind_for(0, 10), repo_shape.KIND_EMPTY)
        self.assertEqual(repo_shape.kind_for(2, 30), repo_shape.KIND_PROSE)
        self.assertEqual(repo_shape.kind_for(3, 30), repo_shape.KIND_PROSE)   # 9%
        self.assertEqual(repo_shape.kind_for(10, 20), repo_shape.KIND_MIXED)  # 33%
        self.assertEqual(repo_shape.kind_for(30, 5), repo_shape.KIND_CODE)
        self.assertEqual(repo_shape.kind_for(25, 0), repo_shape.KIND_CODE)


class TestAssess(_Tree):
    def test_documentation_repo_is_prose(self):
        self.touch("docs/page{i}.md", 30)
        self.touch("drafts/brief{i}.docx", 5)
        self.touch("tools/one.py")
        self.touch("config{i}.json", 40)  # config does not tip the ratio
        shape = repo_shape.assess(self.root)
        self.assertEqual(shape.kind, repo_shape.KIND_PROSE)
        self.assertEqual((shape.source, shape.prose, shape.other), (1, 35, 40))
        self.assertEqual(shape.total, 76)
        self.assertEqual(repo_shape.recommended_mode(shape), "advisory")
        self.assertIn("prose", shape.describe())

    def test_codebase_is_code(self):
        self.touch("src/mod{i}.py", 30)
        self.touch("docs/page{i}.md", 5)
        shape = repo_shape.assess(self.root)
        self.assertEqual(shape.kind, repo_shape.KIND_CODE)
        self.assertIsNone(repo_shape.recommended_mode(shape))

    def test_mixed_repo_gets_no_opinion(self):
        self.touch("src/mod{i}.py", 10)
        self.touch("docs/page{i}.md", 20)
        shape = repo_shape.assess(self.root)
        self.assertEqual(shape.kind, repo_shape.KIND_MIXED)
        self.assertIsNone(repo_shape.recommended_mode(shape))

    def test_tiny_repo_is_too_small_to_judge(self):
        self.touch("README.md")
        self.touch("notes{i}.md", 4)
        shape = repo_shape.assess(self.root)
        self.assertEqual(shape.kind, repo_shape.KIND_EMPTY)
        self.assertIn("too few", shape.describe())

    def test_index_and_dependency_dirs_are_not_counted(self):
        self.touch("docs/page{i}.md", 30)
        self.touch(".c3/cache/blob{i}.py", 50)          # C3's own artefacts
        self.touch("node_modules/dep/lib{i}.js", 50)  # dependencies
        self.touch(".git/objects/ab/cd{i}", 50)
        shape = repo_shape.assess(self.root)
        self.assertEqual(shape.kind, repo_shape.KIND_PROSE)
        self.assertEqual(shape.source, 0)

    def test_cap_is_reported(self):
        self.touch("docs/page{i}.md", 40)
        shape = repo_shape.assess(self.root, max_files=25)
        self.assertTrue(shape.capped)
        self.assertEqual(shape.total, 25)
        self.assertIn("first 25", shape.describe())


class TestShapeProvenance(_Tree):
    """``set_by: repo-shape`` — a default with a reason, never a veto."""

    def setUp(self):
        super().setUp()
        (self.root / ".c3").mkdir()
        (self.root / ".c3" / "config.json").write_text("{}", encoding="utf-8")
        self.proj = str(self.root)

    def test_shape_default_is_accepted_and_shown(self):
        ep.set_mode(ep.MODE_ADVISORY, self.proj, set_by=ep.SET_BY_SHAPE)
        policy = ep.resolve(self.proj)
        self.assertEqual(policy.mode, ep.MODE_ADVISORY)
        self.assertEqual(policy.set_by, ep.SET_BY_SHAPE)
        self.assertIn("repo-shape", policy.describe())
        self.assertEqual(policy.warnings, ())

    def test_shape_default_defers_to_a_user_choice(self):
        ep.set_mode(ep.MODE_STRICT, self.proj, set_by=ep.SET_BY_USER)
        res = ep.set_mode(ep.MODE_ADVISORY, self.proj, set_by=ep.SET_BY_SHAPE)
        self.assertTrue(res["deferred"])
        self.assertEqual(ep.resolve(self.proj).mode, ep.MODE_STRICT)

    def test_shape_default_overrides_a_tier_default(self):
        ep.set_mode(ep.MODE_STRICT, self.proj, set_by=ep.SET_BY_TIER)
        res = ep.set_mode(ep.MODE_ADVISORY, self.proj, set_by=ep.SET_BY_SHAPE)
        self.assertFalse(res["deferred"])
        self.assertEqual(ep.resolve(self.proj).mode, ep.MODE_ADVISORY)

    def test_a_later_tier_choice_overrides_the_shape_default(self):
        ep.set_mode(ep.MODE_ADVISORY, self.proj, set_by=ep.SET_BY_SHAPE)
        ep.set_mode(ep.MODE_STRICT, self.proj, set_by=ep.SET_BY_TIER)
        self.assertEqual(ep.resolve(self.proj).mode, ep.MODE_STRICT)


class TestInitAppliesShapeDefault(_Tree):
    """``cli.c3._apply_repo_shape_default``: the decision, end to end."""

    def setUp(self):
        super().setUp()
        (self.root / ".c3").mkdir()
        (self.root / ".c3" / "config.json").write_text("{}", encoding="utf-8")
        self.proj = str(self.root)
        from cli.c3 import _apply_repo_shape_default
        self.apply = _apply_repo_shape_default

    def _run(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.apply(self.proj, **kw)
        return buf.getvalue()

    def test_prose_repo_defaults_to_advisory_and_says_why(self):
        self.touch("docs/page{i}.md", 30)
        out = self._run()
        policy = ep.resolve(self.proj)
        self.assertEqual((policy.mode, policy.set_by), (ep.MODE_ADVISORY, ep.SET_BY_SHAPE))
        self.assertIn("Repo shape", out)
        self.assertIn("advisory", out)
        self.assertIn("c3 enforce strict", out)

    def test_code_repo_is_left_at_the_default(self):
        self.touch("src/mod{i}.py", 30)
        out = self._run()
        self.assertEqual(ep.resolve(self.proj).mode, ep.MODE_STRICT)
        self.assertIn("Repo shape", out)
        self.assertNotIn("c3 enforce strict", out)

    def test_explicit_user_choice_wins(self):
        self.touch("docs/page{i}.md", 30)
        ep.set_mode(ep.MODE_STRICT, self.proj, set_by=ep.SET_BY_USER)
        out = self._run()
        self.assertEqual(ep.resolve(self.proj).mode, ep.MODE_STRICT)
        self.assertIn("your choice", out)

    def test_explicit_flag_wins(self):
        self.touch("docs/page{i}.md", 30)
        out = self._run(explicit_enforcement="strict")
        self.assertEqual(ep.resolve(self.proj).mode, ep.MODE_STRICT)
        self.assertIn("--enforcement", out)

    def test_tiny_repo_says_nothing(self):
        self.touch("README.md")
        self.assertEqual(self._run().strip(), "")


if __name__ == "__main__":
    unittest.main()
