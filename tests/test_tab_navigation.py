import pytest

from evals.web_snapshots import dashboard_snapshot
from local_agent.act import browser
from local_agent.act.browser import BrowserError, TabInfo
from local_agent.config import Settings
from local_agent.goal_facts import _on_site
from local_agent.loop import Agent, Hooks
from local_agent.observe import ax_tree
from local_agent.observe.models import Frame, Snapshot, UIElement
from tests.test_loop import DONE, FakeSelector, Outcome, P, ScriptedPlanner

SEARCH = UIElement(id=1, role="AXSearchField", title="Search", frame=Frame(0, 0, 9, 9), path="AXWebArea")
GOAL = 'open youtube in arc and search "dream"'
GOAL_TLD = 'open youtube.com in arc and search "dream"'


def page(url, elements=()):
    return Snapshot("Arc", "company.thebrowser.Browser", 1, "t", list(elements), url=url)


class FakeBridge:
    def __init__(self, tabs=(), fail_open=False):
        self.tabs, self.calls, self.fail_open, self.cur = list(tabs), [], fail_open, None

    def bundle_for(self, name):
        return "company.thebrowser.Browser" if "arc" in name.lower() else None

    def find_tab(self, bundle, site):
        return next((t for t in self.tabs if _on_site(site, t.url) is True), None)

    def select_tab(self, bundle, tab):
        self.calls.append(("select", tab.id))
        self.cur[0] = page(tab.url, [SEARCH])

    def open_tab(self, bundle, url, stage=None):
        if self.fail_open:
            raise BrowserError("other", "no scripting")
        self.calls.append(("open", url))
        if stage:
            stage("waiting_for_page")
        self.cur[0] = page(url + "/", [SEARCH])
        return TabInfo(1, 11, "NEW", url + "/")


