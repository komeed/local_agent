import json

import httpx
import pytest

from evals.planner_cases import fixture
from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.observe.screen_view import render
from local_agent.planner.choice import ChoicePlanner
from local_agent.planner.kind import KindPlanner, kind_state, pick_kind
from local_agent.planner.qwen import make_planner
from local_agent.selector.jev import STEP_KINDS, STEP_KINDS_OLD, ChoiceAnswer, JevError
from tests.test_jev import fake_jev


class Kinds:
    """Selector stand-in answering step_kind with fixed probabilities (or None / an error)."""

    def __init__(self, probs=None, error=False):
        self.probs, self.error, self.states = probs, error, []

    def step_kind(self, state):
        self.states.append(state)
        if self.error:
            raise JevError("llama-server down")
        if self.probs is None:
            return None
        best = max(self.probs, key=self.probs.get)
        return ChoiceAnswer(best, self.probs[best], 0.5, self.probs)


def _planner(selector, responses):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"message": {"content": responses.pop(0)}, "prompt_eval_count": 300})

    client = httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return KindPlanner(Settings(planner_mode="kind"), selector, client=client), calls


@pytest.fixture
def sheet():
    snap = fixture("te_save_sheet")
    return snap, render(snap, 3000)


def _sent(calls) -> str:
    return "\n".join(m["content"] for c in calls for m in c["messages"])


def test_terminal_kind_gets_no_screen_view(sheet):
    snap, screen = sheet
    sel = Kinds({"terminal": 0.8, "click": 0.2})
    planner, calls = _planner(sel, ['{"command": "open -a Safari"}'])
    step = planner.next_step("open Safari", snap.scope(), [], screen)
    assert (step.action, step.command) == ("terminal", "open -a Safari")
    assert len(calls) == 1  # one detail question, no kind question to the planner
    assert "SCREEN:" not in _sent(calls) and "TOP OF SCREEN" not in _sent(calls)
    assert "Top of the screen" in sel.states[0]  # Jev still reads the screen to pick the kind
    assert planner.last_kind == ("terminal", 0.8) and "Jev chose terminal" in step.reasoning


def test_click_kind_gets_the_screen_view(sheet):
    snap, screen = sheet
    save = next(e for e in snap.elements if e.role == "AXButton" and e.name() == "Save").id
    planner, calls = _planner(Kinds({"click": 0.7, "type": 0.3}), [json.dumps({"element": "Save (button)"})])
    step = planner.next_step("save it", snap.scope(), [], screen)
    assert (step.action, step.target.id) == ("click", save)
    assert "SCREEN:" in calls[0]["messages"][-1]["content"]


def test_unsure_done_takes_the_runner_up(sheet):
    snap, screen = sheet
    planner, _ = _planner(Kinds({"done": 0.5, "shortcut": 0.4, "click": 0.1}), ['{"keys": "return"}'])
    assert planner.next_step("save it", snap.scope(), [], screen).keys == "return"
    assert pick_kind({"done": 0.7, "click": 0.3}) == ("done", 0.7)


@pytest.mark.parametrize("sel", [Kinds(None), Kinds(error=True)])
def test_falls_back_to_the_planner_choosing(sel, sheet):
    snap, screen = sheet
    planner, calls = _planner(sel, ['{"kind": "shortcut"}', '{"keys": "cmd+s"}'])
    assert planner.next_step("save it", snap.scope(), [], screen).keys == "cmd+s"
    assert calls[0]["format"]["properties"]["kind"]["enum"][0] == "done" and planner.last_kind is None


def test_kind_state_has_goal_steps_and_note(textedit):
    scope = {**textedit.scope(), "note": "open the app the goal needs"}
    h = [StepRecord(1, "shortcut keys=cmd+a", executed=True, verified=True)]
    state = kind_state("make it bold", scope, h, None)
    assert "User goal: make it bold" in state and "cmd+a -> success" in state
    assert "Note: open the app" in state and "Top of the screen" not in state


def test_jev_step_kind_question():
    jev, requests = fake_jev(lambda names: "terminal")
    ans = jev.step_kind("User goal: open Safari")
    assert ans.choice == "terminal" and ans.probability == 0.9
    q = requests[0]["questions"]["kind"]
    assert q["type"] == "choice" and list(q["criteria"]) == list(STEP_KINDS_OLD) and "menu" not in q["criteria"]
    jev.settings.menu_actions = True
    jev.step_kind("User goal: copy")
    assert list(requests[1]["questions"]["kind"]["criteria"]) == list(STEP_KINDS)


def test_make_planner_kind_needs_selector():
    sel = Kinds({"click": 1.0})
    assert type(make_planner(Settings(planner_mode="kind"), sel)) is KindPlanner
    assert isinstance(make_planner(Settings(planner_mode="kind"), sel), ChoicePlanner)
    with pytest.raises(ValueError):
        make_planner(Settings(planner_mode="kind"))


def test_menu_actions_are_off_by_default_in_every_mode():
    from local_agent.planner.choice import KINDS, MENU_KINDS
    from local_agent.selector.jev import step_kinds

    for mode in ("json", "choice", "kind"):
        assert not Settings(planner_mode=mode).menus_on()
        assert Settings(planner_mode=mode, menu_actions=True).menus_on()
    assert step_kinds(Settings(planner_mode="kind")) is STEP_KINDS_OLD
    assert "menu" in step_kinds(Settings(planner_mode="kind", menu_actions=True)) and "menu" not in KINDS
    assert "menu" in MENU_KINDS
    assert not ChoicePlanner(Settings(planner_mode="kind")).menus
    assert ChoicePlanner(Settings(planner_mode="kind", menu_actions=True)).menus
