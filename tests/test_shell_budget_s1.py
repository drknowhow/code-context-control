"""S1 of the c3_shell remediation: the response has a byte budget and what it
drops is recoverable by id.

Measured 2026-09-04: 21 c3_shell calls a month returned more than 25k tokens
(the record: a 1.48M-token grep hit on a minified bundle); the client
discards any MCP result over MAX_MCP_OUTPUT_TOKENS, so every one came back
as an error and was re-run. These tests pin:

- the budget arithmetic (defaults, only-lower overrides, the client bound);
- the envelope (a heredoc is not echoed back);
- line clipping as two lines, centred on a grep match when there is one;
- head/tail shaping with an omission note that names the output id;
- the live path: a 3 MB single-line output comes back under 22 KiB with an
  output id, the raw bytes page back by id, and the same id is refused from
  another project, another session, or under a rule that now denies it;
- ``filter_output=False`` never lifts the cap; a filtered small output is
  spilled too, because the filter is lossy.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools import _grants  # noqa: E402
from cli.tools import shell as shell_mod  # noqa: E402
from cli.tools import shell_render as sr  # noqa: E402
from services import access_guard  # noqa: E402
from services.output_filter import OutputFilter  # noqa: E402

PY = sys.executable


def _result(stdout="", stderr="", exit_code=0, timed_out=False, duration_ms=7):
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "duration_ms": duration_ms, "timed_out": timed_out, "shell": "git-bash"}


def _svc(tmp: str, **extra):
    base = dict(project_path=tmp, activity_log=None, edit_ledger=None,
                output_filter=OutputFilter({"HYBRID_DISABLE_TIER1": True}),
                session_mgr=None, hybrid_config={})
    base.update(extra)
    return SimpleNamespace(**base)


def _finalize(name, args, resp, summ, **kw):
    return resp


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


class TestBudget(unittest.TestCase):
    def test_defaults_and_only_lower(self):
        env: dict = {}
        self.assertEqual(sr.effective_budget(env=env), sr.BUDGET_DEFAULT)
        self.assertEqual(sr.effective_budget(4096, env=env), 4096)
        self.assertEqual(sr.effective_budget(999_999, env=env), sr.BUDGET_DEFAULT)
        self.assertEqual(sr.effective_budget(config_default=8192, env=env), 8192)
        self.assertEqual(sr.effective_budget(16000, config_default=8192, env=env), 8192)
        self.assertEqual(sr.effective_budget(1, env=env), sr.BUDGET_FLOOR)
        self.assertEqual(sr.effective_budget("garbage", env=env), sr.BUDGET_DEFAULT)

    def test_client_token_limit_bounds_the_budget(self):
        self.assertEqual(sr.effective_budget(env={"MAX_MCP_OUTPUT_TOKENS": "2000"}), 6000)
        self.assertEqual(sr.effective_budget(env={"MAX_MCP_OUTPUT_TOKENS": "25000"}), sr.BUDGET_DEFAULT)
        self.assertEqual(sr.effective_budget(env={"MAX_MCP_OUTPUT_TOKENS": "x"}), sr.BUDGET_DEFAULT)

    def test_allocation_guarantees_stderr_and_redistributes(self):
        body = sr.BUDGET_DEFAULT - sr.META_RESERVE
        self.assertEqual(sr.allocate(sr.BUDGET_DEFAULT, 100_000, 0), (body, 0))
        self.assertEqual(sr.allocate(sr.BUDGET_DEFAULT, 0, 100_000), (0, body))
        out, err = sr.allocate(sr.BUDGET_DEFAULT, 100_000, 100_000)
        self.assertEqual(err, int(body * sr.STDERR_SHARE))
        self.assertEqual(out + err, body)
        # A small stderr takes what it needs; stdout gets the rest.
        out, err = sr.allocate(sr.BUDGET_DEFAULT, 100_000, 300)
        self.assertEqual(err, 300)
        self.assertEqual(out, body - 300)
        # A small stdout leaves its spare to stderr.
        out, err = sr.allocate(sr.BUDGET_DEFAULT, 500, 100_000)
        self.assertEqual(out, 500)
        self.assertEqual(err, body - 500)


class TestEnvelope(unittest.TestCase):
    def test_one_liner_is_echoed_verbatim(self):
        self.assertEqual(sr.cmd_display("echo hi"), "echo hi")

    def test_heredoc_is_summarised_not_echoed(self):
        cmd = "python - <<'EOF'\nimport json\nprint(json.dumps({'secret_body': 1}))\nEOF"
        shown = sr.cmd_display(cmd)
        self.assertTrue(shown.startswith("python - <<'EOF' (4 lines, "), shown)
        self.assertIn("sha256:", shown)
        self.assertNotIn("secret_body", shown)

    def test_long_single_line_is_capped(self):
        shown = sr.cmd_display("echo " + "x" * 500)
        self.assertLessEqual(len(shown.split(" (")[0]), sr.CMD_DISPLAY_MAX)
        self.assertIn("(1 lines, 505 chars, sha256:", shown)

    def test_grep_pattern(self):
        self.assertEqual(sr.grep_pattern("grep -n needle dist/bundle.js"), "needle")
        self.assertEqual(sr.grep_pattern('cd "U:/x y" && grep -rn "two words" src/'), "two words")
        self.assertEqual(sr.grep_pattern("rg -e 'pat|tern' --type py ."), "pat|tern")
        self.assertEqual(sr.grep_pattern("grep --include=*.js -rn foo ."), "foo")
        self.assertIsNone(sr.grep_pattern("cat big.json"))
        self.assertIsNone(sr.grep_pattern("grep"))


class TestClipAndShape(unittest.TestCase):
    def test_short_line_passes_through(self):
        self.assertEqual(sr.clip_line("hello"), ["hello"])

    def test_long_line_becomes_note_plus_fragment(self):
        line = "a" * 1000 + "b" * 1000
        note, frag = sr.clip_line(line, lineno=7, output_id="o-0123456789ab")
        self.assertTrue(note.startswith("[L7 clipped: 2000 chars"))
        self.assertIn("o-0123456789ab", note)
        for ch in '{}"|':
            self.assertNotIn(ch, note)
        self.assertEqual(frag, "a" * sr.CLIP_PREFIX + "…" + "b" * sr.CLIP_SUFFIX)

    def test_clip_centres_on_the_match(self):
        line = "x" * 5000 + '{"correlation_id":"corr-9f1e"}' + "y" * 5000
        note, frag = sr.clip_line(line, lineno=3, focus="corr-9f1e")
        self.assertIn("around the match", note)
        self.assertIn('"correlation_id":"corr-9f1e"', frag)
        self.assertLessEqual(len(frag), sr.CLIP_PREFIX + sr.CLIP_SUFFIX + 2)

    def test_under_budget_passes_through_untouched(self):
        text = "\n".join("line %d" % i for i in range(40)) + "\n"
        rendered, info = sr.shape_stream(full_text=text, total_bytes=_nbytes(text),
                                         total_lines=40, alloc=4096)
        self.assertEqual(rendered, text)
        self.assertFalse(info["cut"])

    def test_over_budget_full_text_is_windowed_with_a_note(self):
        lines = ["line %05d %s" % (i, "z" * 80) for i in range(2000)]
        text = "\n".join(lines) + "\n"
        rendered, info = sr.shape_stream(full_text=text, total_bytes=_nbytes(text),
                                         total_lines=2000, alloc=4096,
                                         output_id="o-0123456789ab")
        self.assertTrue(info["cut"])
        self.assertLessEqual(_nbytes(rendered), 4096 + 700)
        self.assertIn("line 00000", rendered)
        self.assertIn("line 01999", rendered)
        self.assertIn("lines / ", rendered)
        self.assertIn("omitted; full output via c3_shell(output_id='o-0123456789ab'", rendered)
        self.assertEqual(info["omitted_lines"], 2000 - (rendered.count("\nline ") + 1))

    def test_previews_only_path(self):
        head = "\n".join("head %d" % i for i in range(300)) + "\n"
        tail = "tial-partial\n" + "\n".join("tail %d" % i for i in range(300)) + "\n"
        rendered, info = sr.shape_stream(head=head, tail=tail, total_bytes=5_000_000,
                                         total_lines=100_000, alloc=3000)
        self.assertTrue(info["cut"])
        self.assertIn("head 0", rendered)
        self.assertIn("tail 299", rendered)
        self.assertNotIn("tial-partial", rendered)   # the torn first tail line is dropped
        self.assertIn("not spilled", rendered)
        self.assertLessEqual(_nbytes(rendered), 3000 + 700)


class TestRendererBudget(unittest.TestCase):
    def test_single_line_monster_is_capped_and_marked_for_spill(self):
        monster = "x" * 1_500_000 + "__needle__" + "y" * 1_500_000 + "\n"
        body, stats = shell_mod.render_shell_response(
            "grep -n __needle__ dist/bundle.js", _result(stdout=monster), _svc("."))
        self.assertLessEqual(_nbytes(body), sr.BUDGET_DEFAULT)
        self.assertIn("__needle__", body)
        self.assertIn("clipped", body)
        self.assertTrue(stats["needs_spill"])
        self.assertFalse(stats["spilled"])          # no capture: nothing to keep
        self.assertIn("not spilled", body)
        self.assertEqual(stats["budget_bytes"], sr.BUDGET_DEFAULT)

    def test_filter_off_never_lifts_the_cap(self):
        text = "\n".join("row %d %s" % (i, "q" * 100) for i in range(5000)) + "\n"
        body, stats = shell_mod.render_shell_response(
            "ls -R", _result(stdout=text), _svc("."), filter_output=False)
        self.assertLessEqual(_nbytes(body), sr.BUDGET_DEFAULT)
        self.assertFalse(stats["filtered"])
        self.assertTrue(stats["needs_spill"])

    def test_max_bytes_lowers_and_config_lowers(self):
        text = "\n".join("row %d %s" % (i, "q" * 100) for i in range(500)) + "\n"
        body, stats = shell_mod.render_shell_response(
            "ls -R", _result(stdout=text), _svc("."), filter_output=False, max_bytes=4096)
        self.assertLessEqual(_nbytes(body), 4096)
        body, stats = shell_mod.render_shell_response(
            "ls -R", _result(stdout=text), _svc(".", hybrid_config={"shell_budget_bytes": 3000}),
            filter_output=False)
        self.assertLessEqual(_nbytes(body), 3000)
        self.assertEqual(stats["budget_bytes"], 3000)

    def test_filtered_small_output_needs_a_spill_because_the_filter_is_lossy(self):
        text = "\n".join("line %d" % i for i in range(200)) + "\n"
        body, stats = shell_mod.render_shell_response("ls -R", _result(stdout=text), _svc("."))
        self.assertTrue(stats["filtered"])
        self.assertTrue(stats["needs_spill"])

    def test_stderr_gets_its_share(self):
        out = "\n".join("out %d %s" % (i, "o" * 100) for i in range(3000)) + "\n"
        err = "\n".join("err %d %s" % (i, "e" * 100) for i in range(3000)) + "\n"
        body, _ = shell_mod.render_shell_response(
            "make", _result(stdout=out, stderr=err, exit_code=2), _svc("."), filter_output=False)
        self.assertLessEqual(_nbytes(body), sr.BUDGET_DEFAULT)
        err_part = body.split("--- stderr ---", 1)[1]
        self.assertGreaterEqual(_nbytes(err_part), int((sr.BUDGET_DEFAULT - sr.META_RESERVE) * 0.35))
        self.assertIn("err 0 ", body)
        self.assertIn("err 2999", body)


class TestLiveSpill(unittest.TestCase):
    """The real path: subprocess -> ShellCapture -> spill -> page back by id."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        (self.project / ".c3").mkdir()
        self.store_root = self.root / "store"
        self._env = patch.dict(os.environ, {"C3_SHELL_OUT_DIR": str(self.store_root)})
        self._env.start()
        self.svc = _svc(str(self.project))

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, cmd, **kw):
        return asyncio.run(shell_mod.handle_shell(cmd, "", 60, True, False, self.svc, _finalize, **kw))

    def test_three_megabyte_line_comes_back_small_with_an_id_and_pages_back(self):
        cmd = (f'"{PY}" -c "import sys; sys.stdout.write(\'a\'*1500000 + \'NEEDLE_7f3a\' + \'b\'*1500000 + chr(10)); '
               f'sys.stderr.write(\'warned\\n\')"')
        body = self._run(cmd)
        self.assertLessEqual(_nbytes(body), sr.BUDGET_CEILING, body[:300])
        self.assertIn("[c3_shell:OK]", body)
        self.assertIn("output_id=o-", body)
        self.assertIn("clipped", body)
        self.assertIn("warned", body)
        oid = body.split("output_id=", 1)[1].split(")", 1)[0].strip()
        self.assertRegex(oid, r"^o-[0-9a-f]{12}$")

        # search pages the match back, budgeted
        paged = self._run("", output_id=oid, output_action="search", pattern="NEEDLE_7f3a")
        self.assertIn("[c3_shell:output]", paged)
        self.assertIn("NEEDLE_7f3a", paged)
        self.assertLessEqual(_nbytes(paged), sr.BUDGET_CEILING)
        # read a window of stderr
        paged = self._run("", output_id=oid, output_action="read", stream="stderr", lines="1-1")
        self.assertIn("warned", paged)
        # tail
        paged = self._run("", output_id=oid, output_action="tail", lines=1)
        self.assertIn("[c3_shell:output]", paged)

        # the spill holds the raw bytes
        files = list(self.store_root.rglob(f"{oid}.stdout"))
        self.assertEqual(len(files), 1)
        self.assertIn(files[0].stat().st_size, (3_000_012, 3_000_013))  # LF or CRLF newline

        # another project cannot read it
        other = self.root / "other"
        other.mkdir()
        (other / ".c3").mkdir()
        foreign = asyncio.run(shell_mod.handle_shell(
            "", "", 60, True, False, _svc(str(other)), _finalize,
            output_id=oid, output_action="read"))
        self.assertIn("[c3_shell:error]", foreign)
        self.assertIn("not found for this project and session", foreign)

        # another session cannot read it
        with patch.object(_grants, "session_id", return_value="some-other-session"):
            stranger = self._run("", output_id=oid, output_action="read")
        self.assertIn("not found for this project and session", stranger)

        # a rule that now denies the cwd denies the bytes
        denial = access_guard.Denial("<test>", "deny", "user", "test rule")
        with patch.object(access_guard, "check", return_value=denial):
            denied = self._run("", output_id=oid, output_action="read")
        self.assertIn("[c3_shell:error]", denied)
        self.assertIn("no longer readable", denied)

        # delete, then it is gone
        gone = self._run("", output_id=oid, output_action="delete")
        self.assertIn("deleted", gone)
        self.assertIn("[c3_shell:error]", self._run("", output_id=oid, output_action="read"))

    def test_small_output_leaves_nothing_behind(self):
        body = self._run(f'"{PY}" -c "print(\'tiny\')"')
        self.assertIn("tiny", body)
        self.assertNotIn("output_id", body)
        spool = self.store_root / ".spool"
        leftovers = [p for p in self.store_root.rglob("o-*") if p.is_file()]
        self.assertEqual(leftovers, [])
        self.assertTrue(not spool.exists() or not any(spool.iterdir()))

    def test_bad_action_and_unknown_id(self):
        self.assertIn("output_action must be one of", self._run("", output_id="o-000000000000", output_action="zap"))
        self.assertIn("not found for this project and session", self._run("", output_id="o-000000000000"))
        self.assertIn("not found", self._run("", output_id="nonsense"))


if __name__ == "__main__":
    unittest.main()
