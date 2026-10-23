import pytest

from evals.form_snapshots import extra_field, native_sheet_form, web_signup
from local_agent.config import Settings
from local_agent.forms import (find_form, labelled_quotes, match_values, missing_required, parse_data, sensitive_reason,
                               wants_form)
from local_agent.goal_facts import progress_scope
from local_agent.loop import Agent, Hooks
from local_agent.planner.jev_only import build_options
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import Selection
from tests.test_loop import DONE, FakeSelector, Outcome, ScriptedPlanner

GOAL = 'fill out the form with name "Ada Lovelace", email "ada@example.org" and country "Norway"'


def test_goal_wording_that_asks_for_a_form():
    for g in ["fill out the form", "complete the application", "fill in this sign-up form", "sign up for the newsletter"]:
        assert wants_form(g), g
    for g in ["open the form", "fill the glass", "write a letter"]:
        assert not wants_form(g), g


def test_fields_in_reading_order_with_required_and_empty():
    form = find_form(web_signup())
    assert form.title == "signup" and form.submit.name() == "Create account"
    assert [(f.label, f.kind, f.required, f.empty) for f in form.fields] == [
        ("Full name", "text", True, True), ("Email", "text", True, True), ("Phone", "text", False, True),
        ("Country", "select", True, True), ("Send me the newsletter", "checkbox", False, True),
        ("I accept the terms", "checkbox", True, True)]  # fmt: skip
    sheet = find_form(native_sheet_form({"First name": "Ada"}))
    assert [f.label for f in sheet.fields] == ["First name", "Last name", "Company", "Role"]
    assert not sheet.fields[0].empty and sheet.fields[3].empty  # "Select…" is a placeholder option
    assert sheet.submit.name() == "Register"  # the sheet's default button; Cancel is not the submit


def test_a_filled_select_and_checked_boxes_are_not_empty():
    by = {f.label: f for f in find_form(web_signup({"Country": "Norway", "Terms": True})).fields}
    assert not by["Country"].empty and not by["I accept the terms"].empty and by["Send me the newsletter"].empty


def test_one_field_is_not_a_form():
    snap = web_signup()
    snap.elements = [e for e in snap.elements if e.title in ("Full name *", "Create account", "Address")]
    assert find_form(snap) is None


def test_values_come_from_labelled_quotes_and_data():
    assert labelled_quotes(GOAL) == [("name", "Ada Lovelace"), ("email", "ada@example.org"), ("country", "Norway")]
    fields = find_form(web_signup()).fields
    assert match_values(fields, GOAL, None) == {"Full name": "Ada Lovelace", "Email": "ada@example.org", "Country": "Norway"}
    data = parse_data('{"Phone": "555-0100", "terms": "yes"}')
    assert match_values(fields, "fill out the form", data) == {"Phone": "555-0100", "I accept the terms": "yes"}
    assert parse_data("# c\nemail: a@b.c\nfull name: Ada\n") == {"email": "a@b.c", "full name": "Ada"}


def test_unmatched_keys_ask_jev_and_never_invent_values():
    fields = find_form(native_sheet_form()).fields
    asked = []

    def pick(described, labels):
        asked.append((described, labels))
        return "Last name" if "surname" in described else None

    got = match_values(fields, "fill out the form", {"surname": "Lovelace", "favourite colour": "green"}, pick=pick)
    assert got == {"Last name": "Lovelace"} and len(asked) == 2 and "Company" in asked[0][1]


def test_required_fields_without_a_value_are_listed():
    form = find_form(web_signup())
    values = match_values(form.fields, 'fill out the form with name "Ada"', None)
    assert missing_required(form, values) == ["Email", "Country", "I accept the terms"]
    assert missing_required(form, {"Full name": "a", "Email": "b", "Country": "c", "I accept the terms": "yes"}) == []


def test_card_numbers_ids_and_passwords_are_never_filled():
    snap = web_signup(extra=[extra_field("textbox", "Card number *", "cc-number")])
    card = next(e for e in snap.elements if e.title.startswith("Card number"))
    assert sensitive_reason(card) and sensitive_reason(next(e for e in snap.elements if e.title == "Email *")) == ""
    form = find_form(snap)
    assert missing_required(form, {"Full name": "a", "Email": "b", "Country": "c", "I accept the terms": "y"}) == [
        "Card number (the user must enter it)"]
    for name, hint in [("Social security number", "ssn"), ("Passport", ""), ("CVV", "cc-csc"), ("Number", "cc-number")]:
        extra = web_signup(extra=[extra_field("textbox", name, hint)])
        assert sensitive_reason(next(e for e in extra.elements if e.title == name))


# --- the checklist and the loop -------------------------------------------------------------------------------------


def checklist_for(goal, snap, data=None):
    hooks = Hooks(observe=lambda: snap, execute=lambda s, e: Outcome(True, ""), run_terminal=lambda c: Outcome(True, ""))
    agent = Agent(Settings(), ScriptedPlanner([]), FakeSelector(), hooks, form_data=data)
    agent.apps, agent.history, agent.checklist, agent.facts = [], [], [], []
    return agent, agent._plan_form(goal, snap)


