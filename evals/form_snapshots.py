"""Synthetic form screens for the form-checklist tests: a web sign-up form (the same shape web_outline.js returns for
evals/fixtures/web/web_signup_form.html) and a native sheet form."""

from __future__ import annotations

from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.observe.web import outline_to_elements


def _item(i, role, name, y, *, value="", required=False, hint="", submit=False, checked=None, tag="input", path="AXForm:signup"):
    return {"web_id": f"la{i}", "role": role, "name": name, "value": value, "placeholder": "", "tag": tag, "enabled": True,
            "focused": False, "checked": checked, "rect": [20, y, 240, 22], "path": path, "context": "", "interactive": True,
            "href": "", "required": required, "hint": hint, "submit": submit}  # fmt: skip


def web_signup(values: dict | None = None, extra: list | None = None, submitted: bool = False) -> Snapshot:
    """Name*, Email*, Phone, Country* (select), Newsletter, Terms* and a Create account button."""
    v = values or {}
    if submitted:
        o = {"url": "https://shop.example/welcome", "title": "Welcome", "inner_width": 1000, "inner_height": 700, "more": 0,
             "elements": [{"web_id": "", "role": "heading", "name": "", "value": "Thank you", "interactive": False,
                           "rect": [20, 20, 200, 30], "path": "", "context": ""},
                          {"web_id": "", "role": "text", "name": "", "value": "Your account has been created.",
                           "interactive": False, "rect": [20, 60, 300, 20], "path": "", "context": ""}]}
    else:
        items = [
            _item(1, "textbox", "Full name *", 100, value=v.get("Full name", ""), required=True, hint="name name text"),
            _item(2, "textbox", "Email *", 150, value=v.get("Email", ""), required=True, hint="email email email"),
            _item(3, "textbox", "Phone", 200, value=v.get("Phone", ""), hint="tel phone tel"),
            _item(4, "combobox", "Country *", 250, value=v.get("Country", "Choose a country…"), required=True, tag="select",
                  hint="country"),
            _item(5, "checkbox", "Send me the newsletter", 300, checked=bool(v.get("Newsletter")), hint="news checkbox"),
            _item(6, "checkbox", "I accept the terms *", 340, checked=bool(v.get("Terms")), required=True, hint="terms checkbox"),
            *(extra or []),
            _item(9, "button", "Create account", 400, submit=True, tag="button", hint="submit"),
        ]
        o = {"url": "https://shop.example/signup", "title": "Create your account", "inner_width": 1000, "inner_height": 700,
             "more": 0, "elements": items}
    area = Frame(0, 90, 1000, 700)
    chrome = [UIElement(id=0, role="AXTextField", title="Address", value=o["url"], path="AXWindow:Arc > AXToolbar",
                        frame=Frame(300, 20, 600, 24))]
    els = chrome + outline_to_elements(o, area, 1)
    return Snapshot("Arc", "company.thebrowser.Browser", 1, o["title"], els, url=o["url"], page_title=o["title"])


def extra_field(role, name, hint="", required=True, y=370, i=7, value=""):
    return _item(i, role, name, y, required=required, hint=hint, value=value)


def native_sheet_form(values: dict | None = None) -> Snapshot:
    """A native sheet: First name, Last name, Company (optional), Role pop-up, and Cancel / Register (default)."""
    v = values or {}
    sheet = "AXWindow:Events > AXSheet"
    els = [
        UIElement(id=0, role="AXTextField", title="First name", value=v.get("First name", ""), path=sheet, frame=Frame(100, 100, 200, 22), required=True),
        UIElement(id=1, role="AXTextField", title="Last name", value=v.get("Last name", ""), path=sheet, frame=Frame(100, 140, 200, 22), required=True),
        UIElement(id=2, role="AXTextField", title="Company", value=v.get("Company", ""), path=sheet, frame=Frame(100, 180, 200, 22)),
        UIElement(id=3, role="AXPopUpButton", title="Role", value=v.get("Role", "Select…"), path=sheet, frame=Frame(100, 220, 200, 22), required=True),
        UIElement(id=4, role="AXButton", title="Cancel", path=sheet, frame=Frame(100, 300, 80, 24)),
        UIElement(id=5, role="AXButton", title="Register", is_default=True, path=sheet, frame=Frame(200, 300, 80, 24)),
    ]  # fmt: skip
    return Snapshot("Events", "x.events", 1, "Events", els)
