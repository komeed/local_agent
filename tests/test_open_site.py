from evals.web_snapshots import dashboard_snapshot
from local_agent.config import Settings
from local_agent.loop import Agent, Hooks
from local_agent.observe.models import Frame, Snapshot, UIElement
from tests.test_loop import DONE, FakeSelector, Outcome, P, ScriptedPlanner


def page(url, app="Arc", elements=()):
    return Snapshot(app=app, bundle_id="x", pid=1, window_title="t", elements=list(elements), url=url)


class NoSafety:
    """A cosine-like selector: no command_safety, so every non-turbo terminal command asks."""

    def select(self, goal, snap, step):
        raise AssertionError

    def verify(self, *a):
        return 0.9


def run(goal, start, after, steps=None, selector=None, confirm=True, apps=("Arc", "Safari"), **settings):
    """`after(command)` -> the state after that terminal command (None: unchanged)."""
    cur, ran, asked, scopes = [start], [], [], []

    class Planner(ScriptedPlanner):
        def next_step(self, goal, scope, history, screen=None):
            scopes.append(scope)
            return super().next_step(goal, scope, history, screen)

    def run_terminal(cmd):
        ran.append(cmd)
        if (state := after(cmd)) is not None:
            cur[0] = state
        return Outcome(True, "exit=0")

    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, "x"), run_terminal=run_terminal,
                  confirm_terminal=lambda c, w: asked.append(c) or confirm, sleep=lambda s: None)
    agent = Agent(Settings(**settings), Planner(steps or [DONE] * 4), selector or FakeSelector(), hooks)
    agent.apps = list(apps)
    return agent.run(goal), ran, asked, scopes


SEARCH = UIElement(id=1, role="AXSearchField", title="Search", frame=Frame(0, 0, 9, 9), path="AXWebArea")


def test_opens_the_browser_then_the_site_through_it():
    xcode = page("", app="Xcode")

    def after(cmd):
        return page("https://www.youtube.com/", elements=[SEARCH]) if "youtube" in cmd else dashboard_snapshot()

    result, ran, _, scopes = run('open youtube.com in arc and search "dream"', xcode, after, [DONE] * 3)
    assert ran == ['open -a "Arc"', 'open -a "Arc" "https://youtube.com"']
    assert [h.note.startswith("opened by the agent (goal names site youtube.com)") for h in result.history][1]
    assert scopes[0]["progress"]["next"] == 'type "dream" into the "Search" field, then press return'
    assert scopes[0]["progress"]["done"][:2] == ["open Arc", 'go to youtube.com: click the address bar, type "youtube.com" and press return']


def test_plain_open_when_no_browser_is_named():
    _, ran, _, _ = run("open github.com", page("", app="Xcode"), lambda c: page("https://github.com/"), [DONE] * 2)
    assert ran == ['open "https://github.com"']


def test_a_wrong_guess_leaves_a_note_and_is_never_reopened():
    stay = dashboard_snapshot()
    result, ran, _, _ = run("open foo.com in arc", stay, lambda c: None, [P(action="shortcut", keys="cmd+l")] + [DONE] * 5, max_steps=6)
    assert ran == ['open -a "Arc" "https://foo.com"']
    note = result.history[0].note
    assert "opened https://foo.com, but the tab is on learn.example.org; the site check doesn't hold" in note
    assert any(h.plan.startswith("shortcut") for h in result.history)  # the planner took over


def test_a_redirect_from_our_own_navigation_counts_as_reached():
    result, ran, _, _ = run("open gmail.com in arc", dashboard_snapshot(), lambda c: page("https://mail.google.com/mail/u/0/"),
                            [DONE] * 2)
    assert ran == ['open -a "Arc" "https://gmail.com"']
    assert "reached via redirect: mail.google.com" in result.history[0].note
    assert result.success  # the planner's done is accepted: the alias keeps the fact true


def test_the_command_asks_without_a_safety_check_and_declining_hands_over():
    result, ran, asked, _ = run("open youtube.com in arc", dashboard_snapshot(), lambda c: page("https://youtube.com/"),
                                [DONE] * 4, selector=NoSafety(), confirm=False, max_consecutive_failures=2)
    assert asked == ['open -a "Arc" "https://youtube.com"'] and ran == []
    assert "user declined" in result.history[0].note
    asked.clear()
    _, ran, asked, _ = run("open youtube.com in arc", dashboard_snapshot(), lambda c: page("https://youtube.com/"),
                           [DONE] * 2, selector=NoSafety(), confirm=True)
    assert asked == ['open -a "Arc" "https://youtube.com"'] and ran == asked


def test_dry_run_reports_without_running():
    from local_agent.config import Settings

    cur = [dashboard_snapshot()]
    hooks = Hooks(observe=lambda: cur[0], execute=lambda s, e: Outcome(True, ""),
                  run_terminal=lambda c: (_ for _ in ()).throw(AssertionError("ran")), sleep=lambda s: None)
    agent = Agent(Settings(), ScriptedPlanner([DONE] * 4), FakeSelector(), hooks, dry_run=True)
    agent.apps = ["Arc"]
    result = agent.run("open youtube.com in arc")
    assert result.history[0].note.startswith("opened by the agent (goal names site youtube.com); dry-run: would run")
