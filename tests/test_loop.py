from dataclasses import dataclass

from local_agent.config import Settings
from local_agent.loop import Agent, Hooks
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import Selection


@dataclass
class Outcome:
    ok: bool
    note: str = ""


class ScriptedPlanner:
    def __init__(self, steps):
        self.steps, self.seen, self.screens, self.scopes = list(steps), [], [], []

    def next_step(self, goal, scope, history, screen=None):
        self.seen.append(list(history))
        self.scopes.append(scope)
        self.screens.append(screen)
        return self.steps.pop(0)


class FakeSelector:
    def __init__(self, prob=0.9, verify=0.9, safety=0.95):
        self.prob, self.verify_p, self.safety = prob, verify, safety
        self.selects = self.safety_checks = 0

    def command_safety(self, goal, step, snap, history):
        self.safety_checks += 1
        return self.safety

    def select(self, goal, snap, step):
        self.selects += 1
        return Selection(snap.elements[5], self.prob, self.prob, len(snap.elements), 1)

    def verify(self, goal, step, element, before, after):
        return self.verify_p


def P(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw)


DONE = PlannerOutput(done=True, reasoning="finished")


def make(textedit, steps, selector=None, confirm=True, asked=None, start_app=None, **settings):
    executed, ran = [], []
    asked = [] if asked is None else asked
    hooks = Hooks(
        observe=lambda: textedit,
        execute=lambda step, el: executed.append((step.action, el)) or Outcome(True, "did it"),
        run_terminal=lambda cmd: ran.append(cmd) or Outcome(True, "exit=0"),
        confirm_terminal=lambda cmd, why: asked.append((cmd, why)) or confirm,
        sleep=lambda s: None,
    )
    planner = ScriptedPlanner(steps)
    agent = Agent(Settings(**settings), planner, selector or FakeSelector(), hooks, start_app=start_app)
    return agent, planner, executed, ran


def test_happy_path_feeds_history_back(textedit):
    steps = [P(action="click", target={"role": "AXMenuBarItem", "description": "File"}), P(action="shortcut", keys="cmd+n"), DONE]
    agent, planner, executed, _ = make(textedit, steps)
    result = agent.run("new doc")
    assert result.success and len(result.history) == 2
    assert executed[0][1].title == "File" and executed[1] == ("shortcut", None)
    last_seen = planner.seen[-1]
    assert last_seen[0].verified and last_seen[0].element.startswith("AXMenuBarItem")


def test_low_probability_skips_action(textedit):
    steps = [P(action="click", target={"role": "AXButton", "description": "x"})] * 3
    agent, _, executed, _ = make(textedit, steps, selector=FakeSelector(prob=0.2))
    result = agent.run("g")
    assert not result.success and executed == []
    assert "below threshold" in result.history[0].note and len(result.history) == 3


def test_failed_verification_counts_as_failure(textedit):
    steps = [P(action="shortcut", keys="cmd+b")] * 3
    agent, _, executed, _ = make(textedit, steps, selector=FakeSelector(verify=0.1))
    result = agent.run("g")
    assert not result.success and len(executed) == 3
    assert result.history[0].executed and result.history[0].verified is False


def test_safe_terminal_command_runs_without_asking(textedit):
    asked = []
    agent, _, executed, ran = make(textedit, [P(action="terminal", command="open -a Calculator"), DONE], asked=asked)
    result = agent.run("g")
    assert result.success and ran == ["open -a Calculator"] and executed == [] and asked == []
    assert result.history[0].safety_probability == 0.95


def test_jev_doubt_asks_the_user(textedit):
    asked = []
    selector = FakeSelector(safety=0.3)
    steps = [P(action="terminal", command="mv ~/Desktop/a.txt ~/Documents/"), DONE]
    agent, _, _, ran = make(textedit, steps, selector=selector, asked=asked)
    result = agent.run("g")
    assert ran == ["mv ~/Desktop/a.txt ~/Documents/"] and "p=0.30" in asked[0][1]
    assert result.history[0].note.startswith("approved by the user (Jev rated it safe with p=0.30")


def test_dangerous_pattern_asks_even_when_jev_says_safe(textedit):
    asked = []
    selector = FakeSelector(safety=0.99)
    agent, _, _, ran = make(textedit, [P(action="terminal", command="rm -rf /tmp/x")] * 3, selector=selector,
                            confirm=False, asked=asked)  # fmt: skip
    result = agent.run("g")
    assert ran == [] and asked[0] == ("rm -rf /tmp/x", "deletes folders recursively") and selector.safety_checks == 0
    assert result.history[0].note == "user declined the terminal command (deletes folders recursively)"


