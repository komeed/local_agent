import pytest

from evals.web_snapshots import dashboard_snapshot
from local_agent.act import browser, executor
from local_agent.act.browser import SearchOutcome, TabInfo
from local_agent.config import Settings
from local_agent.dialogs import is_discard_button, leave_buttons, leave_dialog, leave_line
from local_agent.loop import Agent, Hooks
from local_agent.observe import ax_tree
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import Selection
from tests.test_loop import DONE, FakeSelector, Outcome, ScriptedPlanner
from tests.test_search_site import SearchBridge, link, results
from tests.test_tab_navigation import FakeBridge, page

B = "company.thebrowser.Browser"
SHEET = "AXWindow:Docs > AXSheet"


def dialog_snap(text, buttons, app="Arc", url="https://docs.example/edit"):
    els = [UIElement(id=0, role="AXStaticText", value=text, interactive=False, path=SHEET, frame=Frame(300, 200, 400, 20))]
    for i, b in enumerate(buttons, 1):
        els.append(UIElement(id=i, role="AXButton", title=b, path=SHEET, frame=Frame(300 + 90 * i, 280, 80, 24)))
    return Snapshot(app, B if app == "Arc" else "x." + app, 1, "Docs", els, url=url)


LEAVE = dialog_snap("Changes you made may not be saved.", ["Cancel", "Leave"])


def P(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw)


def click(name, i):
    return P(action="click", target={"id": i, "role": "AXButton", "description": name})


class IdSelector(FakeSelector):
    def select(self, goal, snap, step):
        return Selection(next(e for e in snap.elements if e.id == step.target.id), 0.95, 0.95, 3, 1)


# --- page JavaScript is not run behind a dialog --------------------------------------------------------------------------


def test_dialog_roles_and_the_web_state(monkeypatch):
    assert ax_tree.dialog_roles_present(["AXDialog"], []) and ax_tree.dialog_roles_present(["AXSystemFloatingWindow"], [])
    assert ax_tree.dialog_roles_present(["AXStandardWindow"], ["AXSheet"])
    assert not ax_tree.dialog_roles_present(["AXStandardWindow"], ["AXGroup", "AXToolbar"])

    def boom(*a, **k):
        raise AssertionError("page JavaScript ran behind a dialog")

    monkeypatch.setattr(browser, "run_js", boom)
    monkeypatch.setattr(browser, "status", boom)
    assert ax_tree._web_ready(B, "t", dialog_up=True) == (False, "dialog")


