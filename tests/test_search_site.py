import pytest

from local_agent.config import Settings
from local_agent.loop import Agent, Hooks
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.search_results import organic_links, pick_by_host, host_of
from local_agent.selector.jev import Selection
from tests.test_loop import DONE, FakeSelector, Outcome, ScriptedPlanner, P
from tests.test_tab_navigation import FakeBridge, SEARCH, page
from evals.web_snapshots import dashboard_snapshot

ENGINE = "https://search.example/results?q=x"


def link(i, title, href, context="", path="AXWebArea"):
    return UIElement(id=i, role="AXLink", title=title, href=href, context=context, path=path,
                     frame=Frame(10, 100 + 40 * i, 200, 20))


def results(*links, url=ENGINE):
    return Snapshot("Arc", "company.thebrowser.Browser", 1, "r", list(links), url=url)


def test_an_ad_first_and_the_organic_match_second_picks_organic():
    snap = results(
        link(1, "Ad · Cheap video downloads", "https://adnetwork.example/click?x=1", 'section "Sponsored"'),
        link(2, "Sponsored: video site", "https://youtube-clone.example/", ""),
        link(3, "YouTube", "https://www.youtube.com/"),
        link(4, "YouTube - Wikipedia", "https://en.wikipedia.org/wiki/YouTube"))
    links = organic_links(snap)
    assert [e.id for e in links] == [3, 4]
    assert pick_by_host(links, "youtube").id == 3


def test_ads_are_found_in_titles_contexts_and_groups_but_not_inside_other_words():
    snap = results(link(1, "Promoted result", "https://a.example/"), link(2, "Home", "https://b.example/", 'section "Ads"'),
                   link(3, "Home", "https://c.example/", path="AXWebArea > AXGroup:Sponsored"),
                   link(4, "Adobe Reader download", "https://adobe.example/"), link(5, "Broad ideas", "https://d.example/"))
    assert [e.id for e in organic_links(snap)] == [4, 5]


def test_links_to_the_engine_itself_and_non_http_links_are_excluded():
    snap = results(link(1, "Images", "https://search.example/images?q=x"), link(2, "Maps", "https://maps.search.example/"),
                   link(3, "Mail", "mailto:a@b.c"), link(4, "No href", ""), link(5, "Real", "https://real.example/"))
    assert [e.id for e in organic_links(snap)] == [5]


def test_the_host_with_most_of_the_name_wins_and_page_order_breaks_ties():
    links = organic_links(results(link(1, "Other", "https://other.example/"), link(2, "UIUC", "https://www.uiuc.edu/"),
                                  link(3, "Portal", "https://portal.uiuc.edu/")))
    assert pick_by_host(links, "uiuc portal").id == 3
    assert pick_by_host(links, "uiuc").id == 2
    assert pick_by_host(links, "zzz") is None
    assert host_of("https://WWW.Example.org:8080/x") == "www.example.org"


# --- the loop ----------------------------------------------------------------------------------------------------------


class SearchBridge(FakeBridge):
    def __init__(self, results_page, landing, **kw):
        super().__init__(**kw)
        self.results_page, self.landing = results_page, landing

    mine = None  # the tab the search opened; None: the bridge could not identify it

    def search_in_tab(self, bundle, query, stage=None):
        from local_agent.act.browser import SearchOutcome, TabInfo

        self.calls.append(("search", query))
        if stage:
            stage("waiting_for_page")
        self.cur[0] = self.results_page
        self.mine = TabInfo(1, 20, "S1", "file:///blank")
        return SearchOutcome("verified via AX value", self.mine)

    def active_tab(self, bundle):
        return self.mine

    def navigate_tab(self, bundle, url):
        self.calls.append(("navigate", url))
        self.cur[0] = self.landing


class Chooser(FakeSelector):
    def __init__(self, index=1):
        super().__init__()
        self.index, self.asked = index, []

    def choose(self, goal, snap, step, candidates, describe=None):
        self.asked.append((step.target.description, [c.title for c in candidates]))
        return Selection(candidates[self.index], 0.9, 0.9, len(candidates), 1)


def go(goal, bridge, steps, selector=None, confirm=True, **settings):
    from tests.test_tab_navigation import run as _run  # same harness, our bridge and selector

    cur, scopes, asked = [dashboard_snapshot()], [], []
    bridge.cur = cur

    class Planner(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            scopes.append(scope)
            return super().next_step(goal, scope, history, screen)

    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, "x"), run_terminal=lambda c: Outcome(True, ""),
                  sleep=lambda s: None, confirm_terminal=lambda c, w: asked.append((c, w)) or confirm, browser=bridge)
    agent = Agent(Settings(**settings), Planner(steps), selector or FakeSelector(), hooks)
    agent.apps = ["Arc"]
    return agent.run(goal), scopes, asked, cur