def test_backend_without_safety_judgment_asks(textedit):
    asked = []
    selector = FakeSelector()
    selector.command_safety = lambda *a: None
    agent, _, _, ran = make(textedit, [P(action="terminal", command="ls"), DONE], selector=selector, asked=asked)
    agent.run("g")
    assert asked == [("ls", "this Jev model can't judge command safety")] and ran == ["ls"]


def test_turbo_runs_everything_unchecked(textedit):
    asked = []
    selector = FakeSelector(safety=0.0)
    agent, _, _, ran = make(textedit, [P(action="terminal", command="rm -rf /tmp/x"), DONE], selector=selector,
                            asked=asked, turbo=True)  # fmt: skip
    result = agent.run("g")
    assert ran == ["rm -rf /tmp/x"] and asked == [] and selector.safety_checks == 0
    assert result.history[0].safety_probability is None


def test_dry_run_reports_what_the_safety_check_would_do(textedit):
    agent, _, _, ran = make(textedit, [P(action="terminal", command="sudo rm x")])
    agent.dry_run = True
    result = agent.run("g")
    assert ran == [] and result.history[0].note == "dry-run: would run 'sudo rm x' after asking the user (needs administrator rights)"


def test_start_app_note_tells_planner_to_open_the_goal_app(textedit):
    agent, planner, _, _ = make(textedit, [DONE] * 4, start_app="TextEdit")
    seen_scopes = []
    original = planner.next_step
    planner.next_step = lambda goal, scope, history, screen=None: seen_scopes.append(scope) or original(goal, scope, history, screen)
    agent.run("open gmail in arc")
    assert "open -a" in seen_scopes[0]["note"]
    agent, planner, _, _ = make(textedit, [DONE], start_app="Terminal")
    planner.next_step = lambda goal, scope, history, screen=None: seen_scopes.append(scope) or DONE
    agent.run("g")
    assert "note" not in seen_scopes[-1]


def test_dry_run_does_not_act(textedit):
    agent, _, executed, _ = make(textedit, [P(action="click", target={"role": "AXButton", "description": "x"})])
    agent.dry_run = True
    result = agent.run("g")
    assert executed == [] and len(result.history) == 1 and result.history[0].probability == 0.9


def test_max_steps(textedit):
    steps = [P(action="shortcut", keys="tab")] * 5
    agent, _, executed, _ = make(textedit, steps, max_steps=4)
    result = agent.run("g")
    assert not result.success and "max steps" in result.reason and len(executed) == 4


def test_planner_number_pick_skips_jev(textedit):
    selector = FakeSelector()
    steps = [P(action="click", target={"id": 3}), DONE]
    agent, planner, executed, _ = make(textedit, steps, selector=selector)
    assert agent.run("toggle bold").success
    assert selector.selects == 0 and executed[0][1].label == "Bold"
    assert "[3]" in planner.screens[0] and 'checkbox "Bold"' in planner.screens[0]


def test_unknown_number_without_description_fails(textedit):
    steps = [P(action="click", target={"id": 999})] * 3
    agent, _, executed, _ = make(textedit, steps)
    result = agent.run("g")
    assert executed == [] and "not on the current screen" in result.history[0].note


def test_unknown_number_with_description_falls_back_to_jev(textedit):
    selector = FakeSelector()
    steps = [P(action="click", target={"id": 999, "role": "AXMenuBarItem", "description": "File"}), DONE]
    agent, _, executed, _ = make(textedit, steps, selector=selector)
    assert agent.run("g").success and selector.selects == 1


def test_view_modes(textedit):
    for mode, has_numbers in (("ids", True), ("names", False)):
        agent, planner, _, _ = make(textedit, [DONE], planner_view=mode)
        agent.run("g")
        assert ("[3]" in planner.screens[0]) is has_numbers and "Bold" in planner.screens[0]
    agent, planner, _, _ = make(textedit, [DONE], planner_view="off")
    agent.run("g")
    assert planner.screens == [None]