def test_web_actions_do_not_run_page_js_while_a_dialog_is_up(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("page JavaScript ran behind a dialog")

    monkeypatch.setattr(browser, "run_js", boom)
    monkeypatch.setattr(executor, "_front_browser", lambda: B)
    monkeypatch.setattr(executor, "_dialog_up", lambda b: True)
    el = UIElement(id=1, role="AXButton", title="x", web_id="la1", frame=Frame(0, 0, 9, 9))
    r = executor.execute(P(action="click", target={"description": "x"}), el)
    assert not r.ok and r.note == executor.DIALOG_UP
    assert not executor.trusted_click(el).ok and executor.settle_web() == 0.0


# --- leave / discard dialogs ----------------------------------------------------------------------------------------------


def test_leave_dialogs_are_found_by_text_and_button():
    assert leave_dialog(LEAVE) and leave_dialog(dialog_snap("Changes you made may not be saved.", ["Cancel", "OK"]))
    leave, stay = leave_buttons(LEAVE)
    assert leave.title == "Leave" and stay.title == "Cancel"
    assert is_discard_button(LEAVE, leave) and not is_discard_button(LEAVE, stay)
    assert leave_line(LEAVE) == 'answer the dialog: "Changes you made may not be saved.": "Cancel" keeps your changes'
    # an ordinary alert's OK is not a discard
    err = dialog_snap("The file could not be saved.", ["OK"])
    assert not leave_dialog(err) and not is_discard_button(err, err.elements[1])


def test_dont_save_discards_in_any_dialog_but_save_does_not():
    sheet = dialog_snap("Do you want to save the changes made to the document “Untitled”?", ["Don’t Save", "Cancel", "Save"], app="TextEdit")
    by = {e.title: e for e in sheet.elements if e.role == "AXButton"}
    assert is_discard_button(sheet, by["Don’t Save"]) and not is_discard_button(sheet, by["Save"]) and not is_discard_button(sheet, by["Cancel"])


def run_planner(snap, steps, confirm=True, turbo=False, bridge=None):
    ran, asked, scopes = [], [], []

    class Planner(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            scopes.append(scope)
            return super().next_step(goal, scope, history, screen)

    hooks = Hooks(observe=lambda: snap, execute=lambda s, e: ran.append((s.action, e.title if e else s.keys)) or Outcome(True, "ok"),
                  run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None,
                  confirm_terminal=lambda c, w: asked.append((c, w)) or confirm, browser=bridge)
    agent = Agent(Settings(turbo=turbo, max_consecutive_failures=9, max_steps=len(steps) + 1), Planner(steps + [DONE] * 3), IdSelector(), hooks)
    agent.apps = []
    return agent.run("keep going"), ran, asked, scopes


ASK = ("leave the page and lose unsaved changes", "would discard unsaved changes")


@pytest.mark.parametrize("turbo", [False, True])
def test_the_planner_pressing_leave_asks_even_in_turbo_and_declining_keeps_the_page(turbo):
    result, ran, asked, scopes = run_planner(LEAVE, [click("Leave", 2)], confirm=False, turbo=turbo)
    assert asked == [ASK] and ran == [] and "user declined" in result.history[0].note
    assert scopes[0]["progress"]["next"].startswith('answer the dialog: "Changes you made may not be saved."')
    result, ran, asked, _ = run_planner(LEAVE, [click("Leave", 2)], confirm=True, turbo=turbo)
    assert asked == [ASK] and ran == [("click", "Leave")]


def test_cancel_and_stay_are_never_gated_but_return_is():
    _, ran, asked, _ = run_planner(LEAVE, [click("Cancel", 1)])
    assert ran == [("click", "Cancel")] and asked == []
    _, ran, asked, _ = run_planner(LEAVE, [P(action="shortcut", keys="return")], confirm=False)
    assert asked == [ASK] and ran == []


def test_dont_save_in_a_native_sheet_asks():
    sheet = dialog_snap("Do you want to save the changes?", ["Don’t Save", "Cancel", "Save"], app="TextEdit")
    _, ran, asked, _ = run_planner(sheet, [click("Don’t Save", 1)], confirm=False)
    assert asked == [ASK] and ran == []
    _, ran, asked, _ = run_planner(sheet, [click("Save", 3)])
    assert ran == [("click", "Save")] and asked == []


class LeaveBridge(FakeBridge):
    """Opening the tab raises the leave dialog, as navigating a tab with unsaved edits does."""

    def open_tab(self, bundle, url, stage=None):
        super().open_tab(bundle, url, stage)
        self.cur[0] = LEAVE
        return TabInfo(1, 11, "NEW", url + "/")


def run_own_navigation(confirm):
    cur, ran, asked = [dashboard_snapshot()], [], []
    bridge = LeaveBridge()
    bridge.cur = cur

    def execute(step, el):
        ran.append((step.action, el.title if el else step.keys))
        cur[0] = page("https://youtube.com/") if el is not None and el.title == "Leave" else LEAVE
        return Outcome(True, "ok")

    hooks = Hooks(observe=lambda: cur[0], execute=execute, run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None,
                  confirm_terminal=lambda c, w: asked.append((c, w)) or confirm, browser=bridge)
    agent = Agent(Settings(turbo=True, max_steps=6), ScriptedPlanner([DONE] * 4), IdSelector(), hooks)
    agent.apps = ["Arc"]
    return agent.run("open youtube.com in arc"), ran, asked


def test_a_leave_dialog_from_our_own_navigation_is_asked_once_and_answered_in_code():
    result, ran, asked = run_own_navigation(True)
    assert asked == [ASK] and ran == [("click", "Leave")]
    assert any("leave confirmed by user" in r.note for r in result.history)


def test_declining_a_leave_dialog_of_ours_presses_cancel_and_ends_the_run():
    result, ran, asked = run_own_navigation(False)
    assert asked == [ASK] and ran == [("click", "Cancel")]
    assert not result.success and result.reason == "leave_declined"


# --- never navigate the user's tabs ---------------------------------------------------------------------------------------


def chrome_snap():
    bar = UIElement(id=1, role="AXTextField", title="Command Bar Text Field", focused=True, path="AXWindow:Docs > AXToolbar",
                    frame=Frame(300, 20, 600, 24))
    return Snapshot("Arc", B, 1, "Docs", [bar], url="https://docs.example/")


class ActiveBridge(FakeBridge):
    def __init__(self, active):
        super().__init__()
        self.active = active

    def active_tab(self, bundle):
        return self.active


TYPE_URL = P(action="type", text="youtube.com", target={"id": 1, "role": "AXTextField", "description": "address bar"})


def test_typing_into_the_address_bar_of_a_user_tab_is_blocked():
    result, ran, _, _ = run_planner(chrome_snap(), [TYPE_URL], bridge=ActiveBridge(TabInfo(1, 3, "USER", "https://docs.example/")))
    assert ran == [] and result.history[0].note == "that tab is yours; the agent only navigates tabs it opened"


def test_the_address_bar_of_a_tab_we_opened_may_be_typed_into():
    snap = chrome_snap()
    cur, ran = [snap], []
    bridge = ActiveBridge(TabInfo(1, 11, "OURS", "file:///blank"))
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: ran.append(s.text) or Outcome(True, "ok"),
                  run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None, browser=bridge)
    agent = Agent(Settings(), ScriptedPlanner([TYPE_URL, DONE]), IdSelector(), hooks)
    agent.apps = []
    agent.our_tabs.append(TabInfo(1, 11, "OURS", "file:///blank"))
    agent._tried_sites = set()
    agent.run("go on")
    assert ran == ["youtube.com"]


def test_selecting_an_existing_tab_is_allowed_and_never_navigates_it():
    bridge = FakeBridge([TabInfo(1, 4, "T4", "https://www.youtube.com/watch?v=x")])
    cur = [dashboard_snapshot()]
    bridge.cur = cur
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, browser=bridge)
    Agent(Settings(), ScriptedPlanner([DONE] * 3), FakeSelector(), hooks).run("open youtube in arc")
    assert bridge.calls == [("select", "T4")]


