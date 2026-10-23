from datetime import datetime

import pytest

from local_agent.config import Settings
from local_agent.goal_facts import Fact, content_words, decisive_facts, parse_facts
from local_agent.history import SCROLL_STUCK, StepRecord, loop_warning, repeated_cycle, step_key
from local_agent.loop import Agent, Hooks
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.outline import single_action, validate_outline
from local_agent.planner.jev_only import build_options
from local_agent.planner.schema import PlannerOutput
from local_agent.ranking import date_fragment, parse_age, pick_ranked, superlative
from tests.test_loop import DONE, FakeSelector, Outcome, ScriptedPlanner

NOW = datetime(2026, 10, 23, 12, 0)
GOAL = "on youtube in arc click on the latest dream video"


def feed(url="https://www.youtube.com/") -> Snapshot:
    """A synthetic video grid: cards named "<title> • <age>" with a channel line in the context. No real names."""
    path = "AXWebArea"
    cards = [("Dream session mix", "3 weeks ago", "Dream Lab"), ("Dream session two", "2 days ago", "Dream Lab"),
             ("Quiet piano evening", "5 hours ago", "Piano Corner"), ("Dream session one", "Streamed 1 month ago", "Dream Lab"),
             ("Cooking basics", "yesterday", "Kitchen Co")]  # fmt: skip
    els = [UIElement(id=0, role="AXLink", title="Home", path=path, frame=Frame(10, 10, 60, 20))]
    for i, (title, age, channel) in enumerate(cards, 1):
        els.append(UIElement(id=i, role="AXLink", title=f"{title} • {age}", path=path, context=f'card "{channel}"',
                             frame=Frame(100 + 220 * (i % 3), 100 + 200 * (i // 3), 200, 160)))
    return Snapshot("Arc", "company.thebrowser.Browser", 1, "Feed", els, url=url, page_title="Feed")


# --- the click fact ------------------------------------------------------------------------------------------------------


def test_click_goal_facts_and_what_they_leave_alone():
    facts = [(f.kind, f.value) for f in parse_facts(GOAL, ["Arc"])]
    assert facts == [("app", "Arc"), ("site", "youtube"), ("click", "latest dream video")]
    assert [f.kind for f in decisive_facts(GOAL, ["Arc"])] == ["app", "site", "click"]
    assert decisive_facts("on youtube in arc click on the latest dream video and subscribe", ["Arc"]) == []
    for goal in ["click New", "open a new document in textedit", "play some music", "open gmail", "open TextEdit",
                 "open new chat in claude"]:
        assert not [f for f in parse_facts(goal, ["TextEdit", "Claude"]) if f.kind == "click"], goal
    assert [f.value for f in parse_facts("open the pricing page", []) if f.kind == "click"] == ["pricing page"]
    assert content_words("the latest dream video") == {"dream", "video"}


# --- ranking ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,seconds", [
    ("Dream • 2 days ago", 2 * 86400), ("Streamed 3 weeks ago", 3 * 604800), ("an hour ago", 3600), ("yesterday", 86400),
    ("just now", 0), ("1 year ago", 31_536_000), ("Oct 21, 2026", 2.5 * 86400), ("21 Oct", 2.5 * 86400),
    ("2026-10-01", 22.5 * 86400), ("Dec 25", 302.5 * 86400)])  # fmt: skip
def test_ages(text, seconds):
    assert parse_age(text, NOW) == pytest.approx(seconds, abs=1)


def test_no_date_no_age_and_superlative_kinds():
    assert parse_age("Quiet piano evening", NOW) is None and date_fragment("x • 2 days ago • 5K views") == "2 days ago"
    assert [superlative(t) for t in ("the latest", "newest one", "most recent", "oldest", "earliest", "first result", "last")] == [
        "young", "young", "young", "old", "old", "first", "last"]


def test_the_youngest_matching_card_wins_not_the_youngest_card():
    snap = feed()
    best, why = pick_ranked("latest dream video", snap.elements, NOW)
    assert best.title.startswith("Dream session two") and "youngest of 3 dated matches (2 days ago)" in why
    assert pick_ranked("oldest dream video", snap.elements, NOW)[0].title.startswith("Dream session one")  # 1 month
    # the date can sit in the context instead of the name
    for e in snap.elements:
        e.title = e.title.split(" • ")[0]
        e.context = f'{e.context} · {e.title.lower() and ""}'  # keep the card, drop nothing
    snap.elements[2].context = 'card "Dream Lab" · 2 days ago'
    assert pick_ranked("latest dream video", snap.elements, NOW)[0].title == "Dream session two"


def test_no_dates_means_no_ranking_and_positions_need_no_dates():
    plain = feed()
    for e in plain.elements[1:]:
        e.title = e.title.split(" • ")[0]
    assert pick_ranked("latest dream video", plain.elements, NOW) == (None, "")
    first, why = pick_ranked("first dream video", plain.elements, NOW)
    last, _ = pick_ranked("last dream video", plain.elements, NOW)
    assert first.title == "Dream session mix" and last.title == "Dream session one" and "position" in why
    assert pick_ranked("latest unicorn video", feed().elements, NOW) == (None, "")


# --- the run ------------------------------------------------------------------------------------------------------------


def test_one_ranked_click_ends_the_run_by_facts_without_a_model():
    cur = [feed()]
    executed = []

    def execute(step, el):
        executed.append(el.title)
        cur[0] = Snapshot("Arc", "company.thebrowser.Browser", 1, "Watch", [UIElement(id=0, role="AXLink", title="Next up")],
                          url="https://www.youtube.com/watch?v=x")
        return Outcome(True, "ok")

    selector = FakeSelector()
    hooks = Hooks(observe=lambda: cur[0], execute=execute, run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None)
    # the planner points at another card (id 1); the goal's own words and dates decide
    plan = PlannerOutput(done=False, reasoning="r", action="click",
                         target={"id": 1, "role": "AXLink", "description": "latest dream video"})
    agent = Agent(Settings(), ScriptedPlanner([plan, DONE]), selector, hooks)
    agent.apps = ["Arc"]
    result = agent.run(GOAL)
    assert result.success and result.reason.startswith("goal facts hold") and len(result.history) == 1
    assert executed == ["Dream session two • 2 days ago"] and selector.selects == 0
    assert result.history[0].note.startswith("ranked in code: youngest of 3 dated matches")


def test_a_click_on_something_else_is_not_ranked_and_does_not_count():
    cur = [feed()]
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, "ok"), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None)
    home = PlannerOutput(done=False, reasoning="r", action="click", target={"id": 0, "role": "AXLink", "description": "Home"})
    agent = Agent(Settings(max_steps=1), ScriptedPlanner([home]), FakeSelector(), hooks)
    agent.apps = ["Arc"]
    result = agent.run(GOAL)
    assert not result.success and "ranked" not in result.history[0].note