def test_guard_hands_wrong_twin_to_jev():
    from evals.selector_cases import in_row, invoice_list, invoice_name

    snap = invoice_list()
    wrong = next(e for e in snap.elements if in_row(invoice_name(0), title="Delete")(e))
    right = next(e for e in snap.elements if in_row(invoice_name(24), title="Delete")(e))

    class Picker(FakeSelector):
        def select(self, goal, snap, step):
            self.selects += 1
            return Selection(right, 0.95, 0.9, 50, 1)

    selector = Picker()
    steps = [P(action="click", target={"id": wrong.id, "role": "AXButton",
                                       "description": "Delete button for invoice-0425"}), DONE]  # fmt: skip
    agent, _, executed, _ = make(snap, steps, selector=selector)
    result = agent.run("delete invoice-0425")
    assert result.success and selector.selects == 1 and executed[0][1] is right
    assert f"[{wrong.id}] is one of 50 'Delete' controls" in result.history[0].note


class GoalSelector(FakeSelector):
    """FakeSelector whose goal check returns the next probability from `goal_ps` (None: can't judge)."""

    def __init__(self, goal_ps, **kw):
        super().__init__(**kw)
        self.goal_ps, self.goal_calls = list(goal_ps), 0

    def goal_done(self, goal, snap):
        self.goal_calls += 1
        return self.goal_ps.pop(0) if self.goal_ps else 0.0


def test_goal_check_ends_run_at_high_probability(textedit):
    selector = GoalSelector([0.2, 0.95])
    steps = [P(action="shortcut", keys="cmd+t"), P(action="type", text="gmail.com"), P(action="shortcut", keys="cmd+w")]
    agent, planner, executed, _ = make(textedit, steps, selector=selector)
    result = agent.run("open gmail")
    assert result.success and result.reason.startswith("Jev: the screen shows the goal done (p=0.95)")
    assert len(executed) == 2 and result.history[-1].goal_probability == 0.95


def test_middling_goal_probability_hints_the_planner(textedit):
    selector = GoalSelector([0.7])
    steps = [P(action="shortcut", keys="cmd+t"), DONE]
    agent, planner, _, _ = make(textedit, steps, selector=selector)
    assert agent.run("open gmail").success
    hinted = planner.seen[-1][0].for_planner()
    assert hinted["goal_probability"] == 0.7 and "may already show the goal done (p=0.70)" in hinted["note"]


def test_goal_check_skipped_for_failed_steps_and_when_off(textedit):
    selector = GoalSelector([0.99], verify=0.1)  # step fails verification
    agent, _, _, _ = make(textedit, [P(action="shortcut", keys="cmd+t"), DONE], selector=selector)
    agent.run("g")
    assert selector.goal_calls == 0
    selector = GoalSelector([0.99])
    agent, _, _, _ = make(textedit, [P(action="shortcut", keys="cmd+t"), DONE], selector=selector, goal_check=False)
    agent.run("g")
    assert selector.goal_calls == 0


def test_backend_without_goal_judgment_keeps_going(textedit):
    selector = GoalSelector([None])
    agent, _, _, _ = make(textedit, [P(action="shortcut", keys="cmd+t"), DONE], selector=selector)
    result = agent.run("g")
    assert result.success and result.history[0].goal_probability is None


def test_loop_guard_stops_a_repeated_sequence(textedit):
    block = [P(action="shortcut", keys="cmd+t"), P(action="type", text="gmail.com"), P(action="shortcut", keys="enter")]
    agent, planner, executed, _ = make(textedit, block * 5, selector=GoalSelector([]))
    result = agent.run("open gmail")
    assert not result.success and result.reason == "stopped: 3 steps succeeded 3 times in a row"
    assert len(executed) == 9
    from local_agent.history import loop_warning

    assert "successfully 2 times in a row" in loop_warning(planner.seen[6])  # what the planner was warned with


def test_loop_guard_success_when_jev_rated_goal_likely(textedit):
    steps = [P(action="shortcut", keys="cmd+t")] * 5
    agent, _, executed, _ = make(textedit, steps, selector=GoalSelector([0.6, 0.6, 0.6]))
    result = agent.run("open a tab")
    assert result.success and "Jev rated the goal likely done" in result.reason and len(executed) == 3


def test_navigation_keys_may_repeat(textedit):
    steps = [P(action="shortcut", keys="down")] * 4 + [DONE]
    agent, _, executed, _ = make(textedit, steps, selector=GoalSelector([]))
    assert agent.run("pick the 5th item").success and len(executed) == 4