YT = page("https://www.youtube.com/", [SEARCH])


def test_a_bare_name_goes_search_then_result_then_the_fact_holds():
    res = results(link(1, ("Ad"), "https://ads.example/", 'section "Sponsored"'), link(2, "YouTube", "https://www.youtube.com/"))
    bridge = SearchBridge(res, YT)
    result, scopes, asked, _ = go('open youtube in arc and search "dream"', bridge, [DONE] * 3)
    assert bridge.calls == [("search", "youtube"), ("navigate", "https://www.youtube.com/")] and asked == []
    assert "opened the result www.youtube.com" in result.history[0].note
    assert scopes[0]["progress"]["next"] == 'type "dream" into the "Search" field, then press return'


def test_no_host_match_asks_jev_and_the_user_even_in_turbo():
    res = results(link(1, "Campus news", "https://news.example/"), link(2, "Student portal", "https://my.campus.example/"))
    land = page("https://my.campus.example/")
    bridge, chooser = SearchBridge(res, land), Chooser(index=1)
    result, _, asked, _ = go("open acme portal in arc", bridge, [DONE] * 3, selector=chooser, turbo=True)
    assert chooser.asked[0][0] == "the official site for acme portal" and chooser.asked[0][1] == ["Campus news", "Student portal"]
    assert asked == [('open my.campus.example for "acme portal"', "search result does not match the name")]
    assert bridge.calls[-1] == ("navigate", "https://my.campus.example/")
    assert result.success  # the fact is aliased to the chosen host, so the planner's done is accepted


def test_declining_the_unmatched_result_leaves_a_note_and_the_planner_continues():
    res = results(link(1, "Campus news", "https://news.example/"))
    bridge = SearchBridge(res, page("https://news.example/"))
    result, scopes, asked, _ = go("open acme portal in arc", bridge, [DONE] * 4, selector=Chooser(0), confirm=False,
                                  max_consecutive_failures=2)
    assert not any(c[0] == "navigate" for c in bridge.calls) and len(asked) >= 1
    assert 'user declined news.example for "acme portal"' in result.history[0].note
    assert len(scopes) >= 1  # the planner took over


def test_a_tld_goal_goes_direct_and_never_searches():
    bridge = SearchBridge(results(), YT)
    go("open youtube.com in arc", bridge, [DONE] * 3)
    assert bridge.calls == [("open", "https://youtube.com")]


def test_without_a_bridge_a_bare_name_is_not_guessed_as_dot_com():
    cur, ran = [dashboard_snapshot()], []
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, ""),
                  run_terminal=lambda c: ran.append(c) or Outcome(True, ""), sleep=lambda s: None)
    agent = Agent(Settings(), ScriptedPlanner([DONE] * 4), FakeSelector(), hooks)
    agent.apps = ["Arc"]
    result = agent.run("open youtube in arc")
    assert ran == [] and "no scriptable browser" in result.history[0].note


def test_multi_word_names_and_their_host_match():
    from local_agent.goal_facts import Fact, _holds, parse_facts

    facts = [(f.kind, f.value) for f in parse_facts("go to the uiuc portal in safari", ["Safari"])]
    assert facts == [("app", "Safari"), ("site", "uiuc portal")]
    assert [(f.kind, f.value) for f in parse_facts("open new chat in Claude", ["Claude"]) if f.kind == "site"] == []
    assert [(f.kind, f.value) for f in parse_facts("go to the next page", []) if f.kind == "site"] == []
    f = Fact("site", "uiuc portal")
    assert _holds(f, Snapshot("", "", 0, "", [], url="https://portal.uiuc.edu/")) is True
    assert _holds(f, Snapshot("", "", 0, "", [], url="https://uiucportal.example/")) is True
    assert _holds(f, Snapshot("", "", 0, "", [], url="https://www.uiuc.edu/")) is False


def test_scripts_for_the_search_route():
    from local_agent.act import browser
    from local_agent.act.browser import BrowserError

    assert 'set URL of active tab of front window to "https://a.org/x"' in browser.navigate_script("select", "b", "https://a.org/x")
    assert "set URL of current tab of front window" in browser.navigate_script("current", "s", "https://a.org/")
    assert "about:blank" in browser.open_script("select", "b", "about:blank", allow_blank=True)
    for bad in ("about:blank", "javascript:1"):
        with pytest.raises(BrowserError):
            browser.navigate_script("select", "b", bad)
    with pytest.raises(BrowserError):
        browser.open_script("select", "b", "about:blank")