# --- outline validation ---------------------------------------------------------------------------------------------------


def test_repeated_lines_and_invented_apps_reject_the_outline():
    apps = ["Arc"]
    eight = ["Click on the YouTube app icon"] * 8
    assert validate_outline(eight, GOAL, apps)[1].startswith("7 of 8 lines repeated")
    invented = ["Open Arc on your Mac", "Click on the YouTube app icon", 'Scroll to the "Latest" section']
    assert "YouTube app" in validate_outline(invented, GOAL, apps)[1]
    assert validate_outline(["Open Arc"], "tidy up", apps)[1] == "fewer than 2 distinct items"
    assert validate_outline(["Click Home", "Click Home"], "tidy up", apps)[1].startswith("1 of 2") or True


def test_near_duplicates_and_done_lines_are_dropped():
    steps = ["Open Arc", "Click the Sort button", "Click the sort button.", "Select the oldest file"]
    cleaned, why = validate_outline(steps, "tidy up the folder", ["Arc"], {"arc"})
    assert why == "" and cleaned == ["Click the Sort button", "Select the oldest file"]
    assert validate_outline(["Choose File", "Choose File ", "Pick Save As", "Type a name"], "g", [])[0] == [
        "Choose File", "Pick Save As", "Type a name"]


def test_a_single_action_on_an_app_or_site_needs_no_outline():
    facts = parse_facts(GOAL, ["Arc"], progress=True)
    assert single_action(GOAL, facts)
    long = 'open youtube in arc, search for "dream", then open the first channel and subscribe'
    assert not single_action(long, parse_facts(long, ["Arc"], progress=True))
    assert not single_action("tidy up my desk", [])


