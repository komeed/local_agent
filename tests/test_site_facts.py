import pytest

from evals.planner_cases import CASES
from evals.web_snapshots import dashboard_snapshot
from local_agent.goal_facts import (Fact, _holds, checkable_facts, decisive_facts, parse_facts, progress_scope,
                                    rest_of_goal)
from local_agent.observe import screen_view
from local_agent.observe.models import Frame, Snapshot, UIElement
from tests.test_loop import DONE, FakeSelector, P, ScriptedPlanner, Outcome

APPS = ["Arc", "Safari", "Google Chrome", "TextEdit", "Mail", "Notes"]


def kinds(goal):
    return [(f.kind, f.value) for f in parse_facts(goal, APPS)]


@pytest.mark.parametrize("goal,expected", [
    ("open github.com", [("site", "github.com")]),
    ("open youtube in arc", [("app", "Arc"), ("site", "youtube")]),
    ('go to youtube in Safari and search "lofi"', [("app", "Safari"), ("site", "youtube"), ("search", "lofi")]),
    ("visit reddit", [("site", "reddit")]),
    ("open ChatGPT in Safari", [("app", "Safari"), ("site", "chatgpt")]),
    ("visit https://docs.python.org/3", [("site", "docs.python.org")]),
])
def test_site_and_search_facts(goal, expected):
    assert kinds(goal) == expected


@pytest.mark.parametrize("goal", ["open TextEdit", "open Mail", "search my files", "search for cats",
                                  "open notes.txt", "open a new document in textedit", "open new chat in Claude",
                                  "open the settings"])
def test_non_matches(goal):
    assert not [k for k in kinds(goal) if k[0] in ("site", "search")]


def snap(url="", app="Arc", **kw):
    kw.setdefault("window_title", "t")
    return Snapshot(app=app, bundle_id="x", pid=1, elements=kw.pop("elements", []), url=url, **kw)


def test_site_holds_by_host_label_or_domain():
    yt, gh = Fact("site", "youtube"), Fact("site", "github.com")
    assert _holds(yt, snap("https://www.youtube.com/results?search_query=dream")) is True
    assert _holds(yt, snap("https://m.youtube.com/")) is True
    assert _holds(yt, snap("https://notyoutube.com/")) is False
    assert _holds(gh, snap("https://github.com/x")) is True
    assert _holds(gh, snap("https://gist.github.com/")) is True
    assert _holds(gh, snap("https://notgithub.com/")) is False
    # no URL: a title naming the site is not evidence
    assert _holds(yt, snap("", window_title="YouTube")) is None


def test_search_holds_by_decoded_query():
    f = Fact("search", "dream")
    assert _holds(f, snap("https://www.youtube.com/results?search_query=Dream")) is True
    assert _holds(Fact("search", "lo fi"), snap("https://x.com/s?q=lo+fi")) is True
    assert _holds(Fact("search", "lo fi"), snap("https://x.com/s?q=lo%20fi")) is True
    assert _holds(f, snap("https://www.youtube.com/")) is None


GO_TO = 'go to youtube: search for "youtube" in the "Address" field, press return and open the official site from the results'
GOAL = 'open youtube in arc and search "dream"'


def test_site_and_search_are_decisive_with_the_app():
    assert [f.kind for f in decisive_facts(GOAL, APPS)] == ["app", "site", "search"]
    assert decisive_facts(GOAL + " and subscribe", APPS) == []
    assert decisive_facts("open github.com", APPS) != []


def test_without_a_url_done_is_not_gated_on_site_and_search():
    facts = decisive_facts(GOAL, APPS)
    assert [f.kind for f in checkable_facts(facts, snap(""))] == ["app"]
    assert len(checkable_facts(facts, snap("https://youtube.com"))) == 3


