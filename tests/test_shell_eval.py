"""c3_shell output-shaping gate.

Renders the committed fixture suite (tests/shell_eval/fixture_suite.jsonl)
through the same renderer the MCP tool uses — ``render_shell_response`` once
it exists, the harness shim reproducing today's body until then — and
enforces the checked-in baseline:

* every ``must_pass`` case passes its checks (budget, must-keep, markers),
* every aggregate stays at or above its floor and at or below its ceiling,
* the baseline knows every case in the suite (add a case -> refresh it),
* an ``xfail`` that unexpectedly passes is reported, not failed.

No subprocess, no Ollama: generators are seeded and the filter's LLM pass is
disabled. See docs/shell-eval.md.
"""

import json
import warnings
from pathlib import Path

import pytest

from services.bench import shell_eval as she

SUITE = she.BUNDLED_SUITES["fixture"]
BASELINE = she.BUNDLED_BASELINES["fixture"]

# The spec's minimum case list. A rename here is a rename in the suite.
REQUIRED_CASES = {
    "minified_grep_hit", "pytest_buried_failure", "tsc_errors", "npm_build_noise",
    "cr_progress_bar", "jsonl_grep", "python_traceback", "cargo_errors", "jest_failure",
    "binary_garbage", "ansi_colored", "huge_stderr_empty_stdout", "mixed_streams_small",
    "under_budget_120_lines", "timeout_partial", "sed_long_sql_lines",
}


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    work = tmp_path_factory.mktemp("shell_eval")
    return she.run_suite("fixture", work_dir=work, baseline=BASELINE)


def _raw_cases():
    with open(SUITE, encoding="utf-8") as fh:
        objs = [json.loads(line) for line in fh if line.strip()]
    return objs[0], objs[1:]


class TestSuiteShape:
    def test_header_and_size(self):
        header, cases = she.load_suite(SUITE)
        assert header.get("fixture") is True
        assert header.get("default_max_bytes") == she.DEFAULT_MAX_BYTES
        assert len(cases) >= 16
        assert REQUIRED_CASES <= {c.id for c in cases}

    def test_every_generator_exists(self):
        _, cases = she.load_suite(SUITE)
        gens = she.load_generators()
        missing = sorted({c.generator for c in cases} - set(gens.GENERATORS))
        assert not missing, f"cases name unknown generators: {missing}"

    def test_gates_are_declared_and_xfails_name_a_phase(self):
        _, cases = she.load_suite(SUITE)
        assert all(c.gate in she.GATES for c in cases)
        bad = [c.id for c in cases if c.gate == "xfail" and c.phase not in she.PHASES]
        assert not bad, f"xfail cases must name S1 or S2: {bad}"
        assert any(c.gate == "must_pass" for c in cases)
        assert any(c.gate == "xfail" and c.phase == "S1" for c in cases)
        assert any(c.gate == "xfail" and c.phase == "S2" for c in cases)

    def test_checks_use_known_vocabulary_and_budget_ceiling(self):
        _, raw = _raw_cases()
        for d in raw:
            unknown = set(d.get("checks") or {}) - set(she.CHECK_KEYS)
            assert not unknown, f"{d['id']}: unknown checks {sorted(unknown)}"
            assert int((d.get("checks") or {}).get("max_bytes", 0)) <= she.CEILING_MAX_BYTES
        with pytest.raises(ValueError):
            she.ShellCase.from_dict({"id": "x", "cmd": "x", "generator": "empty_output",
                                     "gate": "xfail"})  # no phase
        with pytest.raises(ValueError):
            she.ShellCase.from_dict({"id": "x", "cmd": "x", "generator": "empty_output",
                                     "checks": {"max_bytes": she.CEILING_MAX_BYTES + 1}})

    def test_must_contain_refs_resolve(self):
        """Every ``$name`` a case asks for is a fragment its generator provides."""
        _, cases = she.load_suite(SUITE)
        gens = she.load_generators()
        dangling = []
        for c in cases:
            keep = gens.generate(c.generator, c.params, case_id=c.id).keep
            for key in ("must_contain", "must_not_contain"):
                for v in c.checks.get(key, []):
                    if v.startswith("$") and v[1:] not in keep:
                        dangling.append(f"{c.id}: {v}")
        assert not dangling, "unresolved fragment refs:\n" + "\n".join(dangling)

    def test_baseline_covers_every_case(self):
        _, cases = she.load_suite(SUITE)
        baseline = she.load_baseline(BASELINE)
        missing = sorted({c.id for c in cases} - set(baseline["per_case"]))
        extra = sorted(set(baseline["per_case"]) - {c.id for c in cases})
        assert not missing and not extra, (
            f"baseline out of date (missing={missing}, extra={extra}); run "
            "`c3 shell-eval --update-baseline`")


