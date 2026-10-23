"""Phase 2: page actions through a fake run_js; the real scripts are exercised live (docs/PLAN_browser.md)."""

import pytest

from local_agent.act import browser, executor
from local_agent.config import Settings
from local_agent.loop import Agent, Hooks
from local_agent.observe.models import Frame, UIElement
from local_agent.planner.schema import PlannerOutput
from evals.web_snapshots import web_snapshot
from tests.test_loop import DONE, FakeSelector, Outcome, P, ScriptedPlanner
from local_agent.selector.jev import Selection


class FakePage:
    """Stands in for browser.run_js: records (script is web_actions.js, args) and answers from a queue."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, bundle, src, args=None, kind="read"):
        self.calls.append(args)
        return self.answers.pop(0) if self.answers else {"ok": True}


@pytest.fixture
def page(monkeypatch):
    def install(*answers):
        fake = FakePage(*answers)
        monkeypatch.setattr(browser, "run_js", fake)
        monkeypatch.setattr(browser, "is_running", lambda b: True)
        monkeypatch.setattr(executor, "_front_browser", lambda: "b")
        monkeypatch.setattr(executor.time, "sleep", lambda s: None)
        return fake

    return install


def el(role="AXButton", **kw):
    return UIElement(id=1, role=role, title="x", web_id="la3", frame=Frame(0, 0, 10, 10), **kw)


def plan(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw)


def test_click_runs_the_click_op_on_the_web_id(page):
    fake = page({"ok": True})
    r = executor.execute(plan(action="click", target={"description": "x"}), el())
    assert r.ok and fake.calls == [{"op": "click", "web_id": "la3"}]


@pytest.mark.parametrize("role,how", [("AXTextField", "replaced"), ("AXTextArea", "inserted")])
def test_type_sends_the_text_and_reports_how(page, role, how):
    fake = page({"ok": True, "how": how})
    r = executor.execute(plan(action="type", text="héllo \"q\"\n"), el(role))
    assert r.ok and how in r.note and fake.calls == [{"op": "type", "web_id": "la3", "text": 'héllo "q"\n'}]


def test_contenteditable_falls_back_to_keystrokes(page, monkeypatch):
    typed = []
    monkeypatch.setattr(executor, "type_text", typed.append)
    page({"ok": False, "fallback": True})
    r = executor.execute(plan(action="type", text="hello"), el("AXTextArea"))
    assert r.ok and typed == ["hello"] and "keystrokes" in r.note


def test_scroll_reports_whether_the_position_changed(page):
    page({"ok": True, "moved": True, "where": "element"}, {"ok": True, "moved": False, "where": "window"})
    a = executor.execute(plan(action="scroll", direction="down", target={"description": "list"}), el("AXGroup"))
    b = executor.execute(plan(action="scroll", direction="up", target={"description": "list"}), el("AXGroup"))
    assert a.ok and "the list" in a.note and "did not change" not in a.note
    assert b.ok and "did not change" in b.note


def test_element_gone_fails_with_a_reobserve_note(page):
    page({"ok": False, "gone": True})
    r = executor.execute(plan(action="click", target={"description": "x"}), el())
    assert not r.ok and r.note == "page changed: element gone; re-observe"


def test_password_fields_are_refused_without_running_anything(page):
    fake = page()
    r = executor.execute(plan(action="type", text="hunter2"), el("AXSecureTextField"))
    assert not r.ok and r.note == executor.SECURE and fake.calls == []
    fake = page({"ok": False, "secure": True})  # the script's own refusal
    assert executor.execute(plan(action="type", text="x"), el("AXTextField")).note == executor.SECURE


def test_browser_error_becomes_a_failed_result(page, monkeypatch):
    def boom(*a, **k):
        raise browser.BrowserError("js_disabled", "off")

    page()
    monkeypatch.setattr(browser, "run_js", boom)
    r = executor.execute(plan(action="click", target={"description": "x"}), el())
    assert not r.ok and "js_disabled" in r.note


def test_settle_waits_for_quiet_and_is_capped(monkeypatch):
    seq = iter([{"ready": False, "quiet_ms": 0}, {"ready": True, "quiet_ms": 100}, {"ready": True, "quiet_ms": 400}])
    monkeypatch.setattr(browser, "web_action", lambda *a, **k: next(seq))
    monkeypatch.setattr(browser.time if hasattr(browser, "time") else __import__("time"), "sleep", lambda s: None)
    assert browser.settle("b", poll_s=0) >= 0 and next(seq, None) is None  # stopped at the third answer
    monkeypatch.setattr(browser, "web_action", lambda *a, **k: {"ready": False, "quiet_ms": 0})
    assert browser.settle("b", cap_s=0.05, poll_s=0.01) < 1.0


def test_scripts_are_fixed_and_safe():
    js = browser._actions_js()
    for needed in ("scrollIntoView", "execCommand(\"insertText\"", "getOwnPropertyDescriptor", "MutationObserver",
                   "type === \"password\""):  # fmt: skip
        assert needed in js
    for banned in ("document.cookie", "localStorage", "sessionStorage", "fetch(", "XMLHttpRequest", "eval("):
        assert banned not in js


# --- the loop: a web click, the trusted retry, the password guard -----------------------------------------------------


class WebSelector(FakeSelector):
    def __init__(self, title, **kw):
        super().__init__(**kw)
        self.title = title

    def select(self, goal, snap, step):
        e = next(x for x in snap.elements if x.title == self.title or x.name() == self.title)
        return Selection(e, 0.95, 0.95, 5, 1)

    def verify(self, goal, step, element, before, after):
        return 0.9 if self._moved(before, after) else 0.1

    @staticmethod
    def _moved(before, after):
        return {e.describe(80) for e in before.elements} != {e.describe(80) for e in after.elements}


def web_agent(states, steps, selector, trusted=None, **hook_kw):
    cur = [0]
    ran = {"execute": [], "trusted": [], "settle": 0}

    def execute(step, e):
        ran["execute"].append((step.action, e.web_id))
        return Outcome(True, "clicked in the page")

    def trusted_click(e):
        ran["trusted"].append(e.web_id)
        cur[0] = 1  # the page only reacts to the real click
        return Outcome(True, "trusted click at (1,2)")

    def settle():
        ran["settle"] += 1
        return 0.3

    hooks = Hooks(observe=lambda: states[cur[0]], execute=execute, run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, settle_web=settle, trusted_click=trusted_click if trusted is None else trusted,
                  **hook_kw)  # fmt: skip
    return Agent(Settings(), ScriptedPlanner(steps), selector, hooks), ran, cur


def after_tab_click():
    after = web_snapshot("web_spa_tabs")
    for e in after.elements:
        if e.role == "AXHeading" and e.value == "Overview":
            e.value = "Billing"
    return after


def test_web_click_that_changes_the_page_settles_without_a_retry():
    states = [web_snapshot("web_spa_tabs"), after_tab_click()]
    agent, ran, cur = web_agent(states, [P(action="click", target={"role": "AXTab", "description": "Billing tab"}), DONE],
                                WebSelector("Billing"), trusted=lambda e: pytest.fail("trusted click"))
    hooks = agent.hooks
    orig = hooks.execute

    def execute(step, e):  # the script click works: the page changes
        cur[0] = 1
        return orig(step, e)

    hooks.execute = execute
    result = agent.run("open the Billing tab")
    rec = result.history[0]
    assert result.success and rec.verified and ran["settle"] == 1 and "trusted" not in rec.note


def test_ignored_script_click_is_retried_once_with_a_trusted_click():
    states = [web_snapshot("web_spa_tabs"), after_tab_click()]
    agent, ran, _ = web_agent(states, [P(action="click", target={"role": "AXTab", "description": "Billing tab"}), DONE],
                              WebSelector("Billing"))
    result = agent.run("open the Billing tab")
    rec = result.history[0]
    assert [a for a, _ in ran["execute"]] == ["click"] and len(ran["trusted"]) == 1
    assert rec.verified and "the script click changed nothing; trusted click: trusted click at" in rec.note
    assert ran["settle"] == 2


def test_password_typing_is_refused_unrun_even_in_turbo():
    snap = web_snapshot("web_password_field")
    from local_agent.loop import SECURE_NOTE

    hooks = Hooks(observe=lambda: snap, execute=lambda s, e: pytest.fail("typed"), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None)  # fmt: skip
    agent = Agent(Settings(turbo=True), ScriptedPlanner([P(action="type", text="x", target={"role": "AXTextField", "description": "Password"})] * 3),
                  WebSelector("Password"), hooks)
    result = agent.run("type x into the password field")
    assert not result.success and all(r.note == SECURE_NOTE and not r.executed for r in result.history)


def test_outline_still_never_returns_password_values():
    from local_agent.observe.web import outline_to_elements

    o = {"inner_width": 100, "inner_height": 100, "more": 0, "elements": [
        {"web_id": "la1", "role": "password", "name": "Password", "value": "hunter2", "rect": [0, 0, 5, 5], "interactive": True}]}
    assert outline_to_elements(o, None)[0].value == ""
    assert 'role === "password") return ""' in __import__("local_agent.observe.web", fromlist=["x"]).outline_js()