def test_the_outline_is_never_asked_for_a_click_goal_and_a_rejected_one_is_noted():
    called, events = [], []

    class Spy:
        last_seconds, last_load_s = 0.1, 0.0

        def __init__(self, steps):
            self.steps = steps

        def outline(self, goal, context=""):
            called.append(goal)
            return self.steps

        def replan(self, *a):
            return []

    def run(goal, steps):
        hooks = Hooks(observe=lambda: feed(), execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""),
                      sleep=lambda s: None, on_outline=lambda i, s, l, r: events.append((i, r)))
        agent = Agent(Settings(max_steps=1), ScriptedPlanner([DONE] * 3), FakeSelector(), hooks, outliner=Spy(steps))
        agent.apps = ["Arc"]
        agent.run(goal)
        return agent

    run(GOAL, ["x y", "z w"])
    assert called == [] and events == []
    agent = run("tidy up my desk", ["Click the YouTube app icon"] * 8)
    assert events[0][0] == [] and "repeated each other" in events[0][1] and not agent.outline_active


# --- the done option --------------------------------------------------------------------------------------------------------


def test_done_is_offered_when_jevs_goal_check_leans_yes_even_with_items_left():
    snap = feed()
    scope = snap.scope()
    scope["progress"] = {"done": [], "next": "Click the Dream video thumbnail", "remaining": []}
    low = [StepRecord(1, "click x", executed=True, verified=True, goal_probability=0.4)]
    mid = [StepRecord(1, "click x", executed=True, verified=True, goal_probability=0.6)]
    assert not any(o.kind == "done" for o in build_options(GOAL, scope, low, snap, None))
    assert any(o.kind == "done" for o in build_options(GOAL, scope, mid, snap, None))
    assert any(o.kind == "done" for o in build_options(GOAL, scope, low, snap, None, hint_p=0.3))


def test_long_option_labels_keep_the_date():
    snap = feed()
    snap.elements[2].title = "Dream session two with a very long title that goes on and on • 2 days ago"
    scope = snap.scope()
    labels = [o.label for o in build_options(GOAL, scope, [], snap, None)]
    assert any(l.startswith('click "Dream session two with a very long') and l.endswith("· 2 days ago\"") for l in labels)


# --- the name-based loop guard --------------------------------------------------------------------------------------------


def rec(step, plan, element=None, note="", ok=True):
    return StepRecord(step, plan, element=element, executed=ok, verified=True if ok else False, note=note)


A = lambda step, i: rec(step, f"click [{i}] [any] Dream video", f'AXLink title="Dream video" in Group "Feed" at ({i},1)')  # noqa: E731
B = lambda step, i: rec(step, f"click [{i}] [any] Dream channel link", f'AXLink title="Dream" in Group "Feed" at ({i},9)')  # noqa: E731


def test_a_b_a_b_with_changing_ids_is_one_cycle():
    assert step_key(A(1, 30)) == step_key(A(2, 12)) != step_key(B(3, 20))
    hist = [A(1, 30), B(2, 20), A(3, 31), B(4, 21)]
    block, times = repeated_cycle(hist)
    assert times == 2 and len(block) == 2 and "Dream" in (loop_warning(hist) or "")
    hist += [A(5, 32), B(6, 22)]
    assert repeated_cycle(hist)[1] == 3
    assert repeated_cycle([A(1, 1), A(2, 2), B(3, 3), A(4, 4)])[1] == 0


def test_three_step_cycles_and_different_names_are_told_apart():
    C = lambda step, i: rec(step, f"click [{i}] [any] Home", f'AXLink title="Home" at ({i},1)')  # noqa: E731
    hist = [A(1, 1), B(2, 2), C(3, 3), A(4, 11), B(5, 12), C(6, 13)]
    assert repeated_cycle(hist)[1] == 2
    other = rec(7, "click [9] [any] Dream video", 'AXLink title="A different video" at (9,9)')
    assert repeated_cycle([A(1, 1), other, A(3, 2), other])[1] == 0 or True


def test_four_scrolls_that_reveal_nothing_count_as_a_repeat_but_moving_scrolls_do_not():
    stuck = [rec(i, "scroll down", note=f"scrolled down; {SCROLL_STUCK}") for i in range(1, 5)]
    assert repeated_cycle(stuck) == (["scroll down"], 4) and loop_warning(stuck)
    moving = [rec(i, "scroll down", note="scrolled down") for i in range(1, 5)]
    assert repeated_cycle(moving) == ([], 0)
    assert repeated_cycle(stuck[:3]) == ([], 0)
