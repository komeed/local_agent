import pytest

from local_agent.act import browser
from local_agent.act.browser import BrowserError, FocusedField, TabInfo, fill_address

B = "company.thebrowser.Browser"
BLANK = TabInfo(1, 12, "NEW", "about:blank")


class FakeIO:
    """Scripted primitives. `focus_after` = how many focused() polls before the address field shows up (None: never);
    `settable` = whether AXValue can be set; `readable` = whether it can be read; `typing_lands` = whether keystrokes work."""

    def __init__(self, focus_after=0, settable=True, readable=True, typing_lands=True, focus_on_attempt=1):
        self.log, self.focus_after, self.settable, self.readable = [], focus_after, settable, readable
        self.typing_lands, self.polls, self.text = typing_lands, 0, "old text"
        self.focus_on_attempt, self.attempt, self.t = focus_on_attempt, 0, 0.0
        self.page = FocusedField("page", "AXWebArea")
        self.field = FocusedField("urlbar", "AXTextField", identifier="commandBarTextField")
        self.tab = BLANK
        self.closed = []

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t

    def open_blank(self, bundle, stage):
        self.log.append("open_blank")
        return self.tab

    def activate(self, bundle):
        self.log.append("activate")

    front = True

    def is_front(self, bundle):
        return self.front

    def shortcut(self, keys):
        self.log.append(f"key:{keys}")
        if keys == "cmd+l":
            self.attempt += 1
            self.polls = 0
        if keys == "cmd+a":
            self.selected = True

    def keys(self, text):
        self.log.append(f"type:{text}")
        if self.typing_lands:
            self.text = text

    def focused(self, bundle):
        self.polls += 1
        if self.attempt < self.focus_on_attempt or self.focus_after is None or self.polls <= self.focus_after:
            return self.page
        return self.field

    def same(self, a, b):
        return a == b

    def value(self, field):
        return self.text if self.readable else None

    def set_value(self, field, text):
        self.log.append(f"set:{text}")
        if self.settable:
            self.text = text
        return self.settable

    def close(self, bundle, tab):
        self.closed.append(tab)
        self.log.append("close")


def returns(io):
    return [i for i, e in enumerate(io.log) if e == "key:return"]


def test_the_field_appears_late_then_the_value_is_set_and_checked_before_return():
    io = FakeIO(focus_after=7)
    stages = []
    assert fill_address(io, B, "youtube", stages.append) == "verified via AX value"
    assert io.log.index("set:youtube") < returns(io)[0] and io.log[-1] == "key:return" and io.closed == []
    assert stages == ["focusing_address_bar", "typing_query"]
    assert not any(e.startswith("type:") for e in io.log)  # no keystrokes needed


def test_no_focused_field_closes_our_blank_tab_and_raises_without_pressing_return():
    io = FakeIO(focus_after=None)
    with pytest.raises(BrowserError) as e:
        fill_address(io, B, "youtube")
    assert e.value.kind == "search_field" and "address field" in str(e.value)
    assert returns(io) == [] and io.closed == [BLANK]
    assert io.log.count("key:cmd+l") == 2  # one retry


def test_nothing_is_typed_when_the_browser_is_not_in_front():
    io = FakeIO()
    io.front = False
    with pytest.raises(BrowserError, match="not in front"):
        fill_address(io, B, "youtube")
    assert not any(e.startswith(("key:", "type:", "set:")) for e in io.log) and io.closed == [BLANK]


def test_the_field_shows_on_the_retry():
    io = FakeIO(focus_on_attempt=2)
    assert fill_address(io, B, "youtube") == "verified via AX value" and io.log.count("key:cmd+l") == 2 and io.closed == []


def test_a_refused_ax_value_falls_back_to_keystrokes_and_is_verified():
    io = FakeIO(settable=False)
    assert fill_address(io, B, "YouTube ") == "verified after typing"
    assert io.log.index("key:cmd+a") < io.log.index("type:YouTube ") < returns(io)[0]


def test_a_value_that_does_not_match_retries_then_fails_and_never_presses_return():
    io = FakeIO(settable=False, typing_lands=False)
    with pytest.raises(BrowserError):
        fill_address(io, B, "youtube")
    assert returns(io) == [] and io.closed == [BLANK] and io.log.count("key:cmd+l") == 2


def test_an_unreadable_field_is_accepted_only_while_it_keeps_focus_and_is_called_unverified():
    io = FakeIO(settable=False, readable=False)
    assert fill_address(io, B, "youtube").startswith("unverified")
    assert returns(io)


def test_a_focus_that_is_not_new_does_not_count():
    io = FakeIO()
    io.page = io.field  # the URL field already had focus before cmd+l and keeps it: nothing "appeared"
    with pytest.raises(BrowserError):
        fill_address(io, B, "youtube")
    assert returns(io) == [] and io.closed == [BLANK]


def test_without_a_tab_id_nothing_is_closed():
    io = FakeIO(focus_after=None)
    io.tab = None
    with pytest.raises(BrowserError):
        fill_address(io, B, "youtube")
    assert io.closed == []


def test_which_focused_elements_count_as_address_fields():
    assert browser.is_address_like(FocusedField("r", "AXTextField"))
    assert browser.is_address_like(FocusedField("r", "AXComboBox"))
    assert browser.is_address_like(FocusedField("r", "AXGroup", identifier="omniboxView"))
    assert browser.is_address_like(FocusedField("r", "AXTextArea", description="Address and search bar"))
    assert not browser.is_address_like(FocusedField("r", "AXWebArea"))


def test_close_scripts_close_by_id_or_index():
    assert 'tell tab id "NEW" to close' in browser.close_script("select", B, BLANK)
    assert "close tab 12 of window 1" in browser.close_script("index", "c", BLANK)


def test_the_loop_notes_a_failed_search_field_and_leaves_the_planner_the_address_bar_line():
    from evals.web_snapshots import dashboard_snapshot
    from local_agent.config import Settings
    from local_agent.loop import Agent, Hooks
    from tests.test_loop import DONE, FakeSelector, Outcome, ScriptedPlanner
    from tests.test_search_site import SearchBridge

    class Failing(SearchBridge):
        def search_in_tab(self, bundle, query, stage=None):
            raise BrowserError("search_field", "couldn't focus/fill Arc's address field")

    cur = [dashboard_snapshot()]
    bridge = Failing(None, None)
    bridge.cur = cur
    scopes = []

    class Planner(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            scopes.append(scope)
            return super().next_step(goal, scope, history, screen)

    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, browser=bridge)
    agent = Agent(Settings(max_consecutive_failures=2), Planner([DONE] * 3), FakeSelector(), hooks)
    agent.apps = ["Arc"]
    result = agent.run("open youtube in arc")
    assert "search failed (search_field: couldn't focus/fill Arc's address field)" in result.history[0].note
    assert scopes[0]["progress"]["next"].startswith('go to youtube: search for "youtube" in the')


def test_our_blank_tab_is_a_real_file_url_because_arc_refuses_about_blank():
    url = browser.blank_url()
    assert url.startswith("file://") and url.endswith("local-agent-blank.html") and browser.check_url(url) == url
