import json

import httpx
import pytest

from local_agent.config import Settings
from local_agent.goal_facts import Fact, progress_scope
from local_agent.loop import Agent, Hooks
from local_agent.outline import MAX_REPLANS, Outliner, items_from_steps, parse_outline, plan_matches
from local_agent.planner.jev_only import NO_SHELL
from local_agent.planner.qwen import PlannerError
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import Selection
from tests.test_loop import DONE, FakeSelector, Outcome, ScriptedPlanner


def test_numbered_lines_become_steps():
    text = "Here is a plan:\n1. Open Arc.\n2) **Go to** youtube\n- Search for \"dream\"\n\n4. Pick the first video"
    assert parse_outline(text) == ["Open Arc", "Go to youtube", 'Search for "dream"', "Pick the first video"]
    assert len(parse_outline("\n".join(f"{i}. step {i}" for i in range(1, 15)))) == 8


def test_known_facts_are_detected_and_the_rest_are_outline_items():
    items = items_from_steps(["Open Arc", "Go to youtube", 'Search for "dream"', "Pick the first video"], ["Arc"])
    assert [(f.kind, f.value) for f in items] == [("app", "Arc"), ("site", "youtube"), ("search", "dream"),
                                                  ("outline", "Pick the first video")]
    assert [f.pos for f in items] == [0, 1, 2, 3]


def test_outline_items_show_as_the_checklist_next_line():
    from local_agent.observe.models import Snapshot

    items = items_from_steps(["Click the Downloads folder", "Sort by date"], [])
    snap = Snapshot("Finder", "x", 1, "w", [])
    assert progress_scope(items, snap)["next"] == "Click the Downloads folder"
    done = progress_scope(items, snap, known={items[0]: True})
    assert done["done"] == ["Click the Downloads folder"] and done["next"] == "Sort by date"


def test_a_verified_step_naming_the_item_is_the_cosine_fallback():
    from local_agent.history import StepRecord

    rec = StepRecord(1, "click [AXOutlineRow] Downloads", element='AXRow title="Downloads"', executed=True, verified=True)
    assert plan_matches("Click the Downloads folder", rec) and not plan_matches("Sort by date", rec)


# --- the model call ---------------------------------------------------------------------------------------------------


def mock_client(bodies, content="1. Open Arc\n2. Go to youtube\n3. Search for dream", load_ns=1_500_000_000):
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": content}, "load_duration": load_ns})

    return httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))


def test_low_memory_unloads_the_model_after_each_call_and_reports_the_load_time():
    bodies = []
    out = Outliner(Settings(low_memory=True), mock_client(bodies))
    assert out.outline("find a video") == ["Open Arc", "Go to youtube", "Search for dream"]
    out.replan("find a video", ["open Arc"], "go to youtube", "1. x -> FAILED")
    assert [b["keep_alive"] for b in bodies] == [0, 0] and out.last_load_s == 1.5
    assert out.calls[0]["keep_alive"] == 0 and bodies[0]["model"] == Settings().planner_model
    assert "Goal: find a video" in bodies[0]["messages"][1]["content"]
    assert "Failed step: go to youtube" in bodies[1]["messages"][1]["content"]
    bodies.clear()
    Outliner(Settings(low_memory=False), mock_client(bodies)).outline("g")
    assert "keep_alive" not in bodies[0]


# --- the loop ---------------------------------------------------------------------------------------------------------


class FakeOutliner:
    def __init__(self, first, revised):
        self.first, self.revised, self.replans, self.last_seconds, self.last_load_s = first, list(revised), [], 0.4, 0.2

    def outline(self, goal, context=""):
        return self.first

    def replan(self, goal, done, failed, history):
        self.replans.append((done, failed, history))
        return self.revised.pop(0) if self.revised else []


class NameSelector(FakeSelector):
    def select(self, goal, snap, step):
        e = next(x for x in snap.elements if x.name().lower() == step.target.description.lower())
        return Selection(e, 0.95, 0.95, 3, 1)


def click(name):
    return PlannerOutput(done=False, reasoning="r", action="click", target={"role": "AXButton", "description": name})


