"""Setting facts ("select model to be sonnet 5.5"), a New click that changes nothing, and outline lines tied to facts.
Synthetic Claude-like app: a sidebar "New", an empty focused composer, a "Model: Opus 5.5 High" button."""

import pytest

from local_agent.config import Settings
from local_agent.goal_facts import Fact, decisive_facts, parse_facts, progress_scope, resolve
from local_agent.loop import Agent, Hooks
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.outline import items_from_steps
from local_agent.planner.jev_only import build_options
from local_agent.planner.schema import PlannerOutput
from tests.test_loop import DONE, FakeSelector, Outcome, P, ScriptedPlanner

GOAL = "open new chat in claude, and select model to be sonnet 5.5"
APPS = ["Claude", "TextEdit"]


def el(i, role, title="", **kw):
    return UIElement(id=i, role=role, title=title, frame=Frame(0, i * 30, 120, 24), **kw)


def app(model="Model: Opus 5.5 High", menu=False, composer="", title="Claude"):
    els = [el(1, "AXButton", "New"), el(2, "AXTextArea", value=composer, focused=True), el(3, "AXButton", model)]
    if menu:
        els += [el(10 + i, "AXMenuItem", t, path="AXMenu") for i, t in enumerate(["Opus 5.5", "Sonnet 5.5", "Haiku 5.5"])]
    return Snapshot("Claude", "com.example.claude", 1, title, els)


def click(i, desc):
    return P(action="click", target={"id": i, "role": "AXButton", "description": desc})


class NoOutline:
    def outline(self, *a):
        raise AssertionError("no outline for a goal the facts cover")

    last_seconds = last_load_s = 0.0


def test_the_setting_phrases_parse():
    def settings(goal):
        return [(f.value, f.data) for f in parse_facts(goal, APPS) if f.kind == "setting"]

    assert settings(GOAL) == [("model", "sonnet 5.5")]
    assert settings("set font size to 14") == [("font size", "14")]
    assert settings("choose country Germany") == [("country", "Germany")]
    assert settings("change theme to dark") == [("theme", "dark")]
    assert settings('select the language as "French"') == [("language", "French")]
    assert settings("Model: Opus 5.5") == [("model", "Opus 5.5")]
    assert settings("use dark as the theme") == [("theme", "dark")]
    for goal in ["select all", "set up the printer", "switch to Arc", "choose a file", "open new chat in claude"]:
        assert not settings(goal), goal
    assert sorted(f.kind for f in decisive_facts(GOAL, APPS)) == ["app", "new", "setting"]
    assert [f.kind for f in parse_facts(GOAL, APPS, progress=True)] == ["app", "new", "setting"]


def test_a_setting_holds_when_its_control_shows_the_value():
    fact = Fact("setting", "model", data="sonnet 5.5")
    assert resolve([fact], app()) == [False]
    assert resolve([fact], app("Model: Sonnet 5.5 High")) == [True]
    assert resolve([fact], app("model:  sonnet   5.5")) == [True]
    assert resolve([fact], Snapshot("X", "x", 1, "w", [el(1, "AXButton", "Send")]), known={}) == [False]  # no control: unknown
    checked = el(4, "AXRadioButton", "Sonnet 5.5", value="1", path="AXGroup:Model")
    assert resolve([fact], Snapshot("X", "x", 1, "w", [el(3, "AXButton", "Model"), checked])) == [True]


def test_the_checklist_line_is_read_from_the_screen():
    facts = parse_facts(GOAL, APPS, progress=True)
    nxt = lambda snap: progress_scope(facts, snap, seen={f for f in facts if f.kind == "new"})["next"]  # noqa: E731
    assert nxt(app()) == 'click "Model: Opus 5.5 High", then choose "sonnet 5.5"'
    assert nxt(app(menu=True)) == 'choose "Sonnet 5.5"'
    assert nxt(Snapshot("Claude", "x", 1, "w", [])) == 'set model to "sonnet 5.5"'