# --- goal facts, retype skip, and the failed real run ---------------------------------------------------------------


def _snap(app, title, elements):
    from local_agent.observe.models import Snapshot

    return Snapshot(app=app, bundle_id="x." + app.lower(), pid=1, window_title=title, elements=elements)


def _area(value="", id=2):
    from local_agent.observe.models import UIElement

    return UIElement(id=id, role="AXTextArea", value=value, focused=True)


def test_retype_is_skipped_when_the_field_already_has_the_text(textedit):
    from dataclasses import replace

    area = replace(_area("Hello world"), id=1)
    snap = _snap("TextEdit", "Untitled", [area])
    steps = [P(action="type", text="hello world", target={"id": 1, "role": "AXTextArea", "description": "text area"}), DONE]
    agent, _, executed, _ = make(snap, steps)
    result = agent.run("g")
    assert executed == []
    rec = result.history[0]
    assert rec.succeeded and rec.note == 'already typed: the field contains "hello world"; not typed again'


def test_failed_real_run_ends_by_goal_facts_before_the_retype():
    from local_agent.observe.models import UIElement

    unnamed = UIElement(id=1, role="AXButton")  # a scroll-bar page button the planner read as "New"
    new_item = UIElement(id=3, role="AXMenuItem", title="New")
    states = {
        "finder": _snap("Xcode", "Project", [UIElement(id=1, role="AXButton", title="Run")]),  # LocalAgent hid itself
        "old": _snap("TextEdit", "old.txt", [unnamed, _area("old text"), new_item]),
        "untitled": _snap("TextEdit", "Untitled", [UIElement(id=1, role="AXButton"), _area(""), new_item]),
        "typed": _snap("TextEdit", "Untitled", [UIElement(id=1, role="AXButton"), _area("Hello world"), new_item]),
    }
    cur = ["finder"]
    executed, ran = [], []

    def execute(step, el):
        executed.append((step.action, el))
        cur[0] = "untitled" if step.action == "click" else "typed"  # File > New, then typing
        return Outcome(True, "did it")

    def run_terminal(cmd):
        ran.append(cmd)
        cur[0] = "old"  # TextEdit reopened an old file
        return Outcome(True, "exit=0")

    class Picker(FakeSelector):
        def select(self, goal, snap, step):
            self.selects += 1
            return Selection(next(e for e in snap.elements if e.title == "New"), 0.9, 0.9, 3, 1)

    hooks = Hooks(observe=lambda: states[cur[0]], execute=execute, run_terminal=run_terminal, sleep=lambda s: None)
    steps = [
        P(action="click", target={"id": 1, "role": "AXButton", "description": "New"}),
        P(action="type", text="hello world", target={"id": 2, "role": "AXTextArea", "description": "text area"}),
        P(action="type", text="hello world", target={"id": 2, "role": "AXTextArea", "description": "text area"}),
        DONE,
    ]
    planner = ScriptedPlanner(steps)
    selector = Picker()  # cosine-like: no goal check
    agent = Agent(Settings(), planner, selector, hooks)
    agent.apps = ["TextEdit", "Finder"]
    result = agent.run('open a new document in textedit and type "hello world"')
    assert result.success and result.reason.startswith("goal facts hold: TextEdit in front")
    assert ran == ['open -a "TextEdit"'] and len(result.history) == 3
    assert result.history[0].note.startswith("opened by the agent (goal names TextEdit)")
    assert result.history[1].element.startswith("AXMenuItem") and "does not match" in result.history[1].note
    # After the New click the planner is told the document exists and what comes next.
    after_click = planner.scopes[1]["progress"]
    assert "make a new empty document" in after_click["done"] and after_click["next"] == 'type "hello world" into the document'
    # Nothing was clicked in Xcode, and the retype was never planned.
    assert [a for a, _ in executed] == ["click", "type"] and len(planner.steps) == 2


def _run(states_cur, planner_steps, goal, apps=("TextEdit", "Safari"), **settings):
    """Agent over a mutable current snapshot (a one-item list); returns (agent, planner, executed, ran, result)."""
    executed, ran = [], []

    def execute(step, el):
        executed.append((step.action, step.keys))
        return Outcome(True, "did it")

    hooks = Hooks(observe=lambda: states_cur[0], execute=execute,
                  run_terminal=lambda cmd: ran.append(cmd) or Outcome(True, "exit=0"), sleep=lambda s: None)
    planner = ScriptedPlanner(planner_steps)
    agent = Agent(Settings(**settings), planner, FakeSelector(), hooks)
    agent.apps = list(apps)
    return planner, executed, ran, agent.run(goal)


