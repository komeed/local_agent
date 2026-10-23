import time

import pytest

from local_agent.act import browser
from local_agent.act.browser import BrowserError, TabInfo
from local_agent.observe import ax_tree
from tests.test_loop import DONE
from tests.test_tab_navigation import GOAL_TLD, FakeBridge, run


def test_a_blocked_call_times_out_and_the_next_call_gets_a_fresh_worker():
    t0 = time.monotonic()
    with pytest.raises(BrowserError) as e:
        browser._on_worker(lambda: time.sleep(5), 0.2)
    assert e.value.kind == "timeout" and time.monotonic() - t0 < 1.5
    assert browser._on_worker(lambda: "fine", 1.0) == "fine"  # not queued behind the stuck thread


def test_every_script_runs_under_an_applescript_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(browser, "_run_applescript", lambda src: seen.append(src) or "x")
    browser._execute("return 1", "read")
    browser._execute("return 2", "action")
    assert seen[0].startswith("with timeout of 3 seconds\n") and seen[0].endswith("\nend timeout")
    assert seen[1].startswith("with timeout of 5 seconds\n")
    browser.configure(read_s=2.0, action_s=7.0)
    try:
        browser._execute("return 3", "read")
        browser._execute("return 4", "action")
        assert seen[2].startswith("with timeout of 2 seconds") and seen[3].startswith("with timeout of 7 seconds")
    finally:
        browser.configure(read_s=3.0, action_s=5.0)


def test_the_hard_limit_is_one_second_past_the_script_timeout(monkeypatch):
    limits = []
    monkeypatch.setattr(browser, "_on_worker", lambda fn, t: limits.append(t))
    browser._execute("x", "read")
    browser._execute("x", "action")
    assert limits == [4.0, 6.0]


def test_error_1712_is_a_timeout():
    assert browser.classify_error({"NSAppleScriptErrorNumber": -1712, "NSAppleScriptErrorMessage": "AppleEvent timed out."}).kind == "timeout"


def test_outline_timeout_falls_back_to_ax_for_the_page(monkeypatch):
    from local_agent.config import Settings
    from local_agent.observe import web

    def stuck(*a, **k):
        raise BrowserError("timeout", "slow page")

    walked = []
    monkeypatch.setattr(web, "timed_outline", stuck)
    monkeypatch.setattr(ax_tree, "_frame", lambda el: None)
    monkeypatch.setattr(ax_tree, "_walk", lambda el, path, *a: walked.append((el, path)))
    out = ax_tree._merge_web("b", [("area", "AXWindow")], Settings(), [], 0)
    assert out == ("", "", {"error": "timeout"}) and walked == [("area", "AXWindow")]


def test_a_status_timeout_is_reported_as_the_web_state(monkeypatch):
    monkeypatch.setattr(browser, "status", lambda b: "timeout")
    assert ax_tree._web_ready("company.thebrowser.Browser", "t") == (False, "timeout")


def test_open_tab_sends_without_waiting_then_polls_until_the_new_tab_shows(monkeypatch):
    old = TabInfo(1, 1, "T1", "https://a.org/")
    lists = iter([[old], [old], [old, TabInfo(1, 2, "T2", "about:blank")], [old, TabInfo(1, 2, "T2", "https://youtube.com/")]])
    sent, sleeps, stages = [], [], []
    monkeypatch.setattr(browser, "select_style", lambda b: "select")
    monkeypatch.setattr(browser, "list_tabs", lambda b: next(lists))
    monkeypatch.setattr(browser, "_execute", lambda src, kind="read": sent.append((src, kind)))
    got = browser.open_tab("b", "https://youtube.com", sleep=sleeps.append, clock=lambda: 0.0, stage=stages.append)
    assert got.url == "https://youtube.com/" and len(sleeps) == 3 and set(sleeps) == {0.3}
    assert "ignoring application responses" in sent[0][0] and sent[0][1] == "action" and len(sent) == 1
    assert stages == ["waiting_for_page"]


def test_open_tab_gives_up_after_the_wait_and_selects_in_index_style(monkeypatch):
    now = iter(range(100))
    monkeypatch.setattr(browser, "select_style", lambda b: "index")
    monkeypatch.setattr(browser, "list_tabs", lambda b: [])
    monkeypatch.setattr(browser, "_execute", lambda src, kind="read": None)
    assert browser.open_tab("b", "https://x.org", wait_s=3, sleep=lambda s: None, clock=lambda: float(next(now))) is None
    chosen = []
    lists = iter([[], [TabInfo(1, 1, "", "https://x.org/")]])
    monkeypatch.setattr(browser, "list_tabs", lambda b: next(lists))
    monkeypatch.setattr(browser, "select_tab", lambda b, t: chosen.append(t))
    assert browser.open_tab("b", "https://x.org", sleep=lambda s: None, clock=lambda: 0.0).index == 1 and len(chosen) == 1


def test_open_script_is_fire_and_forget():
    for style in ("select", "index", "current"):
        script = browser.open_script(style, "b", "https://x.org")
        assert "ignoring application responses" in script and script.endswith("end ignoring")


def test_a_loop_reports_the_navigation_stages_in_order(monkeypatch):
    stages = []
    from local_agent.loop import Hooks

    original = Hooks.__init__

    def init(self, *a, **k):
        original(self, *a, **k)
        self.on_stage = lambda n, name: stages.append(name)

    monkeypatch.setattr(Hooks, "__init__", init)
    run(GOAL_TLD, FakeBridge(), [DONE] * 3)
    assert stages[:5] == ["observing", "opening_tab", "waiting_for_page", "observing", "planning"]
