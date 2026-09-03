"""c3_search relevance gate.

Runs the committed fixture suite (tests/search_eval/fixture_suite.jsonl)
through the real ``handle_search`` code path against a temp copy of
tests/fixtures/search_eval_repo, then enforces the checked-in baseline:

* every ``must_pass`` case hits within its k,
* no case leaks forbidden text (the masked-CSV canary),
* every aggregate stays at or above its floor,
* the baseline knows every case in the suite (add a case -> refresh it).

Semantic cases are skipped here (no Ollama in CI); ``c3 search-eval`` runs
them locally. See docs/search-eval.md.
"""

import json
import warnings
from pathlib import Path

import pytest

from services.bench import search_eval as se

SUITE = se.BUNDLED_SUITES["fixture"]
BASELINE = se.BUNDLED_BASELINES["fixture"]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    work = tmp_path_factory.mktemp("search_eval")
    return se.run_suite("fixture", work_dir=work, semantic="off", baseline=BASELINE)


def _raw_cases():
    with open(SUITE, encoding="utf-8") as fh:
        objs = [json.loads(line) for line in fh if line.strip()]
    return objs[0], objs[1:]


class TestSuiteShape:
    def test_header_and_size(self):
        header, cases = se.load_suite(SUITE)
        assert header.get("fixture") is True
        assert header.get("c3_config", {}).get("access", {}).get("mask"), "fixture must carry a mask rule"
        assert len(cases) >= 40

    def test_every_expected_file_exists_in_the_fixture(self):
        _, cases = _raw_cases()
        missing = []
        for case in cases:
            for rel in (case.get("expect") or {}).get("files", []):
                if not (se.FIXTURE_REPO / rel).exists():
                    missing.append(f"{case['id']}: {rel}")
        assert not missing, "expected files not in fixture:\n" + "\n".join(missing)

    def test_gates_are_declared_and_xfails_name_a_phase(self):
        _, cases = se.load_suite(SUITE)
        bad = [c.id for c in cases if c.gate == "xfail" and not c.fixed_by]
        assert not bad, f"xfail cases must say which phase fixes them: {bad}"
        assert any(c.gate == "must_pass" for c in cases)

    def test_baseline_covers_every_case(self):
        _, cases = se.load_suite(SUITE)
        baseline = se.load_baseline(BASELINE)
        missing = sorted({c.id for c in cases} - set(baseline["per_query"]))
        extra = sorted(set(baseline["per_query"]) - {c.id for c in cases})
        assert not missing and not extra, (
            f"baseline out of date (missing={missing}, extra={extra}); run "
            "`c3 search-eval --update-baseline`")


class TestParseHits:
    def test_code_and_semantic_rows(self):
        resp = ("--- cli\\tools\\search.py:L66-104 handle_search (function)\n"
                "def handle_search(): ...\n"
                "--- services/indexer.py:L1-5 (block)\n"
                "[c3-access:limited] footer\n")
        hits = se.parse_hits("code", resp)
        assert [h.file for h in hits] == ["cli/tools/search.py", "services/indexer.py"]
        assert hits[0].name == "handle_search" and hits[0].type == "function"
        assert hits[1].name == "" and hits[1].type == "block"

    def test_files_rows(self):
        resp = ("- services/retrieval_broker.py (L1-129) — contains class 'MemoryRetrievalBroker'\n"
                "  def search()\n"
                "- cli\\tools\\federate.py (L1-120)\n")
        hits = se.parse_hits("files", resp)
        assert [h.file for h in hits] == ["services/retrieval_broker.py", "cli/tools/federate.py"]
        assert hits[0].name == "MemoryRetrievalBroker"

    def test_exact_rows_and_zero_results(self):
        resp = "--- services/watcher.py ---\n L1: x\n>L2: def rebuild_if_needed\n---\n"
        assert [h.file for h in se.parse_hits("exact", resp)] == ["services/watcher.py"]
        assert se.parse_hits("code", "[search:foo] 0 results") == []

    def test_symbol_matching_accepts_qualified_names(self):
        assert se._symbol_matches("Invoice.compute_total", "compute_total")
        assert se._symbol_matches("compute_total", "compute_total")
        assert not se._symbol_matches("compute_totals", "compute_total")


class TestFixtureGate:
    def test_index_built_and_exact_universe_complete(self, report):
        assert report.stats.files_indexed >= 35
        assert report.stats.exact_coverage == 1.0, "fixture file_memory should track every indexed file"

    def test_must_pass_cases_pass(self, report):
        failed = report.aggregates["must_pass_failed"]
        detail = "\n".join(f"{r.id}: {r.reason}" for r in report.results if r.id in failed)
        assert not failed, f"must_pass regressions:\n{detail}"

    def test_no_forbidden_text_leaks(self, report):
        assert not report.aggregates["forbidden_hits"], report.aggregates["forbidden_hits"]

    def test_aggregates_meet_floors(self, report):
        floors = [v for v in report.baseline_violations if "below floor" in v]
        assert not floors, "\n".join(floors)

    def test_xfail_and_regressions_are_visible(self, report):
        for w in report.baseline_warnings:
            warnings.warn(w, stacklevel=1)
        for cid in report.aggregates["xfail_passing"]:
            warnings.warn(f"{cid} is xfail but passes; promote it", stacklevel=1)

    def test_report_renders(self, report):
        text = report.render()
        assert "verdict:" in text
        assert report.suite == "fixture"
        assert Path(report.project_path).name == "repo"
