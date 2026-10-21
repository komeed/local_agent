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
        self.steps, self.seen = list(steps), []

    def next_step(self, goal, scope, history):
        self.seen.append(list(history))
        return self.steps.pop(0)


class FakeSelector:
    def __init__(self, prob=0.9, verify=0.9):
        self.prob, self.verify_p = prob, verify

    def select(self, goal, snap, step):
        return Selection(snap.elements[5], self.prob, self.prob, len(snap.elements), 1)

    def verify(self, goal, step, element, before, after):
        return self.verify_p


def P(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw)


DONE = PlannerOutput(done=True, reasoning="finished")


def make(textedit, steps, selector=None, confirm=True, **settings):
    executed, ran = [], []
    hooks = Hooks(
        observe=lambda: textedit,
        execute=lambda step, el: executed.append((step.action, el)) or Outcome(True, "did it"),
        run_terminal=lambda cmd: ran.append(cmd) or Outcome(True, "exit=0"),
        confirm_terminal=lambda cmd: confirm,
        sleep=lambda s: None,
    )
    planner = ScriptedPlanner(steps)
    agent = Agent(Settings(**settings), planner, selector or FakeSelector(), hooks)
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


def test_terminal_runs_after_confirmation(textedit):
    agent, _, executed, ran = make(textedit, [P(action="terminal", command="open -a TextEdit"), DONE])
    assert agent.run("g").success
    assert ran == ["open -a TextEdit"] and executed == []


def test_terminal_declined(textedit):
    agent, _, _, ran = make(textedit, [P(action="terminal", command="rm -rf /tmp/x")] * 3, confirm=False)
    result = agent.run("g")
    assert ran == [] and "declined" in result.history[0].note


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