def test_checklist_wording_reads_the_screen():
    facts = parse_facts(GOAL, APPS, progress=True)
    on_dash = dashboard_snapshot()
    p = progress_scope(facts, on_dash)
    assert p["done"] == ["open Arc"]
    assert p["next"] == GO_TO
    assert p["remaining"] == ['search for "dream"']
    field = UIElement(id=1, role="AXSearchField", title="Search", frame=Frame(0, 0, 9, 9), path="AXWebArea")
    yt = snap("https://www.youtube.com/", elements=[field])
    assert progress_scope(facts, yt)["next"] == 'type "dream" into the "Search" field, then press return'
    done = snap("https://www.youtube.com/results?search_query=dream")
    assert progress_scope(facts, done) == {"done": ['open Arc', 'go to youtube: search for "youtube" in the address bar, press return and open the official site from the results',
                                                    'search for "dream"'], "next": None, "remaining": []}


def test_next_is_never_empty_while_the_facts_cannot_end_the_run():
    goal = 'open youtube in arc and search "dream" and subscribe to the first channel'
    facts = parse_facts(goal, APPS, progress=True)
    assert decisive_facts(goal, APPS) == []
    rest = rest_of_goal(goal, facts)
    assert rest == "subscribe to the first channel"
    p = progress_scope(facts, snap("https://www.youtube.com/results?search_query=dream"), rest=rest)
    assert p["next"] == "the rest of the goal: subscribe to the first channel" and p["remaining"] == []
    p = progress_scope(facts, dashboard_snapshot(), rest=rest)
    assert p["next"].startswith("go to youtube") and p["remaining"][-1] == "the rest of the goal: subscribe to the first channel"


# --- the screen when the next item is a site we're not on --------------------------------------------------------------


def test_the_current_page_is_collapsed_until_we_are_on_the_site():
    s = dashboard_snapshot()
    full = screen_view.render(s, 6000)
    short = screen_view.render(s, 6000, collapse_page="youtube")
    assert "Open record 3" in full and "Open record" not in short and "Student" not in short
    assert "Page: learn.example.org (" in short and "controls hidden: not needed until you are on youtube)" in short
    assert 'window "Course dashboard"' not in short and "Address" in short and "Inbox" in short  # browser chrome stays
    assert "File" in short  # menu bar stays
    assert len(short) < len(full) / 4


def test_dialogs_on_the_page_survive_the_collapse():
    s = dashboard_snapshot()
    s.elements.append(UIElement(id=999, role="AXButton", title="Accept", path="AXWebArea > AXDialog:Cookies",
                                frame=Frame(0, 0, 9, 9)))
    assert 'button "Accept"' in screen_view.render(s, 6000, collapse_page="youtube")


# --- the 1123 run, synthetic ------------------------------------------------------------------------------------------


def test_loop_follows_the_checklist_and_ends_by_facts():
    from local_agent.config import Settings
    from local_agent.loop import Agent, Hooks

    states = [dashboard_snapshot(), dashboard_snapshot(),
              snap("https://www.youtube.com/", elements=[UIElement(id=0, role="AXSearchField", title="Search",
                                                                   value="dream", frame=Frame(0, 0, 9, 9), path="AXWebArea")]),
              snap("https://www.youtube.com/results?search_query=dream")]
    cur = [0]
    ran, screens, scopes = [], [], []

    class Planner(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            scopes.append(scope)
            screens.append(screen)
            return super().next_step(goal, scope, history, screen)

    def run_terminal(cmd):
        ran.append(cmd)
        cur[0] = 2  # code opened the site: the tab is now on it, search box filled
        return Outcome(True, "exit=0")

    def execute(step, el):
        ran.append(step.summary())
        cur[0] = 3
        return Outcome(True, "pressed")

    steps = [P(action="shortcut", keys="return")]
    hooks = Hooks(observe=lambda: states[cur[0]], execute=execute, run_terminal=run_terminal, sleep=lambda s: None,
                  confirm_terminal=lambda c, w: True)  # fmt: skip
    agent = Agent(Settings(), Planner(steps), FakeSelector(), hooks)
    result = agent.run(GOAL.replace('youtube', 'youtube.com'))
    assert result.success and result.reason.startswith("goal facts hold"), result.reason
    assert ran == ['open -a "Arc" "https://youtube.com"', "shortcut keys=return"]
    assert scopes[0]["progress"]["next"] == 'type "dream" into the "Search" field, then press return'
    assert "Open record" not in screens[0] and scopes[0]["window"] == "t"
