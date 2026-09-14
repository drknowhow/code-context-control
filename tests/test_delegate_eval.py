"""c3 delegate-eval: the gold suite, its grading, and the tier recommendation.

The replay fixture holds one correct and one plausibly wrong answer per case.
Every correct answer must pass and every wrong one must fail — so a regex
that rejects a right answer, or waves a wrong one through, fails here rather
than quietly skewing a live comparison between tiers.

No model is called: live runs go through a monkeypatched handle_delegate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.bench import delegate_eval as de  # noqa: E402

FIXTURE = de.SUITE_DIR / "replay_fixture.json"


def _case(**over):
    base = {"id": "c", "task_type": "ask", "task": "t", "checks": {"must_match": ["x"]}}
    base.update(over)
    return de.DelegateCase.from_dict(base)


# ── Suite ───────────────────────────────────────────────────────────────────


def test_gold_suite_loads():
    header, cases = de.load_suite(de.BUNDLED_SUITES["gold"])
    assert header["suite"] == "gold"
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)) == 26
    assert sum(1 for c in cases if c.gate == "lookup") == 4
    core_types = {c.task_type for c in cases if c.gate == "core"}
    assert {"summarize", "explain", "docstring", "review", "ask", "test", "diagnose"} <= core_types
    for c in cases:
        if c.gate == "core":
            assert c.context or c.file_path, c.id
            if c.file_path:
                assert (de.PROJECT_DIR / c.file_path).exists(), c.id
        else:
            assert not c.context and not c.file_path


@pytest.mark.parametrize("over, message", [
    ({"gate": "maybe"}, "unknown gate"),
    ({"task_type": "poem"}, "unknown task_type"),
    ({"checks": {"must_match": ["("]}}, "bad must_match regex"),
    ({"checks": {"max_words": 5}}, "needs must_match or any_match"),
    ({"checks": {"must_match": ["x"], "contains": ["y"]}}, "unknown check"),
    ({"gate": "lookup", "context": "leak"}, "gives no context"),
    ({"context_file": "no-such.txt"}, "not found"),
])
def test_case_validation(over, message):
    with pytest.raises(ValueError, match=message):
        _case(**over)


# ── Grading ─────────────────────────────────────────────────────────────────


def test_every_reference_answer_passes_and_every_wrong_answer_fails():
    report = de.run_suite("gold", replay=FIXTURE)
    assert report.mode == "replay"
    by_target = {t: [r for r in report.results if r.target == t] for t in report.targets}
    wrongly_failed = [(r.id, r.reason) for r in by_target["reference"] if r.status != "pass"]
    wrongly_passed = [r.id for r in by_target["wrong"] if r.status == "pass"]
    assert wrongly_failed == []
    assert wrongly_passed == []
    assert report.aggregates["reference"]["pass_rate_core"] == 1.0
    assert report.aggregates["wrong"]["pass_rate_core"] == 0.0


def test_grade_checks():
    case = _case(checks={"must_match": [r"\b7\b"], "any_match": ["retr", "attempt"],
                         "must_not_match": ["maybe"], "max_words": 5})
    assert de.grade(case, "7 retries") == []
    assert de.grade(case, "SEVEN") != []
    assert any("any_match" in f for f in de.grade(case, "7 tries"))
    assert any("must_not_match" in f for f in de.grade(case, "maybe 7 retries"))
    assert any("max_words" in f for f in de.grade(case, "7 retries and then a few more words"))


@pytest.mark.parametrize("status", ["error", "timeout", "blocked", "disabled", "unavailable", "degraded"])
def test_no_answer_statuses_are_errors_not_wrong_answers(status):
    res = de.result_from_answer(_case(), "claude", "[delegate:blocked] x contains x", status, {}, 12)
    assert res.status == "error"
    assert res.checks_failed == []
    assert res.reason.startswith("[delegate:")


def test_confidence_status_counts_as_an_answer():
    res = de.result_from_answer(_case(), "ollama", "x marks it", "medium", {"model": "m"}, 5)
    assert (res.status, res.outcome, res.model) == ("pass", "ok", "m")


def test_empty_answer_is_an_error():
    assert de.result_from_answer(_case(), "t", "  ", "ok", {}, 1).status == "error"


# ── Recommendation ──────────────────────────────────────────────────────────


def _agg(rate, cost):
    return {"by_task_type": {"review": {"n": 10, "passing": int(rate * 10), "pass_rate": rate}},
            "cost_usd_mean": cost}


def test_recommend_picks_the_cheapest_tier_within_tolerance():
    aggs = {"claude:small": _agg(0.8, 0.002), "claude:medium": _agg(0.9, 0.006),
            "claude:large": _agg(0.9, 0.02)}
    row = de.recommend(aggs)["claude"]["review"]
    assert row["target"] == "claude:small"


def test_recommend_skips_a_tier_outside_tolerance():
    aggs = {"claude:small": _agg(0.8, 0.002), "claude:large": _agg(1.0, 0.02)}
    assert de.recommend(aggs)["claude"]["review"]["target"] == "claude:large"


def test_recommend_names_nothing_under_the_floor():
    aggs = {"claude:small": _agg(0.5, 0.002), "claude:large": _agg(0.7, 0.02)}
    row = de.recommend(aggs)["claude"]["review"]
    assert row["target"] is None and row["best_pass_rate"] == 0.7


def test_recommend_unpriced_targets_sort_after_priced_ones():
    aggs = {"codex:small": _agg(0.9, None), "codex:large": _agg(0.9, 0.01)}
    assert de.recommend(aggs)["codex"]["review"]["target"] == "codex:large"


def test_parse_target():
    assert de.parse_target("Claude:Small") == ("claude", "small")
    assert de.parse_target("ollama") == ("ollama", "")
    with pytest.raises(ValueError):
        de.parse_target(":small")


# ── Live mode (handle_delegate stubbed) ─────────────────────────────────────


def _reference_answers():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["targets"]["reference"]


def test_live_run_records_answers_on_a_project_copy(monkeypatch, tmp_path):
    answers = _reference_answers()
    seen = []

    def fake_handle(task, task_type, context, file_path, svc, finalize, backend,
                    allow_write_delegation=False, **kw):
        seen.append((backend, kw.get("tier"), svc.project_path, allow_write_delegation))
        assert Path(svc.project_path, "app", "pricing.py").exists()
        assert Path(svc.project_path).resolve() != de.PROJECT_DIR.resolve()
        rec = answers[task_to_id[task]]
        return finalize("c3_delegate", {**rec["meta"], "backend": backend}, rec["output"], rec["status"])

    _, cases = de.load_suite(de.BUNDLED_SUITES["gold"])
    task_to_id = {c.task: c.id for c in cases}
    monkeypatch.setattr("cli.tools.delegate.handle_delegate", fake_handle)
    monkeypatch.setattr(de, "build_eval_svc",
                        lambda project, overrides=None: SimpleNamespace(project_path=str(project)))
    record = tmp_path / "rec.json"
    progress = []
    report = de.run_suite("gold", targets=["claude:small"], record=record,
                          case_ids=["explain-slice", "pack-pricing-bug"],
                          allow_write_delegation=True, progress=progress.append)

    assert [r.status for r in report.results] == ["pass", "pass"]
    assert len(progress) == 2
    assert {s[0] for s in seen} == {"claude"} and {s[1] for s in seen} == {"small"}
    assert all(s[3] is True for s in seen)
    saved = json.loads(record.read_text(encoding="utf-8"))
    assert set(saved["targets"]["claude:small"]) == {"explain-slice", "pack-pricing-bug"}
    assert not Path(seen[0][2]).exists()  # the project copy is removed afterwards


def test_live_run_before_tiers_exist_reports_an_error(monkeypatch):
    def old_handle(task, task_type, context, file_path, svc, finalize, backend="ollama",
                   allow_write_delegation=False):
        return finalize("c3_delegate", {}, "x", "ok")

    monkeypatch.setattr("cli.tools.delegate.handle_delegate", old_handle)
    monkeypatch.setattr(de, "build_eval_svc",
                        lambda project, overrides=None: SimpleNamespace(project_path=str(project)))
    report = de.run_suite("gold", targets=["claude:small"], case_ids=["ask-config-merge"])
    (res,) = report.results
    assert res.status == "error" and "no tiers" in res.reason


def test_run_suite_rejects_unknown_cases_and_empty_targets():
    with pytest.raises(ValueError, match="unknown case"):
        de.run_suite("gold", replay=FIXTURE, case_ids=["nope"])
    with pytest.raises(ValueError, match="at least one target"):
        de.run_suite("gold")


def test_render_and_json():
    report = de.run_suite("gold", replay=FIXTURE)
    text = report.render()
    assert "suite=gold mode=replay" in text
    assert "reference: core pass=1.0" in text
    data = report.to_dict()
    assert data["aggregates"]["wrong"]["failing_core"]
    json.dumps(data)


def test_cli_floor_exits_nonzero(capsys):
    from cli.c3 import cmd_delegate_eval

    args = SimpleNamespace(suite="gold", targets="wrong", cases="", replay=str(FIXTURE), record=None,
                           floor=0.8, allow_write_delegation=False, json=False)
    with pytest.raises(SystemExit) as exc:
        cmd_delegate_eval(args)
    assert exc.value.code == 1
    assert "under floor 0.8: wrong" in capsys.readouterr().err

    args.targets = "reference"
    cmd_delegate_eval(args)
    assert "reference: core pass=1.0" in capsys.readouterr().out
