import pytest
from pydantic import ValidationError

from local_agent.planner.schema import PlannerOutput


def test_terminal_requires_command():
    with pytest.raises(ValidationError, match="command"):
        PlannerOutput(done=False, reasoning="x", action="terminal")
    with pytest.raises(ValidationError, match="command"):
        PlannerOutput(done=False, reasoning="x", action="terminal", command="   ")
    step = PlannerOutput(done=False, reasoning="x", action="terminal", command="open -a TextEdit")
    assert not step.needs_jev()


def test_click_requires_target():
    with pytest.raises(ValidationError, match="target"):
        PlannerOutput(done=False, reasoning="x", action="click")


def test_type_without_target_skips_jev():
    step = PlannerOutput(done=False, reasoning="x", action="type", text="hello")
    assert not step.needs_jev()
    step = PlannerOutput(
        done=False, reasoning="x", action="type", text="hi", target={"role": "AXTextField", "description": "To"}
    )
    assert step.needs_jev()


def test_done_needs_nothing_else():
    assert PlannerOutput(done=True, reasoning="finished").summary() == "done"


def test_not_done_requires_action():
    with pytest.raises(ValidationError):
        PlannerOutput(done=False, reasoning="x")