def test_false_done_is_rejected_and_the_run_continues():
    snap = _snap("TextEdit", "Untitled", [_area("")])
    planner, _, _, result = _run([snap], [DONE, DONE, DONE, DONE], 'type "hello world" in TextEdit', max_consecutive_failures=3)
    assert not result.success and result.reason == "3 consecutive failed steps"
    assert result.history[0].note == 'not done: "hello world" isn\'t in any text field' and not result.history[0].succeeded
    assert len(planner.steps) == 1  # asked three times, not accepted once


def test_done_is_accepted_when_the_facts_hold():
    snap = _snap("TextEdit", "Untitled", [_area("hello world")])
    _, _, _, result = _run([snap], [DONE], 'type "hello world" in TextEdit')
    assert result.success


def test_goal_app_is_opened_first_even_when_not_decisive():
    from local_agent.observe.models import UIElement

    snap = _snap("Xcode", "p", [UIElement(id=1, role="AXButton", title="Run")])
    _, executed, ran, result = _run([snap], [DONE] * 4, "Open ChatGPT in Safari")
    assert ran == ['open -a "Safari"'] and executed == [] and result.history[0].step == 1


def test_no_open_when_the_app_is_already_in_front():
    snap = _snap("Safari", "Start", [])
    _, _, ran, _ = _run([snap], [DONE], "Open ChatGPT in Safari")
    assert ran == []  # the app isn't reopened; a bare site name is searched for by the browser bridge, never guessed as .com


def test_ax_typing_inserts_then_falls_back(monkeypatch):
    pytest = __import__("pytest")
    pytest.importorskip("ApplicationServices")
    from local_agent.act import executor
    from local_agent.observe.models import UIElement

    calls, value = [], {"v": ""}
    monkeypatch.setattr(executor, "_ax_get", lambda ref, name: value["v"] if name == "AXValue" else "win")
    monkeypatch.setattr(executor, "type_text", lambda t: calls.append(("keys", t)))
    monkeypatch.setattr(executor, "_focus", lambda el: calls.append(("focus",)))
    monkeypatch.setattr(executor, "_raise_window", lambda el: calls.append(("raise",)))
    el = UIElement(id=1, role="AXTextArea", ref=object())

    def took(ref, name, v):
        value["v"] = v
        return True

    monkeypatch.setattr(executor, "_ax_set", took)
    assert executor.type_text_into(el, "hi") == "inserted 2 chars via AX" and calls == []
    value["v"] = ""
    monkeypatch.setattr(executor, "_ax_set", lambda *a: True)  # accepted but the value didn't change
    assert executor.type_text_into(el, "hi") == "typed 2 chars"
    assert calls == [("raise",), ("focus",), ("keys", "hi")]


def test_quoted_text_guard_rejects_text_not_in_the_goal(textedit):
    steps = [P(action="type", text="Untitledhello"), P(action="type", text=" Hello World ")]
    agent, _, executed, _ = make(textedit, steps, max_steps=2)
    result = agent.run('type "hello world" and save as "helloworld.txt"')
    assert result.history[0].note == 'the goal\'s text is one of: "hello world", "helloworld.txt"; type one of them exactly'
    assert not result.history[0].succeeded and len(executed) == 1  # the second, matching one ran
    agent, _, executed, _ = make(textedit, [P(action="type", text="anything")], max_steps=1)
    agent.run("type something")
    assert len(executed) == 1  # no quoted strings in the goal: no check


def test_single_line_field_is_retyped_unless_it_equals_the_text():
    from local_agent.observe.models import UIElement

    field = UIElement(id=1, role="AXTextField", value="helloworld.txt extra")
    snap = _snap("TextEdit", "Save", [field])
    steps = [P(action="type", text="helloworld.txt", target={"id": 1, "description": "name field"})]
    agent, _, executed, _ = make(snap, steps, max_steps=1)
    agent.run("g")
    assert len(executed) == 1  # contains the text but isn't equal: typed (replacing)
    agent, _, executed, _ = make(_snap("TextEdit", "Save", [UIElement(id=1, role="AXTextField", value="helloworld.txt")]),
                                 steps, max_steps=1)  # fmt: skip
    assert agent.run("g").history[0].note.startswith("already typed")
    assert executed == []


