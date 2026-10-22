import json

import httpx
import pytest

from evals.planner_cases import fixture
from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.observe.screen_view import render
from local_agent.planner.choice import NOT_LISTED, ChoicePlanner, history_lines, screen_choices, screen_ids
from local_agent.planner.qwen import PlannerError, QwenPlanner, make_planner


def _planner(responses, status=200):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if status == 501 and "format" in body:
            return httpx.Response(501, json={"error": "structured output is unavailable"})
        return httpx.Response(200, json={"message": {"content": responses.pop(0)}, "prompt_eval_count": 900})

    client = httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return ChoicePlanner(Settings(planner_mode="choice"), client=client), calls


@pytest.fixture
def sheet():
    snap = fixture("te_save_sheet")
    return snap, render(snap, 3000)


def _el(snap, role, word):
    return next(e for e in snap.elements if e.role == role and word in e.name().lower())


def test_screen_ids_finds_numbers_and_text_inputs(sheet):
    snap, screen = sheet
    ids, text_ids = screen_ids(screen)
    field = next(e for e in snap.elements if e.role == "AXTextField" and e.focused)
    assert field.id in ids and field.id in text_ids
    assert _el(snap, "AXButton", "save").id in ids
    assert _el(snap, "AXButton", "save").id not in text_ids


def _label(screen, el_id):
    return next(k for k, v in screen_choices(screen).items() if v == el_id)


def test_choices_are_name_first_without_quotes(sheet):
    snap, screen = sheet
    choices = screen_choices(screen)
    assert choices["Save (button)"] == _el(snap, "AXButton", "save").id
    assert not any('"' in k for k in choices)
    assert all(not k.endswith("disabled)") for k in choices)


def test_click_by_label_shares_the_prompt_prefix(sheet):
    snap, screen = sheet
    save = _el(snap, "AXButton", "save").id
    planner, calls = _planner(['{"kind": "click"}', json.dumps({"element": "Save (button)"})])
    step = planner.next_step("save it", snap.scope(), [], screen)
    assert (step.action, step.target.id) == ("click", save)
    assert calls[0]["format"]["properties"]["kind"]["enum"][0] == "done"
    assert "Save (button)" in calls[1]["format"]["properties"]["element"]["enum"]
    assert calls[1]["messages"][: len(calls[0]["messages"])] == calls[0]["messages"]  # cached prefix reused
    assert planner.last_prompt_tokens == 900


def test_unlisted_element_is_described(sheet):
    snap, screen = sheet
    planner, _ = _planner(['{"kind": "open"}', json.dumps({"element": NOT_LISTED}),
                           '{"description": "invoice.pdf in the list"}'])  # fmt: skip
    step = planner.next_step("open invoice.pdf", snap.scope(), [], screen)
    assert step.action == "double_click" and step.target.id is None
    assert step.target.description == "invoice.pdf in the list"


def test_type_into_field_or_focused(sheet):
    snap, screen = sheet
    field = next(e for e in snap.elements if e.role == "AXTextField" and e.focused).id
    planner, _ = _planner(['{"kind": "type"}', json.dumps({"text": "notes", "field": field}),
                           '{"kind": "type"}', '{"text": "notes", "field": 0}'])  # fmt: skip
    assert planner.next_step("name it notes", snap.scope(), [], screen).target.id == field
    step = planner.next_step("name it notes", snap.scope(), [], screen)
    assert step.text == "notes" and step.target is None


def test_done_keeps_the_answer(textedit):
    planner, _ = _planner(['{"kind": "done"}', '{"summary": "There are 42 files."}'])
    step = planner.next_step("how many files?", textedit.scope(), [])
    assert step.done and "42" in step.reasoning


def test_shortcut_and_terminal(textedit):
    planner, _ = _planner(['{"kind": "shortcut"}', '{"keys": "Cmd+A"}', '{"kind": "terminal"}', '{"command": "ls ~"}'])
    assert planner.next_step("select all", textedit.scope(), []).keys == "cmd+a"
    assert planner.next_step("list home", textedit.scope(), []).command == "ls ~"


def test_unstructured_fallback_coerces_and_skips_schema_note(sheet):
    snap, screen = sheet
    save = _el(snap, "AXButton", "save").id
    planner, calls = _planner(['{"kind": "Click"}', 'Here: {"element": "Save (button)"}'], status=501)
    assert planner.next_step("save it", snap.scope(), [], screen).target.id == save
    sent = [c for c in calls if "format" not in c]
    assert not any("JSON schema" in m["content"] for c in sent for m in c["messages"])


def test_bad_kind_and_repair(textedit):
    planner, _ = _planner(['{"kind": "teleport"}'], status=501)
    with pytest.raises(PlannerError):
        planner.next_step("g", textedit.scope(), [])
    planner, calls = _planner(["nope", '{"kind": "scroll"}', '{"direction": "down"}'])
    assert planner.next_step("g", textedit.scope(), []).direction == "down"
    assert "Invalid" in calls[1]["messages"][-1]["content"]


def test_history_lines_show_element_and_result():
    h = [StepRecord(1, "click [12]", element='AXButton title="Save"', executed=True, verified=False, note="no change")]
    assert history_lines(h) == '1. click [12] on AXButton title="Save" -> FAILED (no change)'
    assert history_lines([]) == "none yet"


def test_make_planner():
    assert type(make_planner(Settings())) is QwenPlanner
    assert type(make_planner(Settings(planner_mode="choice"))) is ChoicePlanner