def run(goal, bridge, steps, start=None, execute=None, **settings):
    cur, ran, scopes = [start or dashboard_snapshot()], [], []
    bridge.cur = cur

    class Planner(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            scopes.append(scope)
            return super().next_step(goal, scope, history, screen)

    hooks = Hooks(observe=lambda: cur[0], execute=execute(cur) if execute else (lambda s, e: Outcome(True, "x")),
                  run_terminal=lambda c: ran.append(c) or Outcome(True, "exit=0"), sleep=lambda s: None,
                  confirm_terminal=lambda c, w: True, browser=bridge)  # fmt: skip
    agent = Agent(Settings(**settings), Planner(steps), FakeSelector(), hooks)
    agent.apps = ["Arc", "Safari"]
    return agent.run(goal), ran, scopes, cur


def test_an_existing_tab_is_selected_and_no_tab_is_opened():
    bridge = FakeBridge([TabInfo(1, 4, "T4", "https://www.youtube.com/watch?v=x"), TabInfo(1, 1, "T1", "https://a.org/")])
    result, ran, scopes, _ = run(GOAL, bridge, [DONE] * 3)
    assert bridge.calls == [("select", "T4")] and ran == []
    assert result.history[0].note.startswith("opened by the agent (goal names site youtube); switch to the existing youtube tab")
    assert scopes[0]["progress"]["next"] == 'type "dream" into the "Search" field, then press return'


def test_no_tab_means_exactly_one_new_tab_and_no_open_command():
    bridge = FakeBridge([TabInfo(1, 1, "T1", "https://a.org/")])
    result, ran, _, _ = run(GOAL_TLD, bridge, [DONE] * 3)
    assert bridge.calls == [("open", "https://youtube.com")] and ran == []
    assert "open youtube.com in a new Arc tab" in result.history[0].note


def test_a_planner_repeat_open_is_blocked_but_another_site_is_allowed():
    bridge = FakeBridge()
    steps = [P(action="terminal", command='open -a "Arc" "https://youtube.com"'),
             P(action="terminal", command="open https://www.youtube.com/results"),
             P(action="terminal", command='open -a "Arc" "https://example.org"'), DONE]
    result, ran, _, _ = run(GOAL_TLD, bridge, steps + [DONE] * 12, max_steps=12, max_consecutive_failures=10)
    blocked = [h for h in result.history if "is already open in the front tab; work on:" in h.note]
    assert len(blocked) == 2 and blocked[0].note.startswith("youtube.com is already open") and not blocked[0].executed
    assert ran == ['open -a "Arc" "https://example.org"'] and bridge.calls == [("open", "https://youtube.com")]


def test_browser_errors_fall_back_to_open_once_with_a_note():
    result, ran, _, _ = run("open youtube.com in arc", FakeBridge(fail_open=True), [DONE] * 8, max_steps=6)
    assert ran == ['open -a "Arc" "https://youtube.com"']
    assert "tab navigation failed (other: no scripting)" in result.history[0].note


def test_the_0942_shape_ends_with_the_search_line_then_by_facts():
    bridge = FakeBridge()

    def execute(cur):
        def go(step, el):
            cur[0] = page("https://www.youtube.com/results?search_query=dream")
            return Outcome(True, "pressed")
        return go

    result, ran, scopes, _ = run(GOAL_TLD, bridge, [P(action="shortcut", keys="return")], execute=execute)
    assert scopes[0]["progress"]["next"] == 'type "dream" into the "Search" field, then press return'
    assert result.success and result.reason.startswith("goal facts hold") and ran == []
    assert bridge.calls == [("open", "https://youtube.com")]


# --- the scripting layer ----------------------------------------------------------------------------------------------


def test_scripts_per_browser_style():
    arc = browser.select_script("select", "company.thebrowser.Browser", TabInfo(1, 3, 'a"b', "u"))
    assert 'tell window 1 to tell tab id "a\\"b" to select' in arc and "set index" not in arc
    assert "set active tab index of window 2 to 5" in browser.select_script("index", "c", TabInfo(2, 5, "", "u"))
    assert "set index of window 2 to 1" in browser.select_script("index", "c", TabInfo(2, 5, "", "u"))
    assert "set current tab of window 1 to tab 5 of window 1" in browser.select_script("current", "s", TabInfo(1, 5, "", "u"))
    for style in ("select", "index", "current"):
        script = browser.open_script(style, "b", "https://x.org/a?b=1")
        assert script.count("make new tab") == 1 and "open " not in script.replace("open_", "")
        assert "(count of windows) = 0 then make new" in script


def test_only_plain_urls_are_opened():
    for bad in ('javascript:alert(1)', 'https://x.org/"; do shell script "x', "https://x.org/a b", "x.org"):
        with pytest.raises(BrowserError):
            browser.open_script("select", "b", bad)


def test_parse_tabs():
    text = "1\t1\tID1\thttps://a.org/\n1\t2\t\thttps://b.org/x\ngarbage\n2\t1\tID3\t\n"
    assert browser.parse_tabs(text) == [TabInfo(1, 1, "ID1", "https://a.org/"), TabInfo(1, 2, "", "https://b.org/x"),
                                        TabInfo(2, 1, "ID3", "")]


def test_front_window_must_be_the_window_ax_says_is_frontmost(monkeypatch):
    assert browser.window_matches("Docs", "Docs") and browser.window_matches("Docs", "Docs - Google Chrome")
    assert not browser.window_matches("Docs", "YouTube")
    monkeypatch.setattr(browser, "status", lambda b: "ok")
    monkeypatch.setattr(browser, "front_window_name", lambda b: "Dashboard")
    arc = "company.thebrowser.Browser"
    assert ax_tree._web_ready(arc, "Dashboard") == (True, "")
    # a Little Arc style window: AX is on a small window titled YouTube, scripting's front window is the main one
    assert ax_tree._web_ready(arc, "YouTube") == (False, "front window not readable")
    assert ax_tree._web_ready("com.apple.TextEdit", "x") == (False, "")
    monkeypatch.setattr(browser, "status", lambda b: "no_permission")
    assert ax_tree._web_ready(arc, "Dashboard") == (False, "")


def test_parse_tabs_reads_the_title_last():
    text = "1\t1\tID1\thttps://a.org/\tThe A page\n1\t2\t\thttps://b.org/x\t\n"
    assert browser.parse_tabs(text) == [TabInfo(1, 1, "ID1", "https://a.org/", "The A page"), TabInfo(1, 2, "", "https://b.org/x")]