class TestGenerators:
    def test_deterministic_across_calls(self):
        gens = she.load_generators()
        for name in gens.GENERATORS:
            a = gens.generate(name, case_id=name)
            b = gens.generate(name, case_id=name)
            assert (a.stdout, a.stderr, a.exit_code, a.timed_out) == \
                (b.stdout, b.stderr, b.exit_code, b.timed_out), name

    def test_seed_follows_case_id_not_hash(self):
        gens = she.load_generators()
        assert gens.seed_for("minified_grep_hit") == gens.seed_for("minified_grep_hit")
        assert gens.seed_for("a") != gens.seed_for("b")
        assert gens.seed_for("a", {"seed": 7}) == 7

    def test_sizes_match_their_scenario(self):
        gens = she.load_generators()
        big = gens.generate("minified_grep_hit", {"lines": 3, "line_bytes": 300_000}, case_id="x")
        assert big.stdout.count("\n") == 3 and len(big.stdout) > 900_000
        assert big.keep["token"] in big.stdout
        crlf = gens.generate("tsc_errors", {"lines": 10, "crlf": True}, case_id="x")
        assert "\r\n" in crlf.stdout and crlf.exit_code == 2
        cr = gens.generate("cr_progress_bar", {"steps": 50}, case_id="x")
        assert cr.stdout.count("\r") == 50 and cr.stdout.count("\n") == 1
        blob = gens.generate("binary_garbage", {"bytes": 4096, "strip_newlines": True}, case_id="x")
        assert "\n" not in blob.stdout and "�" in blob.stdout
        err = gens.generate("huge_stderr_empty_stdout", {"bytes": 10_000}, case_id="x")
        assert err.stdout == "" and len(err.stderr) >= 10_000
        to = gens.generate("timeout_partial", {}, case_id="x")
        assert to.timed_out and to.as_result()["timed_out"] is True


class TestChecks:
    def _case(self, **checks):
        return she.ShellCase(id="t", cmd="x", generator="empty_output", checks=checks)

    def test_fragment_refs_and_retention(self):
        class S:
            keep = {"a": "alpha line", "b": "beta line"}
            stdout = stderr = ""
        body = "header\nalpha line\n"
        fails, retention = she.run_checks(self._case(must_contain=["$a", "$b", "header"]),
                                          body, {"response_bytes": len(body)}, S(), None)
        assert retention == round(2 / 3, 4)
        assert fails == ["must_contain: 'beta line' missing"]

    def test_budget_and_forbidden(self):
        body = "x" * 100 + "\x1b[31m"
        fails, _ = she.run_checks(self._case(max_bytes=50, must_not_contain=["\x1b["]),
                                  body, {"response_bytes": len(body)}, None, None)
        assert any(f.startswith("max_bytes") for f in fails)
        assert any(f.startswith("must_not_contain") for f in fails)

    def test_marker_inside_json_and_table(self):
        assert she.marker_inside('{"a": 1, [3 lines omitted] "b": 2}', "json")
        assert not she.marker_inside('{"a": 1}\n[3 lines omitted]\n{"b": 2}', "json")
        assert she.marker_inside("| a | [12 chars clipped] | c |", "table")
        assert not she.marker_inside("| a | b |\n[2 rows omitted]\n| c | d |", "table")
        assert she.MARKER_RE.search("[293 non-error lines omitted]")
        assert she.MARKER_RE.search("[line repeated x6 across output]")
        assert not she.MARKER_RE.search("[c3_shell:OK] 12ms")

    def test_spill_identical_is_pending_before_s1(self):
        fails, _ = she.run_checks(self._case(spill_identical=True), "body",
                                  {"response_bytes": 4, "spilled": False}, None, None)
        assert fails == ["spill_identical: output was not spilled (pre-S1)"]

    def test_spill_identical_reads_a_path(self, tmp_path):
        class S:
            keep = {}
            stdout = "raw out\n"
            stderr = "raw err\n"
        p = tmp_path / "spill.txt"
        p.write_text("raw out\nraw err\n", encoding="utf-8")
        stats = {"response_bytes": 4, "spilled": True, "output_id": "abc", "spill_path": str(p)}
        fails, _ = she.run_checks(self._case(spill_identical=True), "body", stats, S(), None)
        assert fails == []
        p.write_text("something else", encoding="utf-8")
        fails, _ = she.run_checks(self._case(spill_identical=True), "body", stats, S(), None)
        assert fails and "differs" in fails[0]