def agent_for(textedit, steps, outliner, executes, **settings):
    events = {"outline": [], "replan": []}
    hooks = Hooks(observe=lambda: textedit, execute=lambda s, e: Outcome(*executes.pop(0)), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, on_outline=lambda i, s, l, r: events["outline"].append(i),
                  on_replan=lambda r, i, x: events["replan"].append((r, i)))  # fmt: skip
    agent = Agent(Settings(**settings), ScriptedPlanner(steps), NameSelector(), hooks, outliner=outliner)
    agent.apps = []
    return agent, events


def two_buttons(textedit):
    """Three named buttons the scripted planner can click: Downloads, Sort, Header."""
    from local_agent.observe.models import Frame, UIElement

    base = max(e.id for e in textedit.elements) + 1
    for i, name in enumerate(["Downloads", "Sort", "Header"]):
        textedit.elements.append(UIElement(id=base + i, role="AXButton", title=name, path="AXWindow:Untitled",
                                           frame=Frame(10 + 90 * i, 10, 80, 20)))
    return textedit


def test_a_first_outline_that_fails_at_item_2_is_revised_and_the_run_continues(textedit):
    two_buttons(textedit)
    outliner = FakeOutliner(["Click the Downloads folder", "Sort the list by date", "Select the oldest file"],
                            [["Click the Header to reorder by date"]])
    steps = [click("Downloads"), click("Sort"), click("Sort"), click("Header"), DONE]
    executes = [(True, "ok"), (False, "no such control"), (False, "no such control"), (True, "ok")]
    agent, events = agent_for(textedit, steps, outliner, executes, max_consecutive_failures=5)
    result = agent.run("organize my downloads by date")
    assert events["outline"] == [["Click the Downloads folder", "Sort the list by date", "Select the oldest file"]]
    assert events["replan"] == [("two failed steps on one item", ["Click the Header to reorder by date"])]
    done, failed, history = outliner.replans[0]
    assert done == ["Click the Downloads folder"] and failed == "Sort the list by date" and "FAILED" in history
    assert agent.replans == 1 and result.success
    nxt = progress_scope(agent.checklist, textedit, agent.seen_facts, agent.started, agent.known_facts)
    assert nxt["done"][0] == "Click the Downloads folder"


