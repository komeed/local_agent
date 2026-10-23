import pytest

from local_agent.closing import chord_family, command_family, goal_allows, name_family
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.selector.jev import Selection
from tests.test_loop import DONE, FakeSelector, P, make

BLOCKED = ["cmd+w", "cmd+shift+w", "cmd+option+w", "command+W", "Cmd + Opt + w", "alt+cmd+w", "cmd+q", "cmd+option+q",
           "cmd+tab", "cmd+shift+tab", "ctrl+left", "control+right", "cmd+h", "cmd+option+h", "cmd+m"]  # fmt: skip


@pytest.mark.parametrize("keys", BLOCKED)
def test_chords_are_recognised_after_normalising(keys):
    assert chord_family(keys)


@pytest.mark.parametrize("keys", ["cmd+n", "cmd+s", "return", "escape", "cmd+a", "tab", "cmd+shift+t", "left"])
def test_ordinary_chords_are_not(keys):
    assert chord_family(keys) is None


@pytest.mark.parametrize("name,fam", [("Close", "close"), ("Close Window ⌘W", "close"), ("Close Tab", "close"),
                                      ("Close All", "close"), ("Quit Safari", "quit"), ("Quit…", "quit"),
                                      ("Hide Others", "hide"), ("Minimize", "minimize"), ("Minimise", "minimize")])  # fmt: skip
def test_names(name, fam):
    assert name_family(name) == fam


@pytest.mark.parametrize("name", ["Closed captions", "New Window", "Save", "Closet", "Hidden files", "Quite nice"])
def test_other_names_pass(name):
    assert name_family(name) is None


def test_kill_commands():
    for c in ["killall Safari", "pkill -f foo", "kill 123", "osascript -e 'tell application \"Safari\" to quit'"]:
        assert command_family(c) == "quit"
    assert command_family("open -a Safari") is None and command_family("ls ~/skill") is None


@pytest.mark.parametrize("goal,fam,ok", [
    ("close the window", "close", True), ("quit TextEdit", "quit", True), ("switch to Safari", "switch", True),
    ("go back to Notes", "switch", True), ("hide the dock", "hide", True), ("minimise this", "minimize", True),
    ("find my closet photos", "close", False), ("close the window", "quit", False), ("quit TextEdit", "close", False),
    ("open new chat in Arc", "switch", False)])  # fmt: skip
def test_goal_words(goal, fam, ok):
    assert goal_allows(goal, fam) is ok


def run(textedit, goal, step):
    agent, _, executed, ran = make(textedit, [step, DONE], max_steps=3)
    res = agent.run(goal)
    return res, executed, ran


@pytest.mark.parametrize("keys", ["cmd+w", "Command+Q", "cmd+tab"])
def test_blocked_shortcuts_do_not_run(textedit, keys):
    res, executed, _ = run(textedit, "type hello", P(action="shortcut", keys=keys))
    assert executed == [] and "would close/quit/switch apps, which the goal doesn't ask for; work on:" in res.history[0].note


def test_allowed_when_the_goal_asks(textedit):
    res, executed, _ = run(textedit, "close the window", P(action="shortcut", keys="cmd+w"))
    assert [a for a, _ in executed] == ["shortcut"]
    res, executed, _ = run(textedit, "close the window", P(action="shortcut", keys="cmd+q"))
    assert executed == []  # only the close family


def test_terminal_kill_is_blocked_unless_asked(textedit):
    res, _, ran = run(textedit, "open Calculator", P(action="terminal", command="killall Safari"))
    assert "killall Safari" not in ran and any("would close/quit" in r.note for r in res.history)
    res, _, ran = run(textedit, "quit Safari", P(action="terminal", command="killall Safari"))
    assert not any("would close/quit" in r.note for r in res.history)  # goal asks: normal safety path decides


def test_close_buttons_and_menu_items_are_blocked_but_not_in_dialogs(textedit):
    class Pick(FakeSelector):
        def __init__(self, el):
            super().__init__()
            self.el = el

        def select(self, goal, snap, step):
            return Selection(self.el, 0.9, 0.9, 5, 1)

    close = UIElement(id=99, role="AXButton", title="Close", frame=Frame(0, 0, 10, 10), path="AXWindow:Doc")
    alert = UIElement(id=98, role="AXButton", title="Close", frame=Frame(0, 0, 10, 10), path="AXWindow:Doc > AXSheet")
    web = UIElement(id=97, role="AXButton", title="Close", frame=Frame(0, 0, 10, 10), path="AXWebArea > AXDialog:Cookies")
    step = P(action="click", target={"role": "AXButton", "description": "Close"})
    for el, blocked in [(close, True), (alert, False), (web, False)]:
        agent, _, executed, _ = make(textedit, [step, DONE], selector=Pick(el), max_steps=3)
        res = agent.run("type hello")
        assert (executed == []) is blocked, el.path


def test_menu_action_close_is_blocked(textedit):
    from local_agent.observe.menus import MenuCommand

    textedit.menu_commands = [MenuCommand("File > Close", "cmd+w"), MenuCommand("File > Save", "cmd+s")]
    agent, _, executed, _ = make(textedit, [P(action="menu", command="File > Close"), DONE], max_steps=3, menu_actions=True)
    res = agent.run("type hello")
    assert executed == [] and "would close/quit/switch apps" in res.history[0].note


def test_loop_refuses_both_and_continues(textedit):
    steps = [P(action="shortcut", keys="cmd+tab"), P(action="shortcut", keys="cmd+w"), P(action="shortcut", keys="cmd+n"), DONE]
    agent, planner, executed, _ = make(textedit, steps, max_steps=5)
    res = agent.run("open new chat in Arc")
    refused = [r for r in res.history if "would close/quit/switch" in r.note]
    assert [r.plan for r in refused] == ["shortcut keys=cmd+tab", "shortcut keys=cmd+w"] and not any(r.executed for r in refused)
    assert ("shortcut", None) in executed  # the run went on to cmd+n
