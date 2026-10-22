import io
import json

from typer.testing import CliRunner

from local_agent import cli
from local_agent.events import EVENTS_VERSION, EventStream
from local_agent.planner.schema import PlannerOutput
from tests.test_loop import DONE, FakeSelector, Outcome, P


class FakePlanner:
    def __init__(self, steps):
        self.steps = list(steps)
        self.last_prompt_tokens = 1234
        self.last_messages = []

    def next_step(self, goal, scope, history, screen=None):
        self.last_messages = [{"role": "user", "content": goal}, {"role": "assistant", "content": "{}"}]
        return self.steps.pop(0)


def _run(monkeypatch, textedit, steps, args=(), stdin=""):
    import local_agent.act.executor as executor
    import local_agent.observe.ax_tree as ax_tree
    import local_agent.planner.qwen as qwen
    import local_agent.selector.jev as jev

    ran = []
    monkeypatch.setattr(ax_tree, "snapshot", lambda s, **kw: textedit)
    monkeypatch.setattr(executor, "execute", lambda step, el: Outcome(True, "did it"))
    monkeypatch.setattr(executor, "run_terminal", lambda cmd: ran.append(cmd) or Outcome(True, "exit=0 output: 3"))
    monkeypatch.setattr(qwen, "make_planner", lambda s: FakePlanner(steps))
    monkeypatch.setattr(jev, "make_selector", lambda s: FakeSelector())
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    res = CliRunner().invoke(cli.app, ["run", "make it bold", "--events", "--delay", "0", *args], input=stdin)
    events = [json.loads(line) for line in res.stdout.splitlines()]
    return res, events, ran


def test_events_stream_a_full_run(monkeypatch, textedit):
    bold = next(e for e in textedit.elements if e.role == "AXMenuBarItem")
    steps = [P(action="click", target={"id": bold.id}), P(action="click", target={"description": "File menu"}), DONE]
    res, events, _ = _run(monkeypatch, textedit, steps)
    assert res.exit_code == 0, res.output
    assert [e["type"] for e in events] == ["start", "plan", "select", "record", "plan", "select", "record", "plan",
                                           "result"]  # fmt: skip
    start, plan, by_number, rec = events[:4]
    assert start["version"] == EVENTS_VERSION and start["settings"]["planner_model"]
    assert plan["step"] == 1 and plan["plan"]["target"]["id"] == bold.id and plan["prompt_tokens"] == 1234
    assert plan["screen"].startswith("App: TextEdit") and plan["messages"][0]["content"] == "make it bold"
    assert by_number["source"] == "planner" and by_number["element"]["id"] == bold.id and by_number["select_s"] is None
    assert events[5]["source"] == "jev" and events[5]["select_s"] is not None
    assert rec["status"] == "ok" and rec["verify_probability"] == 0.9 and rec["verify_s"] is not None
    assert events[-1] == {**events[-1], "type": "result", "success": True, "steps": 2}
    assert all(e["t"] >= 0 for e in events)


def test_terminal_confirmed_over_stdin(monkeypatch, textedit):
    steps = [P(action="terminal", command="ls ~ | wc -l"), P(action="terminal", command="rm -rf /tmp/x"), DONE]
    res, events, ran = _run(monkeypatch, textedit, steps, stdin="y\nn\n")
    asks = [e for e in events if e["type"] == "confirm_terminal"]
    assert [a["command"] for a in asks] == ["ls ~ | wc -l", "rm -rf /tmp/x"] and asks[0]["step"] == 1
    assert ran == ["ls ~ | wc -l"]
    recs = [e for e in events if e["type"] == "record"]
    assert recs[0]["status"] == "ok" and recs[1]["note"] == "user declined the terminal command"


def test_yes_skips_confirmation_and_dry_run_status(monkeypatch, textedit):
    _, events, ran = _run(monkeypatch, textedit, [P(action="terminal", command="ls"), DONE], args=["--yes"])
    assert ran == ["ls"] and not any(e["type"] == "confirm_terminal" for e in events)
    _, events, _ = _run(monkeypatch, textedit, [P(action="shortcut", keys="cmd+a")], args=["--dry-run"])
    assert events[-2]["status"] == "dry-run" and events[-1]["success"] is True


def test_crash_becomes_an_error_event(monkeypatch, textedit):
    import local_agent.observe.ax_tree as ax_tree

    def boom(s, **kw):
        raise RuntimeError("Accessibility permission missing")

    _run(monkeypatch, textedit, [DONE])  # installs the fakes
    monkeypatch.setattr(ax_tree, "snapshot", boom)
    res = CliRunner().invoke(cli.app, ["run", "g", "--events", "--delay", "0"])
    last = json.loads(res.stdout.splitlines()[-1])
    assert res.exit_code == 2 and last["type"] == "error" and "Accessibility" in last["message"]


def test_stream_lines_are_json():
    buf = io.StringIO()
    clock = iter([0.0, 1.5])
    s = EventStream(buf, clock=lambda: next(clock))
    s.emit("x", plan=PlannerOutput(done=True, reasoning="r").model_dump())
    assert json.loads(buf.getvalue()) == {"type": "x", "t": 1.5, "plan": PlannerOutput(done=True, reasoning="r").model_dump()}