def test_checklist_lines_in_screen_order():
    snap = web_signup()
    data = {"Full name": "Ada", "Email": "a@b.c", "Country": "Norway", "terms": "yes", "Phone": "555"}
    agent, stop = checklist_for("fill out the form", snap, data)
    assert stop is None
    p = progress_scope(agent.checklist, snap)
    assert p["next"] == 'type "Ada" into "Full name"'
    assert p["remaining"] == ['type "a@b.c" into "Email"', 'type "555" into "Phone"', 'choose "Norway" in "Country"',
                              'tick "I accept the terms"', 'submit the form: click "Create account"']  # fmt: skip
    assert "newsletter" not in str(p)  # optional, no value: not an item


def test_missing_required_values_end_the_run_as_needs_input():
    agent, stop = checklist_for('fill out the form with name "Ada"', web_signup())
    assert stop.success is False and stop.reason == "needs_input: Email, Country, I accept the terms"


class FormUI:
    """A tiny stateful page: typing/choosing/ticking fill the values, the button submits."""

    def __init__(self):
        self.values, self.submitted, self.typed = {}, False, []

    def observe(self):
        return web_signup(self.values, submitted=self.submitted)

    def execute(self, step, el):
        label = (el.title or "").rstrip(" *") if el else ""
        if step.action == "type":
            self.values[label] = step.text
            self.typed.append((label, step.text))
        elif step.action == "click" and el.role == "AXCheckBox":
            self.values["Terms" if "terms" in label else "Newsletter"] = True
        elif step.action == "click" and el.role == "AXButton":
            self.submitted = True
        return Outcome(True, "ok")


def P(**kw):
    return PlannerOutput(done=False, reasoning="r", **kw)


def tgt(i, role, d):
    return {"id": i, "role": role, "description": d}


class IdSelector(FakeSelector):
    def select(self, goal, snap, step):
        return Selection(next(e for e in snap.elements if e.id == step.target.id), 0.95, 0.95, 5, 1)


def button_id():
    return next(e.id for e in web_signup().elements if e.title == "Create account")


def steps():
    return [P(action="type", text="Ada Lovelace", target=tgt(1, "AXTextField", "Full name")),
            P(action="type", text="ada@example.org", target=tgt(2, "AXTextField", "Email")),
            P(action="type", text="Norway", target=tgt(4, "AXPopUpButton", "Country")),
            P(action="click", target=tgt(6, "AXCheckBox", "I accept the terms")),
            P(action="click", target=tgt(button_id(), "AXButton", "Create account"))]  # fmt: skip


def run_form(confirm=True, turbo=False):
    ui, asked = FormUI(), []
    hooks = Hooks(observe=ui.observe, execute=ui.execute, run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None,
                  confirm_terminal=lambda c, w: asked.append((c, w)) or confirm)  # fmt: skip
    agent = Agent(Settings(turbo=turbo), ScriptedPlanner(steps() + [DONE] * 3), IdSelector(), hooks, form_data={"terms": "yes"})
    agent.apps = []
    return agent.run(GOAL), ui, asked


def test_a_loop_fills_the_form_and_submits_after_asking_even_in_turbo():
    result, ui, asked = run_form(turbo=True)
    assert result.success and result.reason.startswith("goal facts hold"), result.reason
    assert ui.typed == [("Full name", "Ada Lovelace"), ("Email", "ada@example.org"), ("Country", "Norway")]
    assert asked == [('submit the form "signup"', "sends your data")]


def test_declining_the_submit_stops_the_run_without_clicking():
    result, ui, asked = run_form(confirm=False)
    assert not result.success and result.reason == "declined: submit the form" and not ui.submitted
    assert len(asked) == 1


def test_typing_a_card_number_is_refused_unrun():
    snap = web_signup(extra=[extra_field("textbox", "Gift card number", "cc-number", required=False)])
    card = next(e for e in snap.elements if e.title == "Gift card number")
    ran = []
    hooks = Hooks(observe=lambda: snap, execute=lambda s, e: ran.append(s) or Outcome(True, ""),
                  run_terminal=lambda c: Outcome(True, ""), sleep=lambda s: None)
    plan = P(action="type", text="4111", target=tgt(card.id, "AXTextField", "Gift card number"))
    agent = Agent(Settings(turbo=True), ScriptedPlanner([plan] * 3), IdSelector(), hooks)
    agent.apps = []
    result = agent.run("enter the number")
    assert ran == [] and all("not allowed" in r.note for r in result.history)


def test_jev_mode_offers_the_checklist_lines_as_actions():
    snap = web_signup()
    scope = snap.scope()
    first = {}
    for line in ['type "Ada" into "Full name"', 'choose "Norway" in "Country"', 'tick "I accept the terms"',
                 'submit the form: click "Create account"']:
        scope["progress"] = {"done": [], "next": line, "remaining": []}
        first[line] = build_options("fill out the form", scope, [], snap, None)[0]
    t = first['type "Ada" into "Full name"']
    assert t.label == 'type "Ada" into "Full name"' and t.plan.target.id == 1 and t.plan.text == "Ada"
    c = first['choose "Norway" in "Country"']
    assert c.plan.action == "type" and c.plan.text == "Norway" and c.plan.target.id == 4
    assert first['tick "I accept the terms"'].plan.action == "click"
    assert first['submit the form: click "Create account"'].label == 'click "Create account"'