def test_regression_ends_via_the_saved_fact(tmp_path):
    from local_agent.observe.models import UIElement

    path = tmp_path / "helloworld.txt"
    new_item = UIElement(id=3, role="AXMenuItem", title="New")
    states = {
        "old": _snap("TextEdit", "old.txt", [UIElement(id=1, role="AXButton"), _area("old"), new_item]),
        "untitled": _snap("TextEdit", "Untitled", [UIElement(id=1, role="AXButton"), _area(""), new_item]),
        "typed": _snap("TextEdit", "Untitled", [UIElement(id=1, role="AXButton"), _area("Hello world"), new_item]),
        "sheet": _snap("TextEdit", "Untitled", [
            _area("Hello world"),
            UIElement(id=4, role="AXTextField", title="Save As:", value="Untitled", selected="Untitled",
                      focused=True, path="AXWindow:Untitled > AXSheet"),
            UIElement(id=5, role="AXButton", title="Save", is_default=True, path="AXWindow:Untitled > AXSheet"),
        ]),  # fmt: skip
    }
    cur = ["old"]
    ran = []

    def execute(step, el):
        if step.action == "click":
            cur[0] = "untitled"
        elif step.action == "type" and cur[0] != "sheet":
            cur[0] = "typed"
        elif step.action == "shortcut":  # cmd+s opens the save sheet over the document
            cur[0] = "sheet"
        else:  # the name is typed: the document is written and the window takes its name
            path.write_text("Hello world")
            states["saved"] = _snap("TextEdit", "helloworld.txt", [_area("Hello world")])
            states["saved"].document_path = str(path)
            cur[0] = "saved"
        return Outcome(True, "did it")

    hooks = Hooks(observe=lambda: states[cur[0]], execute=execute, run_terminal=lambda c: ran.append(c) or Outcome(True),
                  sleep=lambda s: None)  # fmt: skip
    steps = [P(action="click", target={"id": 3, "description": "New"}),
             P(action="type", text="hello world", target={"id": 2, "description": "text area"}),
             P(action="shortcut", keys="cmd+s"),
             P(action="type", text="helloworld.txt", target={"id": 4, "description": "name field"}), DONE]  # fmt: skip
    planner = ScriptedPlanner(steps)
    agent = Agent(Settings(), planner, FakeSelector(), hooks)
    agent.apps = ["TextEdit"]
    result = agent.run('open a new document in textedit and type "hello world" and save it as "helloworld.txt"')
    assert result.success and "window titled \"helloworld.txt\"" in result.reason and len(result.history) == 4
    assert planner.scopes[2]["progress"]["next"] == 'save the document as "helloworld.txt"'  # typed, not yet saved
    assert planner.scopes[3]["progress"]["next"] == 'type "helloworld.txt" into the "Save As" field, then click "Save"'
    assert 'type "hello world" into the document' in planner.scopes[3]["progress"]["done"]


def test_open_of_the_app_already_in_front_is_not_run_and_not_asked(textedit):
    asked = []
    steps = [P(action="terminal", command='open -a "TextEdit"'), DONE]
    agent, _, _, ran = make(textedit, steps, asked=asked)
    agent.apps = ["TextEdit"]
    rec = agent.run("g").history[0]
    assert ran == [] and asked == [] and not rec.succeeded
    assert rec.note == ("TextEdit is already in front; opening it again does nothing. "
                        "Work on the next checklist item.")  # fmt: skip
    agent, _, _, ran = make(textedit, [P(action="terminal", command="open -a TextEdit notes.txt"), DONE])
    agent.run("g")
    assert ran == ["open -a TextEdit notes.txt"]  # opening a file is not a no-op


# --- the overwrite alert (a run where the save hit an existing file) and going back to "new document" ------------------

SAVE_GOAL = 'open a new document in textedit and type "hello world" and save it as "helloworld.txt"'