def test_jev_mode_offers_the_control_and_then_the_menu_item():
    scope = {"progress": {"done": [], "next": 'click "Model: Opus 5.5 High", then choose "sonnet 5.5"', "remaining": []}}
    labels = [o.label for o in build_options(GOAL, scope, [], app(), None)]
    assert labels[0] == 'click "Model: Opus 5.5 High"'
    scope = {"progress": {"done": [], "next": 'choose "Sonnet 5.5"', "remaining": []}}
    options = build_options(GOAL, scope, [], app(menu=True), None)
    assert options[0].label == 'click "Sonnet 5.5"' and options[0].from_checklist and options[0].plan.target.id == 11


def test_new_then_model_then_item_ends_by_facts_without_an_outline():
    cur = [app()]

    def execute(step, element):
        name = element.name()
        if name.startswith("Model"):
            cur[0] = app(menu=True)
        elif name == "Sonnet 5.5":
            cur[0] = app("Model: Sonnet 5.5 High")
        return Outcome(True, "ok")

    hooks = Hooks(observe=lambda: cur[0], execute=execute, run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None)
    steps = [click(1, "New"), click(3, "Model"), click(11, "Sonnet 5.5"), DONE]
    agent = Agent(Settings(), ScriptedPlanner(steps), FakeSelector(), hooks, outliner=NoOutline())
    agent.apps = APPS
    result = agent.run(GOAL)
    assert result.success and result.reason.startswith("goal facts hold") and len(result.history) == 3
    assert result.history[0].verified and "already on a new chat; nothing changed" in result.history[0].note


def test_new_that_changes_nothing_without_an_empty_state_is_no_fact():
    cur = [app(composer="half-written message")]
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, "ok"), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None)
    agent = Agent(Settings(max_steps=1), ScriptedPlanner([click(1, "New")]), FakeSelector(verify=0.1), hooks)
    agent.apps = APPS
    agent.run(GOAL)
    assert Fact("new", "chat") not in agent.known_facts and Fact("new", "chat") not in agent.seen_facts


OUTLINE = ['Click the "New Chat" button in the top left corner', "Locate the model selector dropdown",
           'Click the dropdown and select "Sonnet 5.5"']


def test_an_outline_line_that_clicks_new_is_the_new_fact():
    items = items_from_steps(OUTLINE, APPS)
    assert items[0] == Fact("new", "chat") and [f.kind for f in items[1:]] == ["outline", "outline"]
    assert items_from_steps(["Click the New button"], APPS)[0].kind == "outline"  # no noun: nothing to map


def test_the_outline_shares_the_goals_new_fact_and_keeps_unrestated_ones():
    class Outliner:
        last_seconds = last_load_s = 0.0

        def outline(self, goal, context):
            return OUTLINE

    goal = "open new chat in claude and ask what the weather is, then summarise it"
    cur = app()
    agent = Agent(Settings(), ScriptedPlanner([]), FakeSelector(), Hooks(observe=lambda: cur, execute=None, run_terminal=None),
                  outliner=Outliner())
    agent.apps = APPS
    agent.facts = decisive_facts(goal, APPS)
    agent.checklist = parse_facts(goal, APPS, progress=True)
    assert [f.kind for f in agent.checklist] == ["app", "new"] and agent.facts == []
    agent._make_outline(goal, cur)
    assert [f.kind for f in agent.checklist] == ["app", "new", "outline", "outline"]
    assert agent.checklist[1] is not None and agent.checklist.count(Fact("new", "chat")) == 1


def test_a_blocked_new_step_never_points_next_at_a_new_line():
    agent = Agent(Settings(), ScriptedPlanner([]), FakeSelector(), Hooks(observe=lambda: app(), execute=None, run_terminal=None))
    new = Fact("new", "chat")
    agent.checklist = [new, Fact("outline", 'Click the "New Chat" button'), Fact("outline", "Pick a model")]
    agent.seen_facts, agent.started = {new}, 0.0
    kind, note = agent._guard(PlannerOutput(done=False, reasoning="r", action="click", target={"id": 1, "role": "AXButton", "description": "New"}), app().elements[0], None, app())
    assert kind == "block" and note.endswith("work on: Pick a model") and "New Chat" not in note