def run_search(bridge):
    res = results(link(1, "YouTube", "https://www.youtube.com/"))
    cur = [dashboard_snapshot()]
    bridge.cur = cur
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, browser=bridge)
    agent = Agent(Settings(), ScriptedPlanner([DONE] * 3), FakeSelector(), hooks)
    agent.apps = ["Arc"]
    agent.run("open youtube in arc")
    return agent


def test_the_result_is_opened_in_our_search_tab_only_when_that_tab_is_ours():
    res = results(link(1, "YouTube", "https://www.youtube.com/"))
    landing = page("https://www.youtube.com/")
    bridge = SearchBridge(res, landing)
    agent = run_search(bridge)
    assert [c[0] for c in bridge.calls] == ["search", "navigate"] and agent.our_tabs[0].id == "S1"

    class Unidentified(SearchBridge):
        def search_in_tab(self, bundle, query, stage=None):
            super().search_in_tab(bundle, query, stage)
            return SearchOutcome("unverified", None)  # the bridge could not tell which tab it opened

    other = Unidentified(res, landing)
    run_search(other)
    assert [c[0] for c in other.calls] == ["search", "open"]  # a new tab, no navigation of an unknown one

    class Switched(SearchBridge):
        def active_tab(self, bundle):
            return TabInfo(1, 2, "USER", "https://docs.example/")  # the user switched to their own tab meanwhile

    moved = Switched(res, landing)
    run_search(moved)
    assert [c[0] for c in moved.calls] == ["search", "open"]
