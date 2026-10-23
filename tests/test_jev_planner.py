import pytest

from evals.planner_cases import fixture, te_overwrite_alert
from evals.web_snapshots import web_snapshot
from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.observe.screen_view import render
from local_agent.planner.jev_only import NO_SHELL, JevPlanner, build_options
from local_agent.planner.qwen import PlannerError, make_planner
from local_agent.selector.cosine import CosineClient
from local_agent.selector.jev import ChoiceAnswer, JevError


def labels(options):
    return [o.label for o in options]


def opts(goal, snap, progress=None, history=(), menus=False):
    scope = snap.scope()
    if progress:
        scope["progress"] = progress
    return build_options(goal, scope, list(history), snap, render(snap, 3000), menus)


def test_save_sheet_offers_the_field_the_checklist_names_and_the_save_button():
    snap = fixture("te_save_sheet")
    field = next(e for e in snap.elements if e.role == "AXTextField" and e.focused)
    name = field.name() or field.label
    progress = {"done": ["make a new empty document"], "next": f'type "notes" into the "{name}" field', "remaining": []}
    options = opts('Save this document as "notes"', snap, progress)
    first = options[0]
    assert first.label == f'type "notes" into "{name}"' and first.plan.target.id == field.id and first.plan.text == "notes"
    assert any(l == 'click "Save"' for l in labels(options))
    assert labels(options)[-1].startswith("none of these")
    assert not any(o.plan and o.plan.action == "terminal" for o in options)
    assert [o.key for o in options] == [f"o{i}" for i in range(len(options))]  # unique keys, stable order


def test_search_field_goal_gets_type_and_press_return():
    snap = web_snapshot("web_search_field")
    progress = {"done": [], "next": 'type "llama" into the "Search the docs" field, then press return', "remaining": []}
    options = opts('Search the docs for "llama"', snap, progress)
    assert labels(options)[:2] == ['type "llama" into "Search the docs"', "press return"]
    assert options[0].plan.target.id is not None and options[1].plan.keys == "return"
    # the goal's quoted string is also offered for every text field, and press escape / tab exist
    assert 'press escape' in labels(options) and 'press tab' in labels(options)


def test_overwrite_alert_buttons_are_options():
    snap = te_overwrite_alert()
    progress = {"done": [], "next": 'answer the alert: "helloworld.txt already exists": click "Replace" to overwrite or "Cancel"',
                "remaining": []}
    options = opts('Save this document as "helloworld.txt"', snap, progress)
    clicks = labels(options)
    assert 'click "Replace"' in clicks and 'click "Cancel"' in clicks
    replace = next(o for o in options if o.label == 'click "Replace"')
    assert replace.plan.action == "click" and replace.plan.target.id == 3


def test_a_new_thing_goal_offers_cmd_n_and_done_only_when_nothing_is_left():
    snap = fixture("te_plain")
    nxt = opts("make a new document", snap, {"done": [], "next": "make a new empty document", "remaining": []})
    assert "press cmd+n (new)" in labels(nxt) and not any(o.kind == "done" for o in nxt)
    finished = opts("make a new document", snap, {"done": ["make a new empty document"], "next": None, "remaining": []})
    assert finished[0].kind == "done" and finished[0].plan.done
    # no checklist: done only after a step worked
    assert not any(o.kind == "done" for o in opts("g", snap))
    worked = opts("g", snap, history=[StepRecord(1, "click x", executed=True, verified=True)])
    assert any(o.kind == "done" for o in worked)


def test_scroll_only_when_the_view_says_there_is_more():
    assert not any(o.kind == "scroll" for o in opts("g", web_snapshot("web_icon_buttons")))
    assert {"scroll down", "scroll up"} <= set(labels(opts("open the Pricing page", web_snapshot("web_row_actions"))))


def test_menu_commands_only_when_menu_actions_are_on():
    snap = fixture("te_plain")
    scope = snap.scope()
    scope["menu_commands"] = ["File > Save", "Edit > Select All"]
    assert not any(o.kind == "menu" for o in build_options("g", scope, [], snap, None, menus=False))
    assert 'menu "File > Save"' in labels(build_options("g", scope, [], snap, None, menus=True))


