"""S2 of the c3_shell remediation: content-aware keep.

The Cod rule (2026-09-04): always strip ANSI/control sequences and collapse
carriage-return progress updates; otherwise preserve complete under-budget
output; run deterministic parsers always to identify priority regions but
omit only over budget; pytest/unittest, cargo/rustc, tsc, jest/vitest first,
everything else generic error anchors plus head/tail; collapse only
consecutive normalised duplicates. These tests pin:

- normalisation: ANSI (CSI, OSC, single-char), control chars, CRLF, the
  final state of a ``\\r`` line, duplicate runs (never fewer than three),
  the fuzzy duplicate test, the raw line-number map, ``collapse=False``;
- runner detection from realistic snippets and from the command head;
- priority regions per runner and the generic anchors;
- the structured summary per runner;
- ``shape_stream`` with priority regions under and over budget;
- the renderer end to end: a 5,000-line pytest run with one buried failure
  keeps the id, the assertion and the summary under 18 KiB with the legacy
  filter gone from the render path.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools import shell as shell_mod  # noqa: E402
from cli.tools import shell_parsers as sp  # noqa: E402
from cli.tools import shell_render as sr  # noqa: E402
from services.output_filter import OutputFilter  # noqa: E402

GEN = Path(__file__).parent / "shell_eval" / "generators.py"


def _gens():
    from services.bench.shell_eval import load_generators
    return load_generators(GEN)


def _result(stdout="", stderr="", exit_code=0, timed_out=False, duration_ms=7):
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "duration_ms": duration_ms, "timed_out": timed_out, "shell": "git-bash"}


def _svc(tmp: str = ".", **extra):
    base = dict(project_path=tmp, activity_log=None, edit_ledger=None,
                output_filter=OutputFilter({"HYBRID_DISABLE_TIER1": True}),
                session_mgr=None, hybrid_config={})
    base.update(extra)
    return SimpleNamespace(**base)


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


# ── normalisation ───────────────────────────────────────────────────────────

class TestNormalize(unittest.TestCase):
    def test_ansi_csi_osc_and_single_char_escapes_are_stripped_and_counted(self):
        text = ("\x1b[31merror\x1b[0m \x1b]0;title\x07plain \x1b(Bx \x1b7y\x1b8\n"
                "\x1b[2K\x1b[1;32mgreen\x1b[22;39m\n")
        out, info = sp.normalize_stream(text)
        self.assertEqual(out, "error plain x y\ngreen\n")
        self.assertEqual(info["ansi_stripped"], 9)
        self.assertEqual(info["cr_collapsed"], 0)
        self.assertEqual(info["dup_collapsed"], 0)
        self.assertIsNone(info["line_numbers"])

    def test_control_chars_go_but_tab_and_newline_stay(self):
        out, info = sp.normalize_stream("a\tb\x00c\x07d\x08e\x7ff\n")
        self.assertEqual(out, "a\tbcdef\n")
        self.assertEqual(info["ansi_stripped"], 4)

    def test_crlf_is_a_newline_not_a_progress_rewrite(self):
        out, info = sp.normalize_stream("one\r\ntwo\r\nthree\r\n")
        self.assertEqual(out, "one\ntwo\nthree\n")
        self.assertEqual(info["cr_collapsed"], 0)

    def test_cr_line_keeps_its_final_state_and_counts_the_overwritten_ones(self):
        bar = "".join(f"\rDownloading {i}/5" for i in range(1, 6)) + "\n"
        out, info = sp.normalize_stream(bar + "done\n")
        self.assertEqual(out, "Downloading 5/5\ndone\n")
        self.assertEqual(info["cr_collapsed"], 4)
        # a trailing \r before the newline leaves the previous state on screen
        out, info = sp.normalize_stream("abc\r\n")
        self.assertEqual(out, "abc\n")
        out, info = sp.normalize_stream("abc\r\rdef\r\n")
        self.assertEqual(out, "def\n")
        self.assertEqual(info["cr_collapsed"], 1)
        # a leading \r with one state is not a loss
        out, info = sp.normalize_stream("\rsingle\n")
        self.assertEqual((out, info["cr_collapsed"]), ("single\n", 0))

    def test_consecutive_duplicates_fold_from_three_up(self):
        out, info = sp.normalize_stream("x\nx\n")
        self.assertEqual(out, "x\nx\n")
        self.assertEqual(info["dup_collapsed"], 0)
        out, info = sp.normalize_stream("x\nx\nx\ny\n")
        self.assertEqual(out, "x [x 3]\ny\n")
        self.assertEqual(info["dup_collapsed"], 2)
        self.assertEqual(info["line_numbers"], [1, 4])
        out, info = sp.normalize_stream("same\n" * 200)
        self.assertEqual(out, "same [x 200]\n")
        self.assertEqual(info["dup_collapsed"], 199)
        # non-consecutive repeats are not duplicates
        out, info = sp.normalize_stream("a\nb\na\nb\na\nb\n")
        self.assertEqual(info["dup_collapsed"], 0)

    def test_blank_runs_are_never_folded(self):
        out, info = sp.normalize_stream("a\n\n\n\n\nb\n")
        self.assertEqual(out, "a\n\n\n\n\nb\n")
        self.assertEqual(info["dup_collapsed"], 0)

    def test_fuzzy_duplicates_only_when_asked(self):
        log = "\n".join(f"2026-09-04T12:00:{i:02d} worker-7 heartbeat 0x{i * 4919:08x} ok" for i in range(5)) + "\n"
        out, info = sp.normalize_stream(log)
        self.assertEqual(info["dup_collapsed"], 0)
        self.assertEqual(out, log)
        out, info = sp.normalize_stream(log, fuzzy_dups=True)
        self.assertEqual(info["dup_collapsed"], 4)
        self.assertTrue(out.startswith("2026-09-04T12:00:00 worker-7 heartbeat 0x00000000 ok [x 5]\n"), out)
        self.assertEqual(info["line_numbers"], [1])

    def test_collapse_false_strips_ansi_only(self):
        text = "\x1b[31mx\x1b[0m\nx\nx\nx\n\rp1\rp2\n"
        out, info = sp.normalize_stream(text, collapse=False)
        self.assertEqual(out, "x\nx\nx\nx\n\rp1\rp2\n")
        self.assertEqual(info["ansi_stripped"], 2)
        self.assertEqual(info["cr_collapsed"], 0)
        self.assertEqual(info["dup_collapsed"], 0)

    def test_empty_and_no_trailing_newline(self):
        self.assertEqual(sp.normalize_stream("")[0], "")
        self.assertEqual(sp.normalize_stream(None)[0], "")
        out, _ = sp.normalize_stream("a\nb")
        self.assertEqual(out, "a\nb")


# ── runner detection ────────────────────────────────────────────────────────

PYTEST_OUT = ("============================= test session starts =============================\n"
              "collected 3 items\n\ntests/test_a.py::test_one PASSED\n"
              "======== 1 failed, 2 passed in 0.12s ========\n")
UNITTEST_ERR = "test_one (tests.test_a.ATests.test_one) ... ok\n\n" + "-" * 70 + "\nRan 3 tests in 0.004s\n\nOK\n"
CARGO_ERR = ("   Compiling ledgerlite v0.4.2 (/work)\nerror[E0308]: mismatched types\n"
             "  --> src/lib.rs:4:18\n   |\n 4 |     let x: u64 = 1.5;\n   |                  ^^^ expected `u64`, found `f64`\n\n"
             "error: could not compile `ledgerlite` (lib) due to 1 previous error\n")
TSC_OUT = "src/a.ts(3,5): error TS2322: Type 'string' is not assignable to type 'number'.\nFound 1 error in 1 file.\n"
JEST_ERR = ("PASS src/a.test.ts\nFAIL src/b.test.ts\n  ● Invoice › rounds\n\n    expect(received).toBe(expected)\n\n"
            "    Expected: 1\n    Received: 2\n\nTest Suites: 1 failed, 1 passed, 2 total\n"
            "Tests:       1 failed, 3 passed, 4 total\nTime:        1.2 s\n")
VITEST_OUT = (" RUN  v2.1.0 /work\n\n ✓ src/a.test.ts (2)\n ❯ src/b.test.ts (1)\n   × adds\n\n"
              "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯\n\n FAIL  src/b.test.ts > adds\n"
              "AssertionError: expected 3 to be 4\n\n- Expected\n+ Received\n\n- 4\n+ 3\n\n"
              " ❯ src/b.test.ts:5:15\n\n      Test Files  1 failed | 1 passed (2)\n"
              "           Tests  1 failed | 2 passed (3)\n        Duration  412ms\n")


class TestDetectRunner(unittest.TestCase):
    def test_from_output_signatures(self):
        self.assertEqual(sp.detect_runner("make check", PYTEST_OUT, ""), "pytest")
        self.assertEqual(sp.detect_runner("./run", "", UNITTEST_ERR), "unittest")
        self.assertEqual(sp.detect_runner("make", "", CARGO_ERR), "cargo")
        self.assertEqual(sp.detect_runner("npm run typecheck", TSC_OUT, ""), "tsc")
        self.assertEqual(sp.detect_runner("npm test", "", JEST_ERR), "jest")
        self.assertEqual(sp.detect_runner("npm test", VITEST_OUT, ""), "vitest")

    def test_from_command_head_when_output_says_nothing(self):
        self.assertEqual(sp.detect_runner('cd "U:/x y" && python -m pytest -q', "", ""), "pytest")
        self.assertEqual(sp.detect_runner("PYTHONPATH=. python3 -m unittest discover", "", ""), "unittest")
        self.assertEqual(sp.detect_runner("cargo test --release", "", ""), "cargo")
        self.assertEqual(sp.detect_runner("npx tsc --noEmit", "", ""), "tsc")
        self.assertEqual(sp.detect_runner("npx jest --ci", "", ""), "jest")
        self.assertEqual(sp.detect_runner("npx vitest run", "", ""), "vitest")

    def test_none_for_plain_commands(self):
        self.assertIsNone(sp.detect_runner("ls -la", "a\nb\n", ""))
        self.assertIsNone(sp.detect_runner("git status", "On branch main\n", ""))
        self.assertIsNone(sp.detect_runner("cat tests_pytest_notes.md", "some prose\n", ""))


# ── priority regions ────────────────────────────────────────────────────────

def _lines(text: str) -> list[str]:
    return sr.split_lines(text)


def _covered(regions) -> set[int]:
    return {i for a, b, _ in regions for i in range(a, b + 1)}


class TestPriorityRegions(unittest.TestCase):
    def test_pytest_marks_failure_block_short_summary_summary_and_progress_line(self):
        streams = _gens().generate("pytest_buried_failure", {"lines": 400, "passed": 300, "warning_lines": 20},
                                   case_id="t")
        lines = _lines(streams.stdout)
        regions = sp.priority_regions("pytest", lines)
        whys = [why for _, _, why in regions]
        self.assertEqual(whys[0], "pytest summary")
        self.assertIn("short test summary", whys)
        self.assertTrue(any(w.startswith("pytest failure test_invoice_rounding_3366") for w in whys))
        self.assertIn("failed test", whys)
        kept = _covered(regions)
        for needle in (streams.keep["summary"], "FAILED " + streams.keep["failing_test"],
                       "E   AssertionError", streams.keep["last_frame"], "FAILED [", "_ test_invoice_rounding_3366 _"):
            self.assertTrue(any(needle in lines[i] for i in kept), needle)
        self.assertLess(len(kept), 60)

    def test_pytest_long_block_keeps_header_and_tail(self):
        lines = ["___ test_big ___", ""] + [f"captured line {i}" for i in range(80)] + \
                ["E   AssertionError: boom", "", "tests/test_x.py:9: AssertionError", "=== 1 failed in 1s ==="]
        regions = sp.priority_regions("pytest", lines)
        kept = _covered(regions)
        self.assertIn(0, kept)
        self.assertIn(len(lines) - 2, kept)
        self.assertIn(len(lines) - 4, kept)
        self.assertNotIn(20, kept)

    def test_unittest_marks_headers_blocks_progress_and_totals(self):
        streams = _gens().generate("unittest_failures", {"tests": 60}, case_id="u")
        lines = _lines(streams.stderr)
        regions = sp.priority_regions("unittest", lines)
        whys = [why for _, _, why in regions]
        self.assertEqual(whys[0], "unittest totals")
        self.assertIn("unittest FAIL test_rounding", whys)
        self.assertIn("unittest ERROR test_lookup", whys)
        kept = _covered(regions)
        for needle in (streams.keep["assertion"], streams.keep["exception"], streams.keep["ran"],
                       streams.keep["verdict"], "test_rounding (", "... FAIL", "... ERROR"):
            self.assertTrue(any(needle in lines[i] for i in kept), needle)

    def test_cargo_marks_verdict_error_blocks_with_arrow_then_warnings(self):
        text = CARGO_ERR + "warning: unused variable: `y`\n  --> src/lib.rs:9:9\n   |\n\nwarning: `ledgerlite` (lib) generated 1 warning\n"
        lines = _lines(text)
        regions = sp.priority_regions("cargo", lines)
        whys = [why for _, _, why in regions]
        self.assertEqual(whys[0], "cargo verdict")
        self.assertIn("rustc error[E0308]", whys)
        self.assertEqual(whys[-1], "rustc warning")
        kept = _covered(regions)
        self.assertTrue(any("--> src/lib.rs:4:18" in lines[i] for i in kept))
        self.assertTrue(any("could not compile" in lines[i] for i in kept))
        self.assertTrue(any("generated 1 warning" in lines[i] for i in kept))

    def test_cargo_test_failures_and_panics(self):
        text = ("running 3 tests\ntest a ... ok\ntest b ... FAILED\ntest c ... ok\n\nfailures:\n\n"
                "---- b stdout ----\nthread 'b' panicked at src/lib.rs:12:5:\nassertion failed: x == y\n\n"
                "failures:\n    b\n\ntest result: FAILED. 2 passed; 1 failed; 0 ignored\n")
        regions = sp.priority_regions("cargo", _lines(text))
        whys = [why for _, _, why in regions]
        self.assertIn("cargo verdict", whys)
        self.assertIn("failed test", whys)
        self.assertIn("panic b", whys)

    def test_tsc_first_last_found_then_the_rest(self):
        streams = _gens().generate("tsc_errors", {"lines": 50}, case_id="t")
        lines = _lines(streams.stdout)
        regions = sp.priority_regions("tsc", lines)
        self.assertEqual(regions[0][:2], (0, 0))
        self.assertEqual(regions[1][:2], (49, 49))
        self.assertEqual(regions[2], (50, 50, "tsc totals"))
        # first, last, totals, then the wall capped to TSC_ERRORS_HEAD + TSC_ERRORS_TAIL
        self.assertEqual(len(regions), 3 + sp.TSC_ERRORS_HEAD + sp.TSC_ERRORS_TAIL)
        kept = {r[0] for r in regions}
        self.assertTrue({1, 2, 20}.issubset(kept))
        self.assertTrue({44, 48}.issubset(kept))
        self.assertNotIn(30, kept)
        # the pretty (multi-line) tsc format is an error line too
        pretty = ["src/a.ts:3:5 - error TS2322: Type 'x'.", "", "3 const n: number = 'x';", "", "Found 1 error."]
        regions = sp.priority_regions("tsc", pretty)
        self.assertEqual([r[:2] for r in regions], [(0, 0), (4, 4)])

    def test_jest_bullet_block_through_received_and_code_frame_plus_totals(self):
        streams = _gens().generate("jest_failure", {"suites": 5}, case_id="j")
        lines = _lines(streams.stdout)
        regions = sp.priority_regions("jest", lines)
        whys = [why for _, _, why in regions]
        self.assertEqual(whys[0], "jest totals")
        self.assertIn("jest failure Invoice › rounds totals to cents", whys)
        self.assertIn("failed suite", whys)
        kept = _covered(regions)
        for needle in ("● Invoice", "Received: 1234.57", "> 42 |", "Tests:       1 failed", "FAIL src/__tests__/invoice"):
            self.assertTrue(any(needle in lines[i] for i in kept), needle)
        self.assertFalse(any(lines[i].startswith("PASS ") for i in kept))

    def test_vitest_case_block_and_totals(self):
        lines = _lines(VITEST_OUT)
        regions = sp.priority_regions("vitest", lines)
        whys = [why for _, _, why in regions]
        self.assertEqual(whys[0], "vitest totals")
        self.assertIn("vitest failure adds", whys)
        kept = _covered(regions)
        for needle in ("FAIL  src/b.test.ts > adds", "AssertionError: expected 3", "❯ src/b.test.ts:5:15",
                       "Test Files  1 failed"):
            self.assertTrue(any(needle in lines[i] for i in kept), needle)

    def test_generic_anchors_with_context_last_first_then_latest(self):
        lines = [f"line {i}" for i in range(100)]
        lines[10] = "Traceback (most recent call last):"
        lines[40] = "npm ERR! code ELIFECYCLE"
        lines[70] = "FATAL: database is locked"
        lines[90] = "KeyError: 'x'"
        regions = sp.priority_regions(None, lines)
        self.assertEqual([r[:2] for r in regions][:2], [(88, 92), (8, 12)])
        self.assertEqual({r[2] for r in regions}, {"error anchor"})
        self.assertEqual(_covered(regions), set(range(8, 13)) | set(range(38, 43)) | set(range(68, 73)) | set(range(88, 93)))
        # cap
        many = ["error here"] * 500
        self.assertEqual(len(sp.priority_regions(None, many)), sp.GENERIC_ANCHOR_CAP)
        self.assertEqual(sp.priority_regions(None, ["all", "fine"]), [])
        self.assertEqual(sp.priority_regions("pytest", []), [])


# ── structured tail ─────────────────────────────────────────────────────────

class TestStructuredTail(unittest.TestCase):
    def test_none_for_no_runner(self):
        self.assertEqual(sp.structured_tail(None, ["Error: x"], 1), "")

    def test_pytest_totals_and_one_line_per_failure(self):
        streams = _gens().generate("pytest_three_failures", {"passed": 100, "warning_lines": 5}, case_id="p")
        tail = sp.structured_tail("pytest", _lines(streams.stdout), 1)
        rows = tail.splitlines()
        self.assertEqual(rows[0], "--- summary ---")
        self.assertEqual(rows[1], "pytest: 3 failed, 100 passed, 7 warnings in 188.40s")
        self.assertEqual(rows[2], "failed: tests/test_billing.py::test_invoice_rounding — AssertionError: assert 1234.57 == 1234.56")
        self.assertEqual(len(rows), 5)
        self.assertLess(_nbytes(tail), 400)
        # without a short summary the failure headers + first E line are used
        block = ["___ test_x ___", "E   ValueError: bad", "=== 1 failed in 0.1s ==="]
        self.assertIn("failed: test_x — ValueError: bad", sp.structured_tail("pytest", block, 1))
        self.assertIn("pytest: exit 2, no summary line found", sp.structured_tail("pytest", ["boom"], 2))

    def test_unittest_cargo_tsc_jest_vitest(self):
        u = sp.structured_tail("unittest", _lines(_gens().generate("unittest_failures", {"tests": 40}, case_id="u").stderr), 1)
        self.assertIn("unittest: Ran 40 tests in 12.345s; FAILED (failures=1, errors=1)", u)
        self.assertIn("fail: test_rounding (tests.test_billing.BillingTests.test_rounding) — AssertionError: 1234.57 != 1234.56", u)
        self.assertIn("error: test_lookup (tests.test_cache.CacheTests.test_lookup) — KeyError: 'invoice_total'", u)
        c = sp.structured_tail("cargo", _lines(CARGO_ERR), 101)
        self.assertIn("cargo: error: could not compile `ledgerlite` (lib) due to 1 previous error", c)
        self.assertIn("error[E0308]: mismatched types — src/lib.rs:4:18", c)
        t = sp.structured_tail("tsc", _lines(TSC_OUT), 2)
        self.assertIn("tsc: Found 1 error in 1 file.", t)
        self.assertIn("src/a.ts(3,5): TS2322 Type 'string' is not assignable to type 'number'.", t)
        j = sp.structured_tail("jest", _lines(JEST_ERR), 1)
        self.assertIn("jest: Test Suites: 1 failed, 1 passed, 2 total; Tests: 1 failed, 3 passed, 4 total", j)
        self.assertIn("failed: Invoice › rounds — Expected: 1 / Received: 2", j)
        v = sp.structured_tail("vitest", _lines(VITEST_OUT), 1)
        self.assertIn("vitest: Test Files 1 failed | 1 passed (2); Tests 1 failed | 2 passed (3)", v)
        self.assertIn("failed: adds — AssertionError: expected 3 to be 4", v)

    def test_failure_list_is_capped(self):
        streams = _gens().generate("tsc_errors", {"lines": 900}, case_id="t")
        tail = sp.structured_tail("tsc", _lines(streams.stdout), 2)
        rows = tail.splitlines()
        self.assertEqual(rows[1], "tsc: Found 900 errors in 900 files.")
        self.assertEqual(len(rows), 2 + sp.SUMMARY_FAIL_CAP + 1)
        self.assertEqual(rows[-1], "… and 880 more")


# ── shape_stream with priority ──────────────────────────────────────────────

class TestShapeWithPriority(unittest.TestCase):
    def _text(self, n=3000):
        lines = ["line %05d %s" % (i, "z" * 60) for i in range(n)]
        if n > 1501:
            lines[1500] = "line 01500 THE FAILURE"
            lines[1501] = "line 01501 E   AssertionError"
        return "\n".join(lines) + "\n", lines

    def test_under_budget_priority_changes_nothing(self):
        text, _ = self._text(40)
        rendered, info = sr.shape_stream(full_text=text, total_bytes=_nbytes(text), total_lines=40,
                                         alloc=8192, priority=[(20, 21, "x")])
        self.assertEqual(rendered, text)
        self.assertFalse(info["cut"])
        self.assertEqual(info["priority_kept"], 0)

    def test_over_budget_priority_survives_inside_the_allocation_with_notes(self):
        text, lines = self._text()
        rendered, info = sr.shape_stream(full_text=text, total_bytes=_nbytes(text), total_lines=3000,
                                         alloc=4096, output_id="o-0123456789ab",
                                         priority=[(1500, 1501, "pytest failure test_x"), (2999, 2999, "summary")])
        self.assertTrue(info["cut"])
        self.assertLessEqual(_nbytes(rendered), 4096)
        self.assertIn("line 01500 THE FAILURE", rendered)
        self.assertIn("line 01501 E   AssertionError", rendered)
        self.assertIn("[L1501-1502: pytest failure test_x]", rendered)
        self.assertIn("line 00000", rendered)
        self.assertIn("line 02999", rendered)
        self.assertNotIn("[L3000: summary]", rendered)   # contiguous with the tail: no note
        self.assertIn("omitted; full output via c3_shell(output_id='o-0123456789ab'", rendered)
        self.assertEqual(info["priority_kept"], 3)
        self.assertEqual(info["omitted_lines"], 3000 - (rendered.count("\nline ") + 1))
        self.assertGreater(info["omitted_bytes"], 0)
        # region note and the gap note sit on their own lines
        for ln in rendered.splitlines():
            if ln.startswith("[L") or ln.startswith("[…"):
                self.assertTrue(ln.endswith("]"), ln)

    def test_regions_in_priority_order_until_the_allocation_is_spent(self):
        text, lines = self._text()
        prio = [(2999, 2999, "summary"), (1500, 1501, "failure")] + [(i, i, "tsc error") for i in range(0, 2999)]
        rendered, info = sr.shape_stream(full_text=text, total_bytes=_nbytes(text), total_lines=3000,
                                         alloc=6144, priority=prio)
        self.assertLessEqual(_nbytes(rendered), 6144)
        self.assertIn("line 02999", rendered)
        self.assertIn("THE FAILURE", rendered)
        self.assertGreater(info["priority_kept"], 40)
        self.assertLess(info["priority_kept"], 3000)

    def test_priority_share_leaves_room_for_head_and_tail(self):
        text, lines = self._text()
        prio = [(i, i, "error anchor") for i in range(100, 2900)]
        rendered, info = sr.shape_stream(full_text=text, total_bytes=_nbytes(text), total_lines=3000,
                                         alloc=6144, priority=prio, priority_share=0.6)
        self.assertLessEqual(_nbytes(rendered), 6144)
        self.assertIn("line 00000", rendered)
        self.assertIn("line 02999", rendered)
        self.assertIn("line 00100", rendered)

    def test_line_numbers_map_notes_to_raw_numbers(self):
        raw = "\n".join(["dup"] * 50 + ["line %04d %s" % (i, "q" * 80) for i in range(1500)]) + "\n"
        norm, info = sp.normalize_stream(raw)
        self.assertEqual(info["dup_collapsed"], 49)
        rendered, _ = sr.shape_stream(full_text=norm, total_bytes=_nbytes(norm), total_lines=1550,
                                      alloc=3000, line_numbers=info["line_numbers"],
                                      priority=[(800, 800, "marker")])
        self.assertIn("[L850: marker]", rendered)   # 800th normalised line is raw line 850
        self.assertIn("dup [x 50]", rendered)

    def test_previews_path_with_priority(self):
        head = "\n".join(f"head {i}" for i in range(200)) + "\n"
        tail = "torn\n" + "\n".join(f"tail {i}" for i in range(200)) + "\n"
        h, t, torn = sr.split_preview(head, tail)
        self.assertTrue(torn)
        idx = len(h) + t.index("tail 20")     # outside both windows: needs its own note
        rendered, info = sr.shape_stream(head=head, tail=tail, total_bytes=9_000_000, total_lines=200_000,
                                         alloc=1500, priority=[(idx, idx, "the error")])
        self.assertLessEqual(_nbytes(rendered), 1500)
        self.assertIn("tail 20\n", rendered)
        self.assertIn(": the error]", rendered)
        self.assertEqual(info["priority_kept"], 1)
        self.assertIn("head 0", rendered)
        self.assertIn("tail 199", rendered)
        self.assertNotIn("torn", rendered)
        self.assertIn("not spilled", rendered)


# ── the renderer end to end ─────────────────────────────────────────────────

class TestRendererS2(unittest.TestCase):
    def test_legacy_filter_is_gone_from_the_render_path(self):
        self.assertFalse(hasattr(shell_mod, "handle_filter"))

    def test_5000_line_pytest_keeps_id_assertion_and_summary_under_18k(self):
        streams = _gens().generate("pytest_buried_failure", {"lines": 5000, "passed": 3366, "warning_lines": 40},
                                   case_id="pytest_buried_failure")
        body, stats = shell_mod.render_shell_response("python -m pytest -v", streams.as_result(), _svc())
        self.assertLessEqual(_nbytes(body), 18 * 1024)
        self.assertNotIn("[stdout filtered]", body)
        self.assertEqual(stats["runner"], "pytest")
        self.assertIn(streams.keep["failing_test"] + " FAILED", body)
        self.assertIn("E   AssertionError: assert 1234.57 == 1234.56", body)
        self.assertIn(streams.keep["last_frame"], body)
        self.assertIn(streams.keep["summary"], body)
        # the failure block sits inside the tail window (no note needed); the
        # progress line is 1,700 lines from either end and rides on its note
        self.assertIn(": failed test]\n" + streams.keep["failing_test"] + " FAILED", body)
        self.assertIn("--- summary ---\npytest: 1 failed, 3366 passed, 12 warnings in 240.12s\n"
                      "failed: tests/test_billing.py::test_invoice_rounding_3366 — AssertionError", body)
        self.assertIn("omitted; not spilled", body)
        self.assertTrue(stats["needs_spill"])
        self.assertGreater(stats["priority_lines"], 10)
        self.assertGreater(stats["dup_collapsed"], 0)   # the padding lines fold once over budget
        self.assertEqual(stats["ansi_stripped"], 0)
        self.assertIn("[collapsed", body)

    def test_under_budget_output_is_whole_and_unnumbered(self):
        text = "\n".join(f"{i:03d} row {i}" for i in range(120)) + "\n"
        body, stats = shell_mod.render_shell_response("ls -la", _result(stdout=text), _svc())
        self.assertIn("--- stdout ---\n" + text, body)
        self.assertFalse(stats["filtered"])
        self.assertFalse(stats["needs_spill"])
        self.assertIsNone(stats["runner"])
        self.assertEqual(stats["priority_lines"], 0)
        self.assertNotIn("--- summary ---", body)

    def test_ansi_is_stripped_without_a_loss_note_and_cr_is_a_loss(self):
        body, stats = shell_mod.render_shell_response(
            "npx eslint --color", _result(stdout="\x1b[31merror\x1b[0m  rule\n", exit_code=1), _svc())
        self.assertIn("--- stdout ---\nerror  rule\n", body)
        self.assertEqual(stats["ansi_stripped"], 2)
        self.assertFalse(stats["filtered"])
        self.assertFalse(stats["needs_spill"])
        self.assertNotIn("[collapsed", body)
        bar = "".join(f"\r{i}/9" for i in range(10)) + "\n"
        body, stats = shell_mod.render_shell_response("python dl.py", _result(stdout=bar), _svc())
        self.assertIn("--- stdout ---\n9/9\n", body)
        self.assertEqual(stats["cr_collapsed"], 9)
        self.assertTrue(stats["filtered"] and stats["needs_spill"])
        self.assertIn("[collapsed 9 cr rewrites]", body)

    def test_filter_output_false_skips_collapses_but_strips_ansi_and_keeps_the_cap(self):
        text = "\x1b[32mok\x1b[0m\n" + "same\n" * 100 + "".join(f"\rp{i}" for i in range(5)) + "\n"
        body, stats = shell_mod.render_shell_response("./run", _result(stdout=text), _svc(), filter_output=False)
        self.assertNotIn("\x1b[", body)
        self.assertEqual(body.count("same\n"), 100)
        self.assertNotIn("[x 100]", body)
        self.assertIn("\rp0\rp1", body)
        self.assertFalse(stats["filtered"])
        self.assertEqual(stats["dup_collapsed"], 0)
        big = "\n".join("row %d %s" % (i, "q" * 100) for i in range(5000)) + "\n"
        body, stats = shell_mod.render_shell_response("./run", _result(stdout=big), _svc(), filter_output=False)
        self.assertLessEqual(_nbytes(body), sr.BUDGET_DEFAULT)

    def test_summary_only_past_thirty_lines_and_inside_the_budget(self):
        short = PYTEST_OUT
        body, stats = shell_mod.render_shell_response("pytest", _result(stdout=short, exit_code=1), _svc())
        self.assertEqual(stats["runner"], "pytest")
        self.assertNotIn("--- summary ---", body)
        streams = _gens().generate("tsc_errors", {"lines": 900}, case_id="tsc_wall_900")
        body, stats = shell_mod.render_shell_response("npx tsc", streams.as_result(), _svc(), max_bytes=6000)
        self.assertLessEqual(_nbytes(body), 6000)
        self.assertIn("--- summary ---\ntsc: Found 900 errors in 900 files.\n", body)
        self.assertIn(streams.keep["first"], body)
        self.assertIn(streams.keep["last_error"], body)
        self.assertIn("[L900: tsc error]", body)
        self.assertEqual(stats["runner"], "tsc")

    def test_crlf_output_from_windows_tools(self):
        text = "a\r\nb\r\n" + "".join(f"\r{i}%" for i in range(3)) + "\r\ndone\r\n"
        body, stats = shell_mod.render_shell_response("build.bat", _result(stdout=text), _svc())
        self.assertIn("--- stdout ---\na\nb\n2%\ndone\n", body)
        self.assertEqual(stats["cr_collapsed"], 2)

    def test_previews_path_keeps_the_runner_verdict(self):
        head = "\n".join(f"ld.lld: warning: obj{i}.o discarded" for i in range(400)) + "\n"
        tail = "d\n" + "\n".join(f"ld.lld: warning: obj{i}.o discarded" for i in range(9000, 9400)) + \
               "\nld.lld: error: undefined symbol: compute_total_v2\n"
        st = SimpleNamespace(bytes=5_000_000, lines=120_000, longest_line=80, head=head, tail=tail)
        empty = SimpleNamespace(bytes=0, lines=0, longest_line=0, head="", tail="")
        capture = SimpleNamespace(output_id="o-0123456789ab", stats=SimpleNamespace(stdout=empty, stderr=st))
        result = _result(stdout=None, stderr=None, exit_code=1)
        result["capture"] = capture
        body, stats = shell_mod.render_shell_response("ninja -C build", result, _svc())
        self.assertLessEqual(_nbytes(body), sr.BUDGET_DEFAULT)
        self.assertIn("ld.lld: error: undefined symbol: compute_total_v2", body)
        self.assertIn("output_id='o-0123456789ab'", body)
        self.assertTrue(stats["spilled"])
        self.assertGreater(stats["dup_collapsed"], 0)      # fuzzy fold applies to previews at once
        self.assertGreaterEqual(stats["priority_lines"], 1)

    def test_stats_carry_the_s2_keys(self):
        _, stats = shell_mod.render_shell_response("true", _result(), _svc())
        for key in ("runner", "ansi_stripped", "cr_collapsed", "dup_collapsed", "priority_lines",
                    "filtered", "spilled", "output_id", "budget_bytes", "omitted_lines", "clipped_lines"):
            self.assertIn(key, stats)


if __name__ == "__main__":
    unittest.main()
