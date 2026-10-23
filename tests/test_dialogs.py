import pytest

from local_agent.dialogs import name_sheet, overwrite_alert
from local_agent.observe.models import Snapshot, UIElement
from tests.test_loop import DONE, FakeSelector, Outcome, P, make

SHEET = "AXWindow:Untitled > AXSheet"
GOAL = 'open a new document in textedit and type "hello world" and save it as "helloworld.txt"'


def snap(*els, title="Untitled", app="TextEdit"):
    return Snapshot(app=app, bundle_id="b", pid=1, window_title=title, elements=list(els))


def name_field(value="Untitled", **kw):
    return UIElement(id=1, role="AXTextField", title="Save As:", value=value, focused=True, path=SHEET, **kw)


def save_sheet(value="Untitled"):
    return snap(UIElement(id=0, role="AXTextArea", value="Hello world"), name_field(value),
                UIElement(id=2, role="AXButton", title="Cancel", path=SHEET),
                UIElement(id=3, role="AXButton", title="Save", is_default=True, path=SHEET))  # fmt: skip


def alert():
    return snap(UIElement(id=0, role="AXStaticText", value='"x.txt" already exists. Do you want to replace it?',
                          interactive=False, path=SHEET),
                UIElement(id=1, role="AXButton", title="Cancel", path=SHEET),
                UIElement(id=2, role="AXButton", title="Replace", path=SHEET))  # fmt: skip


def test_detects_overwrite_alert_and_name_sheet():
    assert overwrite_alert(alert()) and not overwrite_alert(save_sheet())
    sheet = name_sheet(save_sheet())
    assert sheet.label == "Save As" and sheet.button == "Save" and sheet.field.value == "Untitled"
    # No AXDefaultButton: a button named Save/OK stands in.
    plain = save_sheet()
    plain.elements[-1].is_default = False
    assert name_sheet(plain).button == "Save"
    assert name_sheet(snap(UIElement(id=0, role="AXTextField", value="x"))) is None  # not in a dialog


def test_overwrite_click_and_return_ask_even_in_turbo():
    for step in (P(action="click", target={"id": 2}), P(action="shortcut", keys="return")):
        asked = []
        agent, _, executed, _ = make(alert(), [step, DONE], confirm=False, asked=asked, turbo=True)
        result = agent.run("g")
        assert asked and asked[0][1] == "would overwrite an existing file" and executed == []
        assert result.history[0].note == "user declined: would overwrite an existing file"
    asked = []
    agent, _, executed, _ = make(alert(), [P(action="click", target={"id": 2}), DONE], confirm=True, asked=asked)
    agent.run("g")
    assert len(asked) == 1 and len(executed) == 1  # approved: it runs


def test_cancel_in_the_alert_does_not_ask():
    asked = []
    agent, _, executed, _ = make(alert(), [P(action="click", target={"id": 1}), DONE], asked=asked)
    agent.run("g")
    assert asked == [] and len(executed) == 1


@pytest.mark.parametrize("step", [P(action="shortcut", keys="cmd+s"), P(action="shortcut", keys="return")])
def test_save_is_blocked_until_the_name_field_is_filled(step):
    agent, _, executed, _ = make(save_sheet(), [step, DONE], max_steps=1)
    agent.apps = ["TextEdit"]
    rec = agent.run(GOAL).history[-1]
    assert executed == [] and rec.note == 'the name field says "Untitled"; type "helloworld.txt" into it first'
    agent, _, executed, _ = make(save_sheet("helloworld.txt"), [step, DONE], max_steps=1)
    agent.apps = ["TextEdit"]
    agent.run(GOAL)
    assert len(executed) == 1  # the field is right: return/cmd+s may run


def test_progress_line_spells_out_field_and_default_button():
    agent, planner, _, _ = make(save_sheet(), [DONE], max_steps=1)
    agent.apps = ["TextEdit"]
    agent.run(GOAL)
    nxt = planner.scopes[0]["progress"]["next"]
    assert nxt == 'type "helloworld.txt" into the "Save As" field, then click "Save"'
