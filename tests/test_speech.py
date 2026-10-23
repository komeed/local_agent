"""Spoken lines (local_agent/speech.py): one test per phrase type, the shortening, privacy and the --speak runner."""

import pytest

from local_agent import speech
from local_agent.history import StepRecord
from local_agent.planner.schema import PlannerOutput


def rec(plan, element=None, note="ok", executed=True, verified=True):
    return StepRecord(1, plan, element=element, executed=executed, verified=verified, note=note)


def summary(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw).summary()


def line(r):
    return speech.record_say(r)


@pytest.mark.parametrize("name,short", [
    ("Model: Opus 5.5 High", "Model"), ("The Simpsons - StreamSite", "The Simpsons"), ("Inbox • 3 new", "Inbox"),
    ("Save (cmd S)", "Save"), ("one two three four five six", "one two three four"), ("", ""), ("New Chat…", "New Chat")])
def test_short_names(name, short):
    assert speech.short_name(name) == short


def test_spoken_keys():
    assert speech.spoken_keys("cmd+s") == "Command S" and speech.spoken_keys("cmd+shift+4") == "Command Shift 4"
    assert speech.spoken_keys("return") == "Return" and speech.spoken_keys("ctrl+left") == "Control Left arrow"


def test_step_lines():
    click = summary(action="click", target={"id": 3, "role": "AXButton", "description": "Model"})
    assert line(rec(click, 'AXButton title="Model: Opus 5.5 High" at (1,2)')) == ("Clicked Model", "step")
    assert line(rec(click, 'AXMenuItem title="Sonnet 5.5" in Menu "x"')) == ("Selected Sonnet 5.5", "step")
    assert line(rec(summary(action="click", target={"role": "AXButton", "description": "Save"}))) == ("Clicked Save", "step")
    assert line(rec(summary(action="click", target={"id": 3, "role": "AXButton", "description": ""}), "AXButton at (1,2)"))[0] == "Clicked a control"
    assert line(rec(summary(action="type", text="hello"), 'AXTextArea focused')) == ("Typed hello", "step")
    assert line(rec(summary(action="type", text="x" * 30), 'AXTextArea'))[0] == "Typed your text"
    assert line(rec(summary(action="shortcut", keys="cmd+s"))) == ("Pressed Command S", "step")
    assert line(rec(summary(action="scroll", direction="down"))) == ("Scrolled down", "step")
    assert line(rec(summary(action="scroll", direction="up")))[0] == "Scrolled up"
    assert line(rec(summary(action="menu", command="File > Save As…")))[0] == "Chose Save As…"
    assert line(rec(summary(action="terminal", command="ls ~")))[0] == "Ran a command"


def test_steps_the_agent_took_itself():
    note = "opened by the agent (goal names Claude); exit=0"
    assert line(rec(summary(action="terminal", command='open -a "Claude"'), note=note)) == ("Opened Claude", "step")
    tab = rec(summary(action="terminal", command='(switch to the "The Simpsons - X" tab)'),
              note='opened by the agent (goal names the tab "simpsons"); switched to the "The Simpsons - X" tab')
    assert line(tab) == ("Switched to your simpsons tab", "step")
    site = rec(summary(action="terminal", command="(open youtube.com in a new Arc tab)"),
               note="opened by the agent (goal names site www.youtube.com); open youtube.com in a new Arc tab")
    assert line(site) == ("Opened youtube", "step")
    search = rec(summary(action="terminal", command='(search for "dream" in a new Arc tab)'),
                 note="opened by the agent (goal names site dream); search")
    assert line(search) == ("Searched for dream", "step")


def test_private_text_is_never_spoken():
    plan = summary(action="type", text="hunter2")
    assert line(rec(plan, 'AXSecureTextField title="Password"'))[0] == "Typed your text"
    assert line(rec(plan, 'AXTextField label="Card number"'))[0] == "Typed your text"
    assert "hunter2" not in line(rec(plan, 'AXSecureTextField'))[0]


