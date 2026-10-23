"""A goal that names an open tab ("open my simpsons tab on arc"): the tab fact, the code-side switch, goal-word click
ranking in jev mode, the outline skip and the low-confidence rule. Synthetic Arc-like snapshots; nothing live."""

from local_agent.act.browser import TabInfo
from local_agent.config import Settings
from local_agent.goal_facts import decisive_facts, parse_facts, tab_matches
from local_agent.loop import Agent, Hooks
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.outline import single_action
from local_agent.planner.jev_only import JevPlanner, Unsure, build_options
from local_agent.selector.jev import ChoiceAnswer
from tests.test_loop import DONE, FakeSelector, Outcome, P, ScriptedPlanner

GOAL = "Open my simpsons tab on arc"


def button(i, title, path="AXWindow > AXGroup"):
    return UIElement(id=i, role="AXButton", title=title, frame=Frame(0, i * 20, 100, 18), path=path)


def arc(title="repo - code host", url="https://code.example.org/o/r", tabs=("The Simpsons - StreamSite", "Docs - Example")):
    page = [button(1, "Open quick search dialog…", "AXWebArea"), button(2, "Clear search", "AXWebArea")]
    side = [button(10 + i, t) for i, t in enumerate(tabs)]
    return Snapshot("Arc", "company.thebrowser.Browser", 1, title, page + side, url=url, page_title=title)


SIMPSONS = TabInfo(1, 2, "T2", "https://streamsite.example/shows/s", "The Simpsons - StreamSite")
OTHER = TabInfo(1, 1, "T1", "https://code.example.org/o/r", "repo - code host")


class FakeBridge:
    def __init__(self, tabs, on_select=None):
        self.tabs, self.selected, self.on_select = tabs, [], on_select

    def bundle_for(self, name):
        return "company.thebrowser.Browser" if "arc" in name.lower() else None

    def list_tabs(self, bundle):
        return list(self.tabs)

    def select_tab(self, bundle, tab):
        self.selected.append(tab)
        if self.on_select:
            self.on_select(tab)


class NoOutline:
    def outline(self, *a):
        raise AssertionError("no outline for a goal the facts cover")

    last_seconds = last_load_s = 0.0


def run(tabs, steps=(), selector=None, goal=GOAL, outliner=None):
    cur = [arc()]

    def select(tab):
        cur[0] = arc(tab.title, tab.url)

    bridge = FakeBridge(tabs, select)
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, "x"), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, browser=bridge)
    planner = ScriptedPlanner(list(steps))
    agent = Agent(Settings(), planner, selector or FakeSelector(), hooks, outliner=outliner)
    agent.apps = ["Arc", "Safari"]
    return agent.run(goal), bridge, planner


def test_the_goal_parses_to_an_app_and_a_tab_fact():
    assert [(f.kind, f.value) for f in parse_facts(GOAL, ["Arc"])] == [("app", "Arc"), ("tab", "simpsons")]
    assert [f.kind for f in decisive_facts(GOAL, ["Arc"])] == ["app", "tab"]
    assert [f.value for f in parse_facts('switch to the "Docs" tab', ["Arc"]) if f.kind == "tab"] == ["docs"]
    for goal in ["open a new tab in Safari", "go to the next tab", "open the last tab"]:
        assert not [f for f in parse_facts(goal, ["Safari"]) if f.kind == "tab"], goal
    assert decisive_facts(GOAL + ' and type "x"', ["Arc"]) != [] and decisive_facts(GOAL + " then close it", ["Arc"]) == []


def test_tab_words_match_title_or_address_by_whole_word_or_prefix():
    assert tab_matches("simpsons", "The Simpsons - StreamSite")
    assert tab_matches("simpson", "The Simpsons - StreamSite") and not tab_matches("sim", "The Simpsons")
    assert tab_matches("streamsite simpsons", "The Simpsons", "https://www.streamsite.example/x")
    assert not tab_matches("simpsons docs", "The Simpsons - StreamSite")


def test_one_matching_tab_is_selected_in_code_and_the_run_ends_by_fact():
    result, bridge, planner = run([OTHER, SIMPSONS], outliner=NoOutline())
    assert bridge.selected == [SIMPSONS]
    assert result.success and result.reason.startswith("goal facts hold") and len(result.history) == 1
    assert result.history[0].note.endswith('switched to the "The Simpsons - StreamSite" tab') and not planner.seen