def test_each_trigger_replans(textedit):
    # a loop warning (the same successful step three times)
    two_buttons(textedit)
    o1 = FakeOutliner(["Click the Header", "Sort the list"], [["Sort the list by date"]])
    agent, ev = agent_for(textedit, [click("Header")] * 4 + [DONE], o1, [(True, "ok")] * 4, max_repeated_successes=5)
    agent.run("do it")
    assert any(r == "loop warning" for r, _ in ev["replan"])
    # jev mode found no listed action
    two_buttons(textedit)
    o2 = FakeOutliner(["Run the export", "Check the result"], [["Click the Sort button"]])

    class NoneOfThese(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            if not self.steps:
                raise PlannerError(NO_SHELL)
            return super().next_step(goal, scope, history, screen)

    hooks = Hooks(observe=lambda: textedit, execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, on_replan=lambda r, i, x: ev2.append(r))
    ev2 = []
    agent = Agent(Settings(max_steps=3, max_consecutive_failures=9), NoneOfThese([]), NameSelector(), hooks, outliner=o2)
    agent.apps = []
    agent.run("export my data")
    assert ev2 and ev2[0] == "jev found no listed action"


def test_replans_are_capped_and_the_run_is_stuck(textedit):
    two_buttons(textedit)
    outliner = FakeOutliner(["Click the Sort button", "Check the result"], [["Click the Sort button"]] * 5)
    steps = [click("Sort")] * 20
    agent, ev = agent_for(textedit, steps, outliner, [(False, "no")] * 20, max_consecutive_failures=30, max_steps=20)
    result = agent.run("do it")
    assert agent.replans == MAX_REPLANS == len(ev["replan"])
    assert not result.success and result.reason == "stuck: Click the Sort button"


def test_no_outline_when_the_facts_cover_the_goal_or_for_forms(textedit):
    called = []

    class Spy(FakeOutliner):
        def outline(self, goal, context=""):
            called.append(goal)
            return ["x step"]

    agent, _ = agent_for(textedit, [DONE], Spy([], []), [])
    agent.apps = ["TextEdit"]
    agent.run("open TextEdit")  # facts cover it
    assert called == []
    agent, _ = agent_for(textedit, [DONE], Spy([], []), [])
    agent.run("tidy up my desk")
    assert called == ["tidy up my desk"]


def test_outline_and_replan_events_through_the_cli(monkeypatch, textedit):
    from typer.testing import CliRunner

    from local_agent import cli, outline
    from local_agent.act import executor
    from local_agent.observe import ax_tree
    from local_agent.planner import qwen
    from local_agent.selector import jev
    from tests.test_events import Planner

    monkeypatch.setenv("LOCAL_AGENT_SETTLE_S", "0")
    monkeypatch.setattr(cli, "_prepare", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_start_app", lambda *a: None)
    monkeypatch.setattr(ax_tree, "snapshot", lambda s: textedit)
    monkeypatch.setattr(executor, "execute", lambda step, el: Outcome(True, "did it"))
    monkeypatch.setattr(jev, "make_selector", lambda s: FakeSelector())
    monkeypatch.setattr(qwen, "make_planner", lambda s, selector=None: Planner([DONE]))
    monkeypatch.setattr(outline, "make_outliner", lambda s: FakeOutliner(["Click File", "Click Edit"], []))
    r = CliRunner().invoke(cli.app, ["run", "--events", "--app", "TextEdit", "--", "tidy up the menus"])
    events = [json.loads(line) for line in r.stdout.splitlines()]
    out = next(e for e in events if e["type"] == "outline")
    assert out["items"] == ["Click File", "Click Edit"] and out["seconds"] == 0.4 and out["load_s"] == 0.2
    assert [e["type"] for e in events if e["type"] != "stage"][:2] == ["start", "outline"]


def test_event_shapes():
    from local_agent import events

    assert events.outline_event(["a"], 1.234, 0.5) == {"items": ["a"], "seconds": 1.234, "load_s": 0.5, "rejected": None}
    assert events.outline_event([], 1.0, 0.0, "repeated")["rejected"] == "repeated"
    assert events.replan_event("loop warning", ["b"]) == {"reason": "loop warning", "items": ["b"], "rejected": None}


def test_outline_cases_accept_ideal_outlines_and_reject_bad_ones():
    from evals.planner_cases import OUTLINE_CASES, outline_check

    case = {c.name: c for c in OUTLINE_CASES}
    ideal = ["Open Arc", "Go to youtube", 'Search for "lofi"', "Play the first video"]
    assert outline_check(ideal, case["web_search_play"].must) == ""
    assert outline_check(["Play the first video", "Open Arc", "Search lofi on youtube"], case["web_search_play"].must) != ""
    assert outline_check(["Open Arc", "Go to youtube"], case["web_search_play"].must).startswith("2 steps")
    assert outline_check(["Open Keynote", "Go to slide 3", "Export as PDF", "Choose Desktop"],
                         case["export_pdf"].must) == ""
    assert len(OUTLINE_CASES) == 3


# --- the outline backend ------------------------------------------------------------------------------------------------


def test_the_outline_model_defaults_to_jev_only_in_jev_plus_low_memory_mode():
    from local_agent.outline import outline_backend

    assert outline_backend(Settings()) == "planner"
    assert outline_backend(Settings(planner_mode="jev")) == "planner"
    assert outline_backend(Settings(low_memory=True)) == "planner"
    assert outline_backend(Settings(planner_mode="jev", low_memory=True)) == "jev"
    # explicit always wins, either way
    assert outline_backend(Settings(planner_mode="jev", low_memory=True, outline_model="planner")) == "planner"
    assert outline_backend(Settings(outline_model="jev")) == "jev"


def test_the_jev_backend_posts_a_plain_chat_completion_to_the_llama_server():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": "1. Open Safari\n2. Search\n3. Write a note"}}]})

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    out = Outliner(Settings(outline_model="jev", planner_model="qwen2.5-coder:3b"), client)
    assert out.outline("look something up") == ["Open Safari", "Search", "Write a note"]
    path, body = seen[0]
    assert path == "/v1/chat/completions" and body["temperature"] == 0 and body["max_tokens"] <= 300
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and "model" not in body
    assert body["messages"][1]["content"].startswith("Goal: look something up")
    assert out.last_load_s == 0.0 and out.calls[0]["backend"] == "jev" and out.last_text.startswith("1. Open")
    # the same prompt for both backends
    bodies = []
    Outliner(Settings(outline_model="planner"), mock_client(bodies)).outline("look something up")
    assert bodies[0]["messages"] == body["messages"]