def _overwrite_states(path):
    from local_agent.observe.models import UIElement

    sheet = "AXWindow:Untitled > AXSheet"
    doc = UIElement(id=0, role="AXTextArea", value="Hello world", path="AXWindow:Untitled")
    states = {
        "sheet": _snap("TextEdit", "Untitled", [
            doc,
            UIElement(id=4, role="AXTextField", title="Save As:", value="Untitled", focused=True, path=sheet),
            UIElement(id=5, role="AXButton", title="Cancel", path=sheet),
            UIElement(id=6, role="AXButton", title="Save", is_default=True, path=sheet),
        ]),
        # What the real run showed: Cancel focused, Replace, two static texts, no document window title.
        "alert": _snap("TextEdit", "", [
            UIElement(id=0, role="AXStaticText", value="“helloworld.txt” already exists. Do you want to replace it?",
                      interactive=False, path=sheet),
            UIElement(id=1, role="AXStaticText", value="Replacing it will overwrite its current contents.",
                      interactive=False, path=sheet),
            UIElement(id=2, role="AXButton", title="Cancel", focused=True, path=sheet),
            UIElement(id=3, role="AXButton", title="Replace", path=sheet),
        ]),
    }  # fmt: skip
    saved = _snap("TextEdit", "helloworld.txt", [UIElement(id=0, role="AXTextArea", value="Hello world")])
    saved.document_path = str(path)
    states["saved"] = saved
    return states


def _overwrite_run(tmp_path, answer):
    path = tmp_path / "helloworld.txt"
    states, cur, pressed, asked = _overwrite_states(path), ["sheet"], [], []

    def execute(step, el):
        name = el.name() if el else step.keys
        pressed.append(name)
        if name == "Save":
            cur[0] = "alert"
        elif name == "Replace":
            path.write_text("Hello world")
            cur[0] = "saved"
        elif name == "Cancel":
            cur[0] = "sheet"
        return Outcome(True, f"pressed {name}")

    hooks = Hooks(observe=lambda: states[cur[0]], execute=execute, run_terminal=lambda c: Outcome(True),
                  confirm_terminal=lambda cmd, why: asked.append((cmd, why)) or answer, sleep=lambda s: None)  # fmt: skip
    steps = [P(action="type", text="helloworld.txt", target={"id": 4, "description": "name field"}),
             P(action="click", target={"id": 6, "description": "Save"}), DONE, DONE]  # fmt: skip
    planner = ScriptedPlanner(steps)
    agent = Agent(Settings(turbo=True), planner, FakeSelector(), hooks)  # turbo: it must ask anyway
    agent.apps = ["TextEdit"]
    return agent, planner, pressed, asked, agent.run(SAVE_GOAL)


def test_overwrite_alert_asks_once_presses_replace_and_ends_on_the_saved_fact(tmp_path):
    agent, planner, pressed, asked, result = _overwrite_run(tmp_path, True)
    assert asked == [('replace existing "helloworld.txt"', "would overwrite an existing file")]
    assert pressed[-2:] == ["Save", "Replace"]
    assert result.success and result.history[-1].note.startswith("overwrite confirmed by user")
    assert len(planner.steps) == 2  # the planner never saw the alert


def test_overwrite_declined_presses_cancel_and_ends(tmp_path):
    agent, _, pressed, asked, result = _overwrite_run(tmp_path, False)
    assert len(asked) == 1 and pressed[-2:] == ["Save", "Cancel"] and "Replace" not in pressed
    assert not result.success and result.reason == "overwrite_declined"
    assert result.history[-1].note.startswith('overwrite declined: "helloworld.txt" already exists')


def test_alert_not_caused_by_our_typing_is_left_to_the_planner_with_a_checklist_line():
    states = _overwrite_states("x")
    agent, planner, _, _ = make(states["alert"], [DONE], max_steps=1, confirm=False)
    agent.apps = ["TextEdit"]
    asked = []
    agent.hooks.confirm_terminal = lambda c, w: asked.append(c) or False
    agent.run(SAVE_GOAL)  # nothing typed the name in this run
    assert asked == []
    assert planner.scopes[0]["progress"]["next"] == ('answer the alert: "helloworld.txt already exists": '
                                                     'click "Replace" to overwrite or "Cancel"')  # fmt: skip