def test_several_matching_tabs_go_to_jev_and_the_choice_is_used():
    second = TabInfo(1, 3, "T3", "https://tv.example/simpsons-movie", "The Simpsons Movie")

    class Picker(FakeSelector):
        def pick_option(self, state, instructions, options):
            assert list(options.values()) == ["The Simpsons - StreamSite", "The Simpsons Movie"]
            return ChoiceAnswer("t1", 0.8, 0.5, {"t0": 0.2, "t1": 0.8})

    result, bridge, _ = run([OTHER, SIMPSONS, second], selector=Picker())
    assert bridge.selected == [second] and "Jev chose among 2" in result.history[0].note
    # a backend that can't choose (cosine) takes the first, front window first
    class Cosine(FakeSelector):
        def pick_option(self, *a):
            return None

    _, bridge, _ = run([OTHER, SIMPSONS, second], selector=Cosine())
    assert bridge.selected == [SIMPSONS]


def test_no_matching_tab_leaves_a_note_and_the_planner_continues_without_opening_anything():
    result, bridge, planner = run([OTHER], steps=[DONE] * 3)
    assert bridge.selected == [] and planner.seen
    assert result.history[0].note.endswith('no open tab matches "simpsons"') and not result.history[0].executed


def test_goal_words_beat_the_checklist_line_in_the_click_options():
    filler = [button(30 + i, f"Page control {i}", "AXWebArea") for i in range(30)]
    snap = arc()
    snap.elements = filler + snap.elements
    progress = {"done": [], "next": 'type "foo" into the "Search" field', "remaining": []}
    labels = [o.label for o in build_options(GOAL, {"progress": progress}, [], snap, None)]
    assert 'click "The Simpsons - StreamSite"' in labels
    first_click = next(l for l in labels if l.startswith("click"))
    assert first_click.startswith('click "The Simpsons') or 'click "The Simpsons - StreamSite"' in labels[:14]


def test_a_control_named_with_a_goal_word_is_kept_beyond_the_cut():
    many = [button(100 + i, f"Simpsons clip {i} of the series", "AXWebArea") for i in range(20)]
    snap = arc()
    snap.elements = many + snap.elements
    labels = [o.label for o in build_options("Open my simpsons tab on arc", {}, [], snap, None)]
    assert sum("Simpsons" in l for l in labels) >= 21


def test_the_outline_is_skipped_for_a_single_switch_goal():
    assert single_action("Switch to the pricing window", [])
    assert not single_action("Switch to the pricing window, then copy its text", [])
    assert not single_action("Summarise the inbox", [])


class Answers:
    def __init__(self, probabilities):
        self.p = probabilities

    def pick_option(self, state, instructions, options):
        keys = list(options)
        probs = {k: self.p.get(i, 0.01) for i, k in enumerate(keys)}
        top = max(probs, key=probs.get)
        return ChoiceAnswer(top, probs[top], 0.5, probs)


def test_a_low_probability_pick_off_the_checklist_does_nothing():
    snap = arc()
    scope = {"progress": {"done": [], "next": "something else", "remaining": []}}
    options = build_options(GOAL, scope, [], snap, None)
    planner = JevPlanner(Settings(), Answers({0: 0.12}))
    try:
        planner.next_step(GOAL, scope, [], None, snap)
        raise AssertionError("should be unsure")
    except Unsure as e:
        assert str(e).startswith("unsure (p=0.12): no option clearly fits; ") and options[0].label in str(e)
    assert JevPlanner(Settings(jev_min_pick_p=0.0), Answers({0: 0.12})).next_step(GOAL, scope, [], None, snap).action
    # the checklist's own next line is acted on whatever its probability
    scope = {"progress": {"done": [], "next": 'type "x" into the "Search" field, then press return', "remaining": []}}
    listed = build_options(GOAL, scope, [], snap, None)
    assert next(o for o in listed if o.label == "press return").from_checklist


class UnsurePlanner:
    def __init__(self):
        self.calls = 0

    def next_step(self, *a, **k):
        self.calls += 1
        raise Unsure("unsure (p=0.10): no option clearly fits; click \"A\"; click \"B\"")


def test_the_second_unsure_step_in_a_row_ends_the_run():
    snap = arc()
    hooks = Hooks(observe=lambda: snap, execute=lambda s, e: Outcome(True, "x"), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None)
    planner = UnsurePlanner()
    agent = Agent(Settings(), planner, FakeSelector(), hooks)
    agent.apps = ["Arc"]
    result = agent.run("Summarise what is on this page")
    assert not result.success and result.reason == "unsure: Summarise what is on this page" and planner.calls == 2
    assert [h.note.startswith("unsure (p=0.10)") for h in result.history] == [True, True]
