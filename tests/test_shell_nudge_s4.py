"""S4 of the c3_shell remediation: an advisory hint when the shell is used as
a reader or a searcher over project files (48% of commands, measured
2026-09-04). One line, never a refusal, never a rewrite.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools.shell_nudge import bypass_hint  # noqa: E402

PROJ = str(Path(__file__).resolve().parent.parent)


def hint(cmd: str) -> str | None:
    return bypass_hint(cmd, PROJ)


class TestReads(unittest.TestCase):
    def test_cat_of_a_project_file(self):
        h = hint("cat CHANGELOG.md")
        self.assertIsNotNone(h)
        self.assertTrue(h.startswith("[c3_shell:hint]"))
        self.assertIn("c3_read(file_path='CHANGELOG.md')", h)

    def test_cd_prefix_and_head_n(self):
        h = hint('cd "U:/x y" && head -n 40 services/indexer.py')
        self.assertIn("c3_read(file_path='services/indexer.py', lines=[1,40])", h)
        h = hint("head -60 cli/c3.py")
        self.assertIn("lines=[1,60]", h)

    def test_tail_points_at_map_then_read(self):
        h = hint("tail -n 30 .c3/hub.log")
        self.assertIsNone(h)  # .c3 is skipped: not a source read
        h = hint("tail -n 30 docs/shell-eval.md")
        self.assertIn("c3_compress(file_path='docs/shell-eval.md', mode='map')", h)

    def test_sed_range(self):
        h = hint("sed -n '120,180p' cli/tools/shell.py")
        self.assertIn("c3_read(file_path='cli/tools/shell.py', lines=[120,180])", h)
        self.assertIsNone(hint("sed -i 's/a/b/' cli/tools/shell.py"))  # an edit, not a read

    def test_two_files_or_outside_project_is_not_hinted(self):
        self.assertIsNone(hint("cat a.py b.py"))
        self.assertIsNone(hint("cat /etc/hosts"))
        self.assertIsNone(hint("cat ~/.c3/config.json"))
        self.assertIsNone(hint("cat $HOME/x"))
        self.assertIsNone(hint("cat node_modules/left-pad/index.js"))


class TestSearches(unittest.TestCase):
    def test_grep_identifier_maps_to_code_search(self):
        h = hint("grep -rn CodeIndex services/")
        self.assertIn("c3_search(action='code', query='CodeIndex', path='services')", h)

    def test_grep_regex_maps_to_exact_with_ignore_case(self):
        h = hint('grep -rni "content-security-policy\\|unsafe" --include=*.js .')
        self.assertIn("action='exact'", h)
        self.assertIn("ignore_case=True", h)
        self.assertNotIn("path=", h)

    def test_rg_e_pattern_and_pipe(self):
        h = hint("rg -e 'def handle_' cli | head -20")
        self.assertIn("query='def handle_'", h)
        self.assertIn("path='cli'", h)

    def test_grep_outside_project_is_not_hinted(self):
        self.assertIsNone(hint("grep -rn foo /var/log/syslog"))
        self.assertIsNone(hint("grep -rn foo C:/Windows/Temp"))

    def test_grep_without_pattern(self):
        self.assertIsNone(hint("grep"))


class TestFinds(unittest.TestCase):
    def test_find_name(self):
        h = hint("find . -name '*.yml' -not -path '*/node_modules/*'")
        self.assertIn("c3_search(action='files', query='*.yml')", h)

    def test_find_without_name_and_plain_ls(self):
        self.assertIsNone(hint("find . -type d -mtime -1"))
        self.assertIsNone(hint("ls -la"))
        self.assertIsNotNone(hint("ls -R services"))


class TestWiring(unittest.TestCase):
    """render_shell_response appends the one line and telemetry records its kind."""

    def _render(self, cmd):
        from types import SimpleNamespace

        from cli.tools import shell as shell_mod
        from services.output_filter import OutputFilter
        svc = SimpleNamespace(project_path=PROJ, activity_log=None, edit_ledger=None,
                              output_filter=OutputFilter({"HYBRID_DISABLE_TIER1": True}),
                              session_mgr=None, hybrid_config={})
        result = {"exit_code": 0, "stdout": "x = 1\n", "stderr": "", "duration_ms": 3,
                  "timed_out": False, "shell": "git-bash"}
        return shell_mod.render_shell_response(cmd, result, svc)

    def test_read_gets_one_line_and_a_telemetry_kind(self):
        body, stats = self._render("cat cli/c3.py")
        self.assertEqual(body.count("[c3_shell:hint]"), 1)
        self.assertIn("c3_read(file_path='cli/c3.py')", body)
        self.assertEqual(stats["hint"], "a read of cli/c3.py")
        self.assertTrue(body.startswith("[c3_shell:OK] 3ms\n$ cat cli/c3.py\n--- stdout ---\nx = 1\n"))

    def test_other_commands_are_untouched(self):
        body, stats = self._render("echo hi")
        self.assertNotIn("[c3_shell:hint]", body)
        self.assertIsNone(stats["hint"])


class TestNotReads(unittest.TestCase):
    def test_everything_else_is_silent(self):
        for cmd in ("git status", "python -m pytest tests -q", "npm run build", "echo hi",
                    "curl -s http://127.0.0.1:3330/api/health", "", "python - <<'EOF'\nprint(1)\nEOF",
                    "PYTHONPATH= c3 --version", "gh pr list"):
            self.assertIsNone(hint(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