def test_duplicate_names_say_where_each_is():
    snap = web_snapshot("web_form_labels")
    cities = [l for l in labels(opts('Type "Berlin" into the billing City field', snap)) if l.startswith('click "City"') or 'City' in l and l.startswith("type")]
    assert len(set(cities)) == len(cities) and any("Billing" in l for l in cities)


# --- the planner ---------------------------------------------------------------------------------------------------


class Pick:
    """A selector whose pick_option answers with the option whose label starts with `want`."""

    def __init__(self, want, p=0.9, raises=False, none=False):
        self.want, self.p, self.raises, self.none, self.seen = want, p, raises, none, []

    def pick_option(self, state, instructions, options):
        self.seen.append((state, instructions, options))
        if self.raises:
            raise JevError("llama-server is down")
        if self.none:
            return None
        key = next(k for k, v in options.items() if v.startswith(self.want))
        return ChoiceAnswer(key, self.p, self.p, {key: self.p})


def planner(selector):
    return JevPlanner(Settings(planner_mode="jev"), selector)


def step(sel, snap=None, progress=None, history=()):
    snap = snap or fixture("te_save_sheet")
    scope = snap.scope()
    if progress:
        scope["progress"] = progress
    return planner(sel).next_step("Save this document as notes", scope, list(history), render(snap, 3000), snap=snap)


def test_the_chosen_option_becomes_the_planner_output():
    sel = Pick('click "Save"')
    out = step(sel)
    assert out.action == "click" and out.target.id is not None and out.reasoning.startswith('Jev chose: click "Save"')
    state, question, options = sel.seen[0]
    assert "User goal: Save this document as notes" in state and "Work on the PROGRESS" in question
    assert len(state) < 4000 and all(k.startswith("o") for k in options)


def test_none_of_these_fails_the_step_with_the_shell_note():
    with pytest.raises(PlannerError, match="needs a shell command"):
        step(Pick("none of these"))
    assert "shell command" in NO_SHELL


def test_cosine_and_errors_fail_without_a_model_fallback(monkeypatch):
    cos = CosineClient(Settings(jev="cosine-nomic"))
    with pytest.raises(PlannerError, match="can't choose the next action"):
        step(cos)
    with pytest.raises(PlannerError, match="Jev failed"):
        step(Pick("x", raises=True))
    with pytest.raises(PlannerError):
        step(Pick("x", none=True))


def test_a_doubtful_done_gives_way_to_the_runner_up():
    snap = fixture("te_plain")
    finished = {"done": ["x"], "next": None, "remaining": []}

    class Doubtful(Pick):
        def pick_option(self, state, instructions, options):
            keys = list(options)
            return ChoiceAnswer(keys[0], 0.4, 0.4, {keys[0]: 0.4, keys[1]: 0.35})

    out = step(Doubtful(""), snap, finished)
    assert not out.done  # o0 was "done" at p 0.4 < 0.6


def test_make_planner_wiring():
    s = Settings(planner_mode="jev")
    assert isinstance(make_planner(s, Pick("x")), JevPlanner)
    with pytest.raises(ValueError, match="needs the Jev selector"):
        make_planner(s)
    assert make_planner(s, Pick("x")).needs_snapshot


def test_the_agent_hands_the_snapshot_to_a_planner_that_wants_it(textedit):
    from local_agent.loop import Agent, Hooks
    from tests.test_loop import DONE, FakeSelector, Outcome

    got = []

    class Wants:
        needs_snapshot = True

        def next_step(self, goal, scope, history, screen=None, snap=None):
            got.append(snap)
            return DONE

    hooks = Hooks(observe=lambda: textedit, execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None)
    Agent(Settings(), Wants(), FakeSelector(), hooks).run("g")
    assert got == [textedit]


def test_planner_cases_report_what_the_options_cannot_express():
    from evals.planner_cases import CASES

    na = {c.name: c.jev_na() for c in CASES if c.jev_na()}
    assert na["list_downloads"] == "shell command" and na["read_free_space"] == "free-text answer"
    assert na["select_all"] == "shortcut" and "close_menu" not in na and "save_confirm" not in na
