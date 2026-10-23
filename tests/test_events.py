"""`run --events` / `doctor --json`: the JSON contract in docs/APP_INTEGRATION.md, driven through the real CLI with
the planner, selector, observer and executor faked."""

import io
import json

import pytest
from typer.testing import CliRunner

from local_agent import cli
from local_agent.events import EventWriter
from tests.test_loop import DONE, FakeSelector, Outcome, P

pytest.importorskip("ApplicationServices")  # run imports the PyObjC observer/executor modules before we patch them


class Planner:
    """Scripted steps; a step that is an exception class is raised instead (cancel / crash)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.last_prompt_tokens = 0
        self.last_messages = []

    def next_step(self, goal, scope, history, screen=None):
        step = self.steps.pop(0)
        if isinstance(step, type) and issubclass(step, BaseException):
            raise step()
        self.last_prompt_tokens = 1234
        self.last_messages = [{"role": "user", "content": goal}, {"role": "assistant", "content": "{}"}]
        return step


@pytest.fixture
def run_cli(textedit, monkeypatch):
    from local_agent.act import executor
    from local_agent.observe import ax_tree
    from local_agent.planner import qwen
    from local_agent.selector import jev

    monkeypatch.setenv("LOCAL_AGENT_SETTLE_S", "0")
    monkeypatch.setenv("LOCAL_AGENT_OUTLINE", "0")  # no real outline call in the CLI tests
    monkeypatch.setattr(cli, "_prepare", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_start_app", lambda *a: None)
    monkeypatch.setattr(ax_tree, "snapshot", lambda s: textedit)
    monkeypatch.setattr(executor, "execute", lambda step, el: Outcome(True, "did it"))
    monkeypatch.setattr(executor, "run_terminal", lambda cmd: Outcome(True, "exit=0 output: 42"))
    monkeypatch.setattr(jev, "make_selector", lambda s: FakeSelector())

    def run(steps, *args, stdin=""):
        monkeypatch.setattr(qwen, "make_planner", lambda s, selector=None: Planner(steps))
        r = CliRunner().invoke(cli.app, ["run", "--events", "--app", "TextEdit", *args, "--", "do it"], input=stdin)
        events = [json.loads(line) for line in r.stdout.splitlines()]
        run.stages = [e for e in events if e["type"] == "stage"]  # the other tests don't list them
        return r, [e for e in events if e["type"] != "stage"]

    return run


def test_event_sequence_and_shapes(run_cli):
    r, ev = run_cli([P(action="click", target={"role": "AXMenuBarItem", "description": "File"}), DONE])
    assert r.exit_code == 0, r.output
    assert [e["type"] for e in ev] == ["start", "plan", "select", "record", "plan", "result"]
    start, plan, select, record, done, result = ev
    assert start["version"] == 1 and start["app"] == "TextEdit" and start["goal"] == "do it"
    assert start["view"] == "ids" and start["planner"] == "json" and start["dry_run"] is False
    assert plan["step"] == 1 and plan["plan"]["action"] == "click" and plan["plan"]["summary"].startswith("click")
    assert plan["prompt_tokens"] == 1234 and plan["messages"][-1]["role"] == "assistant"
    assert isinstance(plan["screen"], str) and isinstance(plan["planner_s"], float)
    assert select["step"] == 1 and select["source"] == "jev" and select["element"]["role"]
    assert set(select["element"]) == {"id", "role", "name", "description", "frame"}
    assert select["candidates"] > 0 and isinstance(select["select_s"], float)
    assert record["plan"] == plan["plan"]["summary"] and record["executed"] and record["verified"]
    assert isinstance(record["verify_s"], float)
    assert "goal_s" in record and "goal_probability" in record  # null when the selector can't judge the goal
    assert done["step"] == 2 and done["plan"]["done"] is True and done["plan"]["reasoning"] == "finished"
    assert result["success"] and result["steps"] == 1 and result["history"][0]["plan"] == record["plan"]
    assert "type" not in result["history"][0] and all(e["t"] >= 0 for e in ev)


def test_stdout_is_only_json(run_cli):
    r, _ = run_cli([P(action="shortcut", keys="cmd+n"), DONE])
    assert "step 1" in r.stderr and "plan" in r.stderr  # human text still goes to stderr


@pytest.mark.parametrize("answer,ok", [("y\n", True), ("n\n", False), ("", False)])
def test_terminal_confirmation_over_stdin(run_cli, answer, ok):
    r, ev = run_cli([P(action="terminal", command="rm -r ~/old-builds"), DONE], stdin=answer)
    confirm = next(e for e in ev if e["type"] == "confirm_terminal")
    assert confirm == {**confirm, "step": 1, "command": "rm -r ~/old-builds", "reason": "deletes folders recursively",
                       "safety_probability": None}  # fmt: skip
    record = next(e for e in ev if e["type"] == "record")
    assert record["executed"] is ok
    assert record["note"] == ("approved by the user (deletes folders recursively); exit=0 output: 42" if ok
                              else "user declined the terminal command (deletes folders recursively)")  # fmt: skip
    assert [e["type"] for e in ev].index("confirm_terminal") < [e["type"] for e in ev].index("record")


def test_safe_command_runs_without_confirmation(run_cli):
    _, ev = run_cli([P(action="terminal", command="ls ~/Downloads | wc -l"), DONE])
    assert "confirm_terminal" not in [e["type"] for e in ev]
    record = next(e for e in ev if e["type"] == "record")
    assert record["executed"] and record["safety_probability"] == 0.95


@pytest.mark.parametrize("flag", ["--turbo", "--yes"])
def test_turbo_skips_confirmation(run_cli, flag):
    _, ev = run_cli([P(action="terminal", command="rm -r ~/old-builds"), DONE], flag)
    assert "confirm_terminal" not in [e["type"] for e in ev]
    assert ev[0]["turbo"] is True


def test_interrupt_reports_cancelled_with_history(run_cli):
    r, ev = run_cli([P(action="shortcut", keys="cmd+n"), KeyboardInterrupt])
    assert r.exit_code == 130
    result = ev[-1]
    assert result["type"] == "result" and result["reason"] == "cancelled" and not result["success"]
    assert result["steps"] == 1 and result["history"][0]["plan"] == "shortcut keys=cmd+n"


def test_crash_emits_error_event(run_cli):
    r, ev = run_cli([RuntimeError])
    assert r.exit_code == 2
    assert ev[-1]["type"] == "error" and ev[-1]["kind"] == "RuntimeError"
    assert "result" not in [e["type"] for e in ev]


def test_planner_error_record_without_plan(run_cli):
    from local_agent.planner.qwen import PlannerError

    r, ev = run_cli([PlannerError] * 3)
    assert r.exit_code == 1
    assert [e["type"] for e in ev] == ["start", "record", "record", "record", "result"]
    assert ev[1]["plan"] == "planner error" and ev[1]["verify_s"] is None


def test_dry_run(run_cli):
    r, ev = run_cli([P(action="shortcut", keys="cmd+n")], "--dry-run")
    assert ev[0]["dry_run"] is True
    assert ev[-2]["note"].startswith("dry-run") and ev[-1]["reason"] == "dry run: stopped after one step"


def test_event_writer_flushes_one_line_per_event():
    buf = io.StringIO()
    clock = iter([10.0, 10.25]).__next__
    EventWriter(buf, clock).emit("start", version=1, goal="ü")
    assert buf.getvalue() == '{"type": "start", "t": 0.25, "version": 1, "goal": "ü"}\n'


def test_doctor_json(monkeypatch):
    import httpx

    from local_agent.observe import ax_tree
    from local_agent.selector import jev

    class Tags:
        def json(self):
            return {"models": [{"name": "qwen3.5:9b", "size": 6_600_000_000}, {"name": "qwen2.5-coder:3b", "size": 1_900_000_000}]}

    class Sel:
        def health(self):
            return False

    monkeypatch.setattr(ax_tree, "ensure_trusted", lambda prompt=False: True)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: Tags())
    monkeypatch.setattr(jev, "make_selector", lambda s: Sel())
    r = CliRunner().invoke(cli.app, ["doctor", "--json"])
    report = json.loads(r.stdout)
    assert r.exit_code == 1 and report["ok"] is False
    assert report["accessibility"] == {"ok": True}
    assert report["ollama"]["ok"] and report["ollama"]["has_model"]
    assert report["ollama"]["models"] == [{"name": "qwen3.5:9b", "size_gb": 6.6}, {"name": "qwen2.5-coder:3b", "size_gb": 1.9}]
    assert report["jev"] == {**report["jev"], "ok": False, "backend": "jevk5", "url": "http://127.0.0.1:8091"}
    assert report["jev"]["command"].startswith("llama-server ") and "--port 8091" in report["jev"]["command"]


def test_doctor_json_ollama_down(monkeypatch):
    import httpx

    from local_agent.observe import ax_tree
    from local_agent.selector import jev

    def refuse(url, timeout=None):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(ax_tree, "ensure_trusted", lambda prompt=False: False)
    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(jev, "make_selector", lambda s: type("S", (), {"health": lambda self: True})())
    report = json.loads(CliRunner().invoke(cli.app, ["doctor", "--json"]).stdout)
    assert report["ollama"] == {**report["ollama"], "ok": False, "models": [], "error": "Connection refused"}
    assert report["accessibility"]["ok"] is False and report["jev"]["ok"] is True


def test_overwrite_declined_is_an_ordinary_failed_result():
    from local_agent import events

    r = events.result_event(False, "overwrite_declined", [], 1.0)
    assert r["success"] is False and r["reason"] == "overwrite_declined"


def test_stage_events_name_each_phase_in_order(run_cli):
    r, _ = run_cli([P(action="click", target={"role": "AXMenuBarItem", "description": "File"}), DONE])
    stages = run_cli.stages
    assert [e["stage"] for e in stages] == ["observing", "planning", "selecting", "acting", "observing", "verifying",
                                            "planning"]
    assert all(e["step"] >= 1 and e["type"] == "stage" and "t" in e for e in stages)
    assert stages[0]["elapsed_s"] is None and all(isinstance(e["elapsed_s"], float) for e in stages[1:])


def test_two_unsure_steps_end_the_run_with_an_unsure_reason(run_cli):
    from local_agent.planner.jev_only import Unsure

    class UnsureStep(Unsure):
        def __init__(self):
            super().__init__('unsure (p=0.12): no option clearly fits; click "A"; click "B"')

    r, ev = run_cli([UnsureStep, UnsureStep, DONE])
    result = ev[-1]
    assert r.exit_code == 1 and result["type"] == "result" and not result["success"]
    assert result["reason"] == "unsure: do it" and result["steps"] == 2
    assert result["history"][0]["note"].startswith("unsure (p=0.12)") and result["history"][0]["executed"] is False