def test_new_document_steps_are_refused_once_one_is_open():
    from local_agent.observe.models import UIElement

    new_btn = UIElement(id=3, role="AXButton", title="New Document")
    snap = _snap("TextEdit", "Untitled", [_area(""), new_btn])
    goal = 'open a new document in textedit and type "hello world"'
    for step in (P(action="shortcut", keys="cmd+n"), P(action="click", target={"id": 3, "description": "New Document"})):
        agent, _, executed, _ = make(snap, [step, DONE], max_steps=1)
        agent.apps = ["TextEdit"]
        rec = agent.run(goal).history[0]
        assert executed == [] and rec.note == 'a new document is already open; work on: type "hello world" into the document'
    # Before a document is open, the same step is fine.
    old = _snap("TextEdit", "old.txt", [_area("old"), new_btn])
    agent, _, executed, _ = make(old, [P(action="shortcut", keys="cmd+n"), DONE], max_steps=1)
    agent.apps = ["TextEdit"]
    agent.run(goal)
    assert len(executed) == 1


# --- "new <thing>" facts and the no-change repeat hint (a live run: click New, then New again, three times) ----------


def _claude_states():
    from local_agent.observe.models import UIElement

    side = "AXWindow:Claude > AXGroup:Sidebar"
    new = UIElement(id=1, role="AXButton", title="New", path=side)
    toggle = UIElement(id=2, role="AXButton", title="Toggle sidebar", path="AXWindow:Claude")
    return {
        "other": _snap("Xcode", "Project", [UIElement(id=1, role="AXButton", title="Run")]),
        "home": _snap("Claude", "Claude", [new, toggle]),
        "chat": _snap("Claude", "Claude", [new, toggle, UIElement(id=3, role="AXTextArea", placeholder="Reply")]),
        "toggled": _snap("Claude", "Claude", [new, UIElement(id=2, role="AXButton", title="Toggle sidebar", path="x")]),
    }


def _claude_run(steps, goal, after_click):
    states, cur = _claude_states(), ["other"]
    executed = []

    def execute(step, el):
        executed.append(el.name() if el else step.keys)
        cur[0] = after_click.get(cur[0], cur[0])  # a click only changes the screen from some states
        return Outcome(True, "did it")

    hooks = Hooks(observe=lambda: states[cur[0]], execute=execute,
                  run_terminal=lambda c: cur.__setitem__(0, "home") or Outcome(True, "exit=0"), sleep=lambda s: None)  # fmt: skip
    planner = ScriptedPlanner(steps)

    class Heuristic(FakeSelector):  # like cosine's verify: a change is a success, none is 0.1
        def verify(self, goal, step, element, before, after):
            return 0.8 if before is not after else 0.1

    agent = Agent(Settings(), planner, Heuristic(), hooks)
    agent.apps = ["Claude"]
    return agent, planner, executed, agent.run(goal)


def test_new_conversation_goal_ends_after_the_first_new_click():
    new = {"id": 1, "role": "AXButton", "description": "New"}
    steps = [P(action="click", target=new), P(action="click", target=new), P(action="click", target=new)]
    _, planner, executed, result = _claude_run(steps, "open new conversation in claude", {"home": "chat"})
    assert result.success and result.reason == "goal facts hold: Claude in front, a new conversation open"
    assert executed == ["New"] and len(result.history) == 2 and len(planner.steps) == 2  # no second New


def test_no_change_repeat_gets_a_hint_is_not_a_failure_and_ends_the_run():
    tog = {"id": 2, "role": "AXButton", "description": "Toggle sidebar"}
    goal = 'open new conversation in claude and type "hi"'  # not decisive: the repeats are what we look at
    agent, _, executed, result = _claude_run([P(action="click", target=tog)] * 5, goal, {"home": "toggled"})
    notes = [h.note for h in result.history]
    assert "nothing changed: this was already done at step 2. If the goal is met, answer done" in notes[2]
    assert result.history[2].succeeded and result.history[2].verified is None
    assert not result.success and result.reason == "no progress: repeating step 2" and len(executed) == 3


def test_new_steps_are_refused_with_complete_note_when_nothing_is_left():
    from local_agent.goal_facts import Fact

    snap = _claude_states()["chat"]
    agent, _, _, _ = make(snap, [DONE])
    fact = Fact("new", "conversation")
    agent.checklist, agent.seen_facts, agent.known_facts = [fact], {fact}, {fact: True}
    plan = P(action="shortcut", keys="cmd+t")
    assert agent._guard(plan, None, None, snap) == (
        "block", "a new conversation is already open; the goal looks complete; answer done")  # fmt: skip
    assert agent._guard(P(action="shortcut", keys="cmd+s"), None, None, snap)[0] == ""
