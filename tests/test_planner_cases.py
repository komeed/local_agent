"""The eval's checks must accept a correct plan (by description or by screen-view number) and reject live mistakes."""

import pytest

from evals.planner_cases import CASES
from local_agent.planner.schema import PlannerOutput


def P(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw)


def click(role, desc, action="click"):
    return P(action=action, target={"role": role, "description": desc})


def by_id(case, predicate, action="click"):
    el = next(e for e in case.snap().elements if predicate(e))
    return P(action=action, target={"id": el.id})


IDEAL = {
    "select_all": P(action="shortcut", keys="cmd+a"),
    "select_all_first": P(action="shortcut", keys="Cmd + A"),
    "new_document": P(action="shortcut", keys="cmd+n"),
    "close_menu": P(action="shortcut", keys="escape"),
    "open_format_menu": click("AXMenuBarItem", "Format menu"),
    "submenu_parent": click("AXMenuItem", "Font submenu"),
    "submenu_item": click("AXMenuItem", "Bold"),
    "make_rich_text": click("AXMenuItem", "Make Rich Text"),
    "recover_repeated_click": P(action="shortcut", keys="cmd+a"),
    "recover_hidden_item": click("AXMenuBarItem", "Format"),
    "recover_failed_shortcut": click("AXMenuBarItem", "Edit"),
    "list_downloads": P(action="terminal", command="ls ~/Downloads"),
    "make_folder": P(action="terminal", command="mkdir -p ~/Desktop/reports"),
    "open_app": P(action="terminal", command="open -a Calculator"),
    "done_after_typing": PlannerOutput(done=True, reasoning="r"),
    "done_after_terminal": PlannerOutput(done=True, reasoning="r"),
    "not_done_yet": P(action="type", text="hi there"),
    "toolbar_strikethrough": click("AXCheckBox", "strikethrough button"),
    "font_size": P(action="type", text="18", target={"role": "AXComboBox", "description": "font size box"}),
    "save_where": click("AXPopUpButton", "Where popup"),
    "save_name": P(action="type", text="notes"),
    "save_confirm": click("AXButton", "Save"),
    "error_dismiss": click("AXButton", "OK"),
    "find_field": P(action="type", text="cat", target={"role": "AXTextField", "description": "Find field"}),
    "finder_open_visible": click("AXRow", "Q3 report.pdf", "double_click"),
    "finder_open_hidden": click("AXRow", "invoice-0425.pdf", "double_click"),
    "finder_back": click("AXButton", "Back"),
    "read_free_space": PlannerOutput(done=True, reasoning="31.4 GB available"),
}

BY_NUMBER = {
    "toolbar_strikethrough": lambda e: e.label == "strikethrough",
    "save_where": lambda e: e.role == "AXPopUpButton" and e.title == "Where:",
    "error_dismiss": lambda e: e.title == "OK",
    "submenu_item": lambda e: e.role == "AXMenuItem" and e.title == "Bold",
    "open_format_menu": lambda e: e.role == "AXMenuBarItem" and e.title == "Format",
}

WRONG = {
    "select_all": click("AXTextArea", "Hello world"),
    "submenu_parent": click("AXMenuItem", "Bold"),
    "recover_repeated_click": click("AXTextArea", "Hello world"),
    "list_downloads": P(action="shortcut", keys="cmd+space"),
    "done_after_typing": P(action="type", text="hello world"),
    "error_dismiss": click("AXButton", "Save"),
    "save_where": P(action="type", text="Documents"),
}


def case(name):
    return next(c for c in CASES if c.name == name)


def test_every_case_has_an_ideal_answer():
    assert set(IDEAL) == {c.name for c in CASES}


@pytest.mark.parametrize("c", CASES, ids=lambda c: c.name)
def test_ideal_passes(c):
    assert c.check(IDEAL[c.name], c.snap()) == ""


@pytest.mark.parametrize("name", sorted(BY_NUMBER))
def test_number_picks_pass(name):
    c = case(name)
    assert c.check(by_id(c, BY_NUMBER[name]), c.snap()) == ""


def test_wrong_number_fails():
    c = case("toolbar_strikethrough")
    assert c.check(by_id(c, lambda e: e.label == "bold"), c.snap()) != ""


@pytest.mark.parametrize("name", sorted(WRONG))
def test_live_mistakes_fail(name):
    assert case(name).check(WRONG[name], case(name).snap()) != ""
