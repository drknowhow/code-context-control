"""The write-mode eval grades itself: every case passes on its reference change
(fixtures/write_gold) and fails on the untouched fixture. Without this a check
that can never fail — a typo'd path, a mutant whose code is gone — would report
a model as passing. No model is called."""
from __future__ import annotations

import pytest

from services.bench import delegate_write_eval as we

_, CASES = we.load_write_suite()


def test_suite_shape():
    assert len(CASES) >= 10
    for case in CASES:
        assert (we.GOLD_DIR / case.id).is_dir(), case.id


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_gold_passes_and_untouched_fails(case, tmp_path):
    gold, _ = we.run_case(case, "gold", tmp_path)
    assert gold.passed, gold.reasons
    untouched, _ = we.run_case(case, "none", tmp_path)
    assert not untouched.passed


def test_a_change_outside_the_write_set_fails(tmp_path):
    case = next(c for c in CASES if c.id == "fix-rounding")
    work = we._fresh_copy(tmp_path)
    (work / "inv" / "report.py").write_text("# touched\n", encoding="utf-8")
    assert "changed outside the write set: inv/report.py" in we.grade(case, work)


def test_worker_report_section():
    text = "[delegate:write] H\n\n--- worker report ---\nI could not edit report.py\n\n--- diff ---\n+x"
    assert we.worker_report(text) == "I could not edit report.py"
    assert we.worker_report("no markers") == ""


def test_case_validation():
    with pytest.raises(ValueError, match="needs write_paths"):
        we.WriteCase.from_dict({"id": "x", "task": "t", "checks": {"pytest": ["tests"]}})
    with pytest.raises(ValueError, match="unknown check"):
        we.WriteCase.from_dict({"id": "x", "task": "t", "write_paths": "a.py", "checks": {"nope": 1}})
    with pytest.raises(ValueError, match="go together"):
        we.WriteCase.from_dict({"id": "x", "task": "t", "write_paths": "a.py",
                                "checks": {"pytest": ["tests"], "mutants": [{}]}})