class TestRenderer:
    def test_small_output_renders_todays_body_verbatim(self, tmp_path):
        """The shim (or the real renderer) must produce exactly the body
        handle_shell produces for a short command — the shape every short
        command relies on."""
        svc = she.build_eval_svc(tmp_path)
        renderer, _ = she.resolve_renderer()
        result = {"exit_code": 0, "stdout": "a\nb\n", "stderr": "warn\n",
                  "duration_ms": 12, "timed_out": False, "shell": "git-bash"}
        body, stats = renderer("echo hi", result, svc, filter_output=True)
        assert body == "[c3_shell:OK] 12ms\n$ echo hi\n--- stdout ---\na\nb\n--- stderr ---\nwarn\n"
        assert stats["stdout_bytes"] == 4 and stats["stderr_bytes"] == 5
        assert stats["filtered"] is False and stats["spilled"] is False
        assert stats["response_bytes"] == len(body.encode("utf-8"))
        assert stats["response_tokens"] > 0
        for key in ("stdout_bytes", "stderr_bytes", "longest_line", "filtered", "spilled",
                    "output_id", "response_bytes", "response_tokens"):
            assert key in stats, key

    def test_timeout_and_fail_headers(self, tmp_path):
        svc = she.build_eval_svc(tmp_path)
        renderer, _ = she.resolve_renderer()
        body, _ = renderer("x", {"exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 5,
                                 "timed_out": True, "shell": "sh"}, svc)
        assert body.startswith("[c3_shell:TIMEOUT] 5ms\n")
        body, _ = renderer("x", {"exit_code": 3, "stdout": "", "stderr": "", "duration_ms": 5,
                                 "timed_out": False, "shell": "sh"}, svc)
        assert body.startswith("[c3_shell:FAIL(3)] 5ms\n")

    def test_filter_fires_past_threshold_but_not_for_git_diagnostics(self, tmp_path):
        svc = she.build_eval_svc(tmp_path)
        renderer, _ = she.resolve_renderer()
        stdout = "\n".join(f"line {i} distinct words here" for i in range(40)) + "\n"
        result = {"exit_code": 0, "stdout": stdout, "stderr": "", "duration_ms": 1,
                  "timed_out": False, "shell": "sh"}
        body, stats = renderer("ls", result, svc, filter_output=True)
        assert stats["filtered"] is True and "[stdout filtered]" in body
        body, stats = renderer("git status", result, svc, filter_output=True)
        assert stats["filtered"] is False and "[stdout filtered]" not in body
        body, stats = renderer("ls", result, svc, filter_output=False)
        assert stats["filtered"] is False


class TestFixtureGate:
    def test_every_case_executed(self, report):
        assert report.aggregates["n_errors"] == 0, report.aggregates["errors"]
        assert report.aggregates["n_executed"] == report.aggregates["n_cases"]

    def test_must_pass_cases_pass(self, report):
        failed = report.aggregates["must_pass_failed"]
        detail = "\n".join(f"{r.id}: {r.reason}" for r in report.results if r.id in failed)
        assert not failed, f"must_pass regressions:\n{detail}\n\n{report.render()}"

    def test_aggregates_meet_floors_and_ceilings(self, report):
        # The full table rides along so a CI log names the cases that moved,
        # not just the aggregate that fell.
        limits = [v for v in report.baseline_violations if "floor" in v or "ceiling" in v]
        assert not limits, "\n".join(limits) + "\n\n" + report.render()

    def test_no_violations(self, report):
        assert not report.baseline_violations, "\n".join(report.baseline_violations)

    def test_xfail_and_regressions_are_visible(self, report):
        for w in report.baseline_warnings:
            warnings.warn(w, stacklevel=1)
        for cid in report.aggregates["xfail_passing"]:
            warnings.warn(f"{cid} is xfail but passes; promote it", stacklevel=1)

    def test_report_renders_and_serialises(self, report):
        text = report.render()
        assert "verdict:" in text and "renderer=" in text
        assert report.suite == "fixture"
        data = report.to_dict()
        assert data["default_max_bytes"] == she.DEFAULT_MAX_BYTES
        assert len(data["results"]) == report.aggregates["n_cases"]
        json.dumps(data)  # must be JSON-serialisable for --json
        assert Path(report.suite_path).name == "fixture_suite.jsonl"

    def test_baseline_roundtrip(self, report, tmp_path):
        path = tmp_path / "baseline.json"
        she.write_baseline(report, path, floors={"pass_rate_must_pass": 1.0}, ceilings={"bytes_p50": 10})
        data = she.load_baseline(path)
        assert data["floors"] == {"pass_rate_must_pass": 1.0}
        violations, _ = she.compare_to_baseline(report, data)
        assert any("above ceiling" in v for v in violations)
        # keep_limits: a refresh without explicit limits keeps the hand-set ones
        she.write_baseline(report, path)
        assert she.load_baseline(path)["ceilings"] == {"bytes_p50": 10}
