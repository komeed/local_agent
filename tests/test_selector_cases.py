"""Pin the selector eval: the new pre-filter gets every case to Jev, the previous one misses the known ones."""

import pytest

from evals.selector_cases import CASES, PICK_CASES
from evals.selector_eval import passed, pick_run, prefilter_run
from local_agent.config import Settings
from tests.test_jev import fake_jev

BY_NAME = {c.name: c for c in CASES}
# Rows past the 32nd, dropped by the old top_k cut in document order; and the over-cap tie it never reported.
BASELINE_MISSES = {"invoice_download_49", "arc_close_github", "unnamed_delete_200"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_new_prefilter_passes(case):
    run = prefilter_run(case, "new", Settings())
    assert passed(case, run)
    assert len(run.kept) <= Settings().prefilter_max_k


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_baseline_misses_are_the_known_ones(case):
    assert passed(case, prefilter_run(case, "baseline", Settings())) == (case.name not in BASELINE_MISSES)


@pytest.mark.parametrize("name", ["invoice_delete_25", "form_billing_city", "arc_close_github"])
def test_jev_sees_the_target_by_its_surroundings(name):
    """Jev's option text for the right element names its row or label, so it's distinguishable without positions."""
    case = BY_NAME[name]
    snap = case.snap()
    want = {e.id for e in snap.elements if case.expect(e)}
    jev, requests = fake_jev(lambda names: next((n for n in names if int(n[1:]) in want), names[0]))
    sel = jev.select(case.goal, snap, case.step)
    assert sel.element.id in want
    options = {k: v for q in requests[0]["questions"].values() for k, v in q["criteria"].items()}
    text = options[f"e{sel.element.id}"]
    assert ("row " in text or "label " in text) and prefilter_run(case, "new", Settings()).unique


@pytest.mark.parametrize("case", PICK_CASES, ids=lambda c: c.name)
def test_guard_on_planner_numbers(case):
    row = pick_run(case)
    assert row["to_jev"] == case.expect_jev
    if not row["to_jev"]:
        assert row["correct"]