def test_problem_lines():
    click = summary(action="click", target={"id": 3, "role": "AXButton", "description": "Export"})
    assert line(rec(click, executed=False, verified=None, note='selector error: no element named "Export Data"')) == (
        "I couldn't find Export Data", "problem")
    assert line(rec(click, verified=False, note="did it"))[0] == "That step didn't work"
    assert line(rec(summary(action="shortcut", keys="cmd+w"), executed=False, verified=None,
                    note='"cmd+w" would close/quit/switch apps, which the goal doesn\'t ask for; work on: x')) == (
        "Blocked: that would close the app", "problem")
    assert line(rec("planner error", executed=False, verified=None, note="unsure (p=0.12): no option clearly fits; a")) == (
        "I'm not sure what to click", "problem")


def test_skipped_and_dry_run_records_have_no_line():
    assert line(rec(summary(action="shortcut", keys="cmd+n"), executed=False, verified=None, note="dry-run: action not executed")) is None
    assert line(rec("done", executed=False, verified=None, note="not done: x")) is None
    assert line(rec("leave", note="leave confirmed by user")) is None
    assert line(rec(summary(action="click", target={"id": 1, "role": "AXButton", "description": "x"}), executed=False,
                    verified=None, note="")) is None


def test_questions():
    q = speech.question_say
    assert q('replace existing "helloworld.txt"', "x") == ("Should I replace helloworld.txt?", "question")
    assert q('submit the form "Contact"', "sends your data") == ("Should I submit the form?", "question")
    assert q("leave the page and lose unsaved changes", "x") == ("Should I leave this page and lose changes?", "question")
    assert q("rm -r ~/old", "deletes folders") == ("Should I run this command?", "question")


def test_results_and_errors():
    r = speech.result_say
    assert r(True, "planner reported the goal is done", 3) == ("Done", "done")
    assert r(False, "overwrite_declined", 2) == ("Stopped: you declined", "problem")
    assert r(False, "declined: submit the form", 2)[0] == "Stopped: you declined"
    assert r(False, "needs_input: Email, Phone", 0) == ("I need Email, Phone to fill the form", "problem")
    assert len(r(False, "needs_input: " + ", ".join(f"Field number {i}" for i in range(9)), 0)[0]) <= 60
    assert r(False, "stuck: click Export", 5) == ("I got stuck on click Export", "problem")
    assert r(False, "unsure: do it", 2)[0] == "I got stuck on do it"
    assert len(r(False, "stuck: " + "word " * 40, 5)[0]) <= 60
    assert r(False, "reached max steps (20)", 20) == ("Stopped after 20 steps", "problem")
    assert r(False, "cancelled", 1) is None
    assert speech.error_say() == ("Something went wrong", "problem")


def test_every_line_is_short_plain_and_has_a_known_kind():
    long = rec(summary(action="click", target={"id": 3, "role": "AXButton", "description": "x"}),
               'AXButton title="' + "word " * 30 + '"')
    text, kind = line(long)
    assert len(text) <= 60 and kind in speech.KINDS and "[" not in text and "(" not in text


class FakeProc:
    def __init__(self):
        self.running, self.killed = True, False

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running, self.killed = False, True


def test_a_new_step_line_stops_the_last_step_line_but_not_questions():
    procs = []

    def popen(cmd):
        procs.append((cmd, FakeProc()))
        return procs[-1][1]

    sp = speech.Speaker(popen=popen)
    sp.speak(("Clicked A", "step"))
    sp.speak(("Should I run this command?", "question"))
    sp.speak(("Clicked B", "step"))
    assert procs[0][1].killed and not procs[1][1].killed
    assert procs[0][0] == ["say", "-r", "200", "Clicked A"]
    sp.speak(None)
    assert len(procs) == 3
    sp.popen = lambda cmd: (_ for _ in ()).throw(OSError())
    sp.speak(("Done", "done"))  # no `say` binary: silent
