"""Speed-ups: verifying in code, facts-only goal checks, event-driven settle, prompt caching and its counters."""

from dataclasses import replace

from local_agent.config import Settings
from local_agent.loop import Agent, Hooks, wait_stable
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.selector.jev import JevK5Client
from tests.test_loop import DONE, FakeSelector, GoalSelector, Outcome, P, ScriptedPlanner


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_settle_returns_as_soon_as_the_fingerprint_is_stable():
    c, readings = Clock(), iter(["a", "b", "b", "b", "b", "b", "b", "b", "b", "b"])
    waited = wait_stable(lambda: next(readings), c.sleep, 0.8, 0.05, 0.15, clock=c.now)
    assert 0.15 <= waited < 0.4  # changed once, then three polls (0.15 s) of the same reading
    c = Clock()
    assert wait_stable(lambda: "same", c.sleep, 0.8, 0.05, 0.15, clock=c.now) <= 0.25


def test_settle_is_capped_and_an_unreadable_fingerprint_sleeps_the_cap():
    c, n = Clock(), iter(range(1000))
    assert wait_stable(lambda: next(n), c.sleep, 0.8, 0.05, 0.15, clock=c.now) >= 0.8
    c = Clock()
    assert wait_stable(lambda: None, c.sleep, 0.8, 0.05, 0.15, clock=c.now) == 0.8


def field(i, value=""):
    return UIElement(id=i, role="AXTextField", title="Name", value=value, frame=Frame(0, 0, 9, 9))


def button(i, title):
    return UIElement(id=i, role="AXButton", title=title, frame=Frame(0, i * 20, 9, 9))


def snap(title="Doc", elements=(), url=""):
    return Snapshot("App", "x", 1, title, list(elements), url=url)


class CountingSelector(GoalSelector):
    def __init__(self, **kw):
        super().__init__([0.1] * 10, **kw)
        self.verifies = 0

    def verify(self, *a):
        self.verifies += 1
        return super().verify(*a)


def run(before, after, plan, goal="do it", selector=None, **settings):
    cur = [before]

    def execute(step, el):
        cur[0] = after
        return Outcome(True, "ok")

    hooks = Hooks(observe=lambda: cur[0], execute=execute, run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None)
    selector = selector or CountingSelector()
    agent = Agent(Settings(max_steps=1, **settings), ScriptedPlanner([plan, DONE]), selector, hooks)
    agent.apps = ["App"]
    agent.run(goal)
    return agent, selector


CLICK = P(action="click", target={"id": 1, "role": "AXButton", "description": "Go"})


def test_code_verifies_a_click_that_changed_the_window():
    agent, sel = run(snap("A", [button(1, "Go")]), snap("B", [button(1, "Go")]), CLICK)
    rec = agent.history[0]
    assert rec.verified and rec.verify_source == "code" and sel.verifies == 0 and "verified in code" in rec.note


def test_code_verifies_typed_text_a_new_dialog_a_new_address_and_a_vanished_control():
    typed = P(action="type", text="hello", target={"id": 2, "role": "AXTextField", "description": "Name"})
    agent, sel = run(snap(elements=[field(2)]), snap(elements=[field(2, "hello world")]), typed)
    assert agent.history[0].verify_source == "code" and sel.verifies == 0
    menu = UIElement(id=9, role="AXMenuItem", title="Copy", frame=Frame(0, 0, 9, 9), path="AXMenu")
    agent, sel = run(snap(elements=[button(1, "Go")]), snap(elements=[button(1, "Go"), menu]), CLICK)
    assert agent.history[0].verify_source == "code" and sel.verifies == 0
    agent, sel = run(snap(elements=[button(1, "Go")], url="https://a.org/"), snap(elements=[button(1, "Go")], url="https://b.org/"), CLICK)
    assert agent.history[0].verify_source == "code"
    agent, sel = run(snap(elements=[button(1, "Go"), button(2, "x")]), snap(elements=[button(2, "x")]), CLICK)
    assert agent.history[0].verify_source == "code"


def test_jev_is_asked_when_nothing_changed_or_code_verify_is_off():
    same = snap(elements=[button(1, "Go")])
    agent, sel = run(same, same, CLICK)
    assert sel.verifies == 1 and agent.history[0].verify_source == "jev"
    agent, sel = run(snap("A", [button(1, "Go")]), snap("B", [button(1, "Go")]), CLICK, code_verify=False)
    assert sel.verifies == 1 and agent.history[0].verify_source == "jev"


def test_a_goal_the_facts_cover_never_asks_jev_whether_it_is_done():
    agent, sel = run(snap("A", [button(1, "Go")]), snap("B", [button(1, "Go")]), CLICK, goal="open App")
    assert agent.facts and sel.goal_calls == 0


def test_an_uncovered_goal_asks_jev_only_after_a_step_that_changed_the_screen():
    changed_agent, sel = run(snap("A", [button(1, "Go")]), snap("B", [button(1, "Go")]), CLICK, goal="tidy the inbox")
    assert not changed_agent.facts and sel.goal_calls == 1
    same = snap(elements=[button(1, "Go")])
    _, sel = run(same, same, CLICK, goal="tidy the inbox", code_verify=False)
    assert sel.goal_calls == 0


class FakeModel:
    """Stands in for llama-server behind jevk5's client: records the payloads and answers with timings."""

    def __init__(self):
        self.sent = []

    def post(self, path, payload):
        self.sent.append((path, payload))
        if path == "/tokenize":
            return {"tokens": [1, 2, 3]}
        return {"completion_probabilities": [{"top_logprobs": [{"token": "A", "logprob": -0.1}]}], "tokens_evaluated": 3,
                "timings": {"prompt_n": 2, "cache_n": 40, "prompt_ms": 30.0, "predicted_ms": 5.0}}


def test_cache_prompt_is_sent_and_the_counters_report_reuse(monkeypatch):
    fake = FakeModel()
    client = JevK5Client(Settings(jev_cache_prompt=True))
    monkeypatch.setattr(type(client.model), "_post", lambda self, path, payload: fake.post(path, payload))
    client.model._logprobs("x")
    assert fake.sent[-1][1]["cache_prompt"] is True
    stats = client.take_stats()
    assert stats == {"calls": 1, "prompt_n": 2, "cache_n": 40, "server_s": 0.035}
    assert client.take_stats()["calls"] == 0
    off = JevK5Client(Settings(jev_cache_prompt=False))
    monkeypatch.setattr(type(off.model), "_post", lambda self, path, payload: fake.post(path, payload))
    off.model._logprobs("x")
    assert fake.sent[-1][1]["cache_prompt"] is False
