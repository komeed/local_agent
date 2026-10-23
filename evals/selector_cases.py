"""Selector eval cases: screens where many controls share a name and only their surroundings tell them apart (a Delete
button per list row, Billing vs Shipping "City", a Close button per browser tab).

Each case is a synthetic snapshot (run through observe/context.py like a live one), one planner step and which
element(s) count as right. evals/selector_eval.py measures the pre-filter offline and, with --live, Jev itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from local_agent.observe.context import add_context
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from evals.web_snapshots import web_snapshot


def _el(snap: Snapshot, role: str, path: str, x: float, y: float, w: float = 80, h: float = 20, **kw) -> UIElement:
    e = UIElement(id=len(snap.elements), role=role, path=path, frame=Frame(x, y, w, h),
                  interactive=role not in ("AXStaticText", "AXHeading"), **kw)  # fmt: skip
    snap.elements.append(e)
    return e


def _menubar(snap: Snapshot, *titles: str) -> None:
    for i, t in enumerate(titles):
        _el(snap, "AXMenuBarItem", "AXMenuBar", 40 + 50 * i, 0, 40, 22, title=t)


def invoice_name(i: int) -> str:
    return f"invoice-{401 + i:04d}.pdf"


def invoice_list(n: int = 50, icons: bool = False) -> Snapshot:
    """A file list, one row per invoice: name, date and amount cells, then Edit / Download / Delete buttons. With
    `icons`, the buttons have no title; only Delete on even rows has a tooltip ("Move to Trash")."""
    snap = Snapshot("Files", "x.files", 0, "Invoices", [])
    _menubar(snap, "Files", "File", "Edit", "View")
    tb = "AXWindow:Invoices > AXToolbar"
    for i, name in enumerate(["New Folder", "Upload", "Share"]):
        _el(snap, "AXButton", tb, 100 + 90 * i, 40, label=name)
    _el(snap, "AXTextField", tb, 600, 40, 200, placeholder="Search")
    table = "AXWindow:Invoices > AXScrollArea > AXTable"
    for i in range(n):
        y = 100 + 30 * i
        _el(snap, "AXRow", table, 100, y, 700, 28)
        row = f"{table} > AXRow"
        _el(snap, "AXStaticText", f"{row} > AXCell", 110, y + 4, 200, value=invoice_name(i))
        _el(snap, "AXStaticText", f"{row} > AXCell", 350, y + 4, value=f"2026-03-{i % 28 + 1:02d}")
        _el(snap, "AXStaticText", f"{row} > AXCell", 450, y + 4, value=f"${120 + 7 * i}.00")
        if icons:
            _el(snap, "AXButton", row, 600, y + 4, 24)
            _el(snap, "AXButton", row, 650, y + 4, 24)
            _el(snap, "AXButton", row, 700, y + 4, 24, help="Move to Trash" if i % 2 == 0 else "")
        else:
            _el(snap, "AXButton", row, 600, y + 4, 40, title="Edit")
            _el(snap, "AXButton", row, 650, y + 4, 40, title="Download")
            _el(snap, "AXButton", row, 700, y + 4, 40, title="Delete")
    add_context(snap.elements)
    return snap


def checkout_form() -> Snapshot:
    """Shipping and Billing address groups with the same four fields, labeled by static text to their left, and a
    contact group whose labels sit above the fields (Email / Confirm email, Password / Confirm password)."""
    snap = Snapshot("Shop", "x.shop", 0, "Checkout", [])
    _menubar(snap, "Shop", "File", "Edit")
    base = "AXWindow:Checkout > AXScrollArea"
    contact = f"{base} > AXGroup:Contact"
    for i, (label, role) in enumerate([("Email", "AXTextField"), ("Confirm email", "AXTextField"),
                                       ("Password", "AXSecureTextField"), ("Confirm password", "AXSecureTextField")]):  # fmt: skip
        y = 80 + 50 * i
        _el(snap, "AXStaticText", contact, 100, y, 120, 16, value=label)
        _el(snap, role, contact, 100, y + 20, 300, 22)
    for g, top in (("Shipping address", 300), ("Billing address", 480)):
        group = f"{base} > AXGroup:{g}"
        _el(snap, "AXHeading", group, 100, top, 200, value=g)
        for i, label in enumerate(["Full name", "Street", "City", "ZIP code"]):
            y = top + 30 + 32 * i
            _el(snap, "AXStaticText", group, 100, y + 3, 80, 16, value=label)
            _el(snap, "AXTextField", group, 200, y, 250, 22)
    _el(snap, "AXCheckBox", base, 100, 660, 200, title="Billing same as shipping", value="0")
    _el(snap, "AXButton", base, 100, 700, 120, title="Place order")
    add_context(snap.elements)
    return snap


ARC_TABS = [
    "Gmail - Inbox", "Google Calendar", "Hacker News", "GitHub Docs", "Linear - My issues", "Figma", "YouTube",
    "Notion - Roadmap", "Stack Overflow", "Slack", "Claude", "Google Drive",
]  # fmt: skip


def arc_tab_name(i: int) -> str:
    if i == 47:
        return "GitHub - Pull requests"
    return ARC_TABS[i] if i < len(ARC_TABS) else f"Article {i}"


def arc_sidebar(n: int = 60) -> Snapshot:
    """Arc's sidebar: toolbar buttons and a tab list where every tab row has its own "Close tab" button."""
    snap = Snapshot("Arc", "company.thebrowser.Browser", 0, "Arc", [])
    _menubar(snap, "Arc", "File", "Edit", "View", "Spaces", "Tabs", "Archive", "Extensions", "Window", "Help")
    side = "AXWindow:Arc > AXGroup:Sidebar"
    for i, name in enumerate(["Toggle Sidebar", "Back", "Forward", "Reload"]):
        _el(snap, "AXButton", f"{side} > AXToolbar", 10 + 30 * i, 40, 24, label=name)
    _el(snap, "AXTextField", side, 10, 70, 220, 26, placeholder="Search or Enter URL…")
    _el(snap, "AXButton", side, 10, 100, 220, 26, label="New Tab")
    tabs = f"{side} > AXOutline:Tabs"
    for i in range(n):
        y = 140 + 30 * i
        _el(snap, "AXRow", tabs, 10, y, 220, 28, label=arc_tab_name(i))
        _el(snap, "AXButton", f"{tabs} > AXRow", 205, y + 4, 20, label="Close tab")
    add_context(snap.elements)
    return snap


def unnamed_rows(n: int = 200) -> Snapshot:
    """Rows with nothing to tell them apart: the tie is bigger than prefilter_max_k and has to be reported."""
    snap = Snapshot("List", "x.list", 0, "Items", [])
    for i in range(n):
        _el(snap, "AXRow", "AXWindow:Items > AXTable", 100, 100 + 30 * i, 500, 28)
        _el(snap, "AXButton", "AXWindow:Items > AXTable > AXRow", 540, 104 + 30 * i, 50, title="Delete")
    add_context(snap.elements)
    return snap


# --- cases -------------------------------------------------------------------------------------------------------


def open_panel() -> Snapshot:
    """Live TextEdit Open panel (file names anonymized): the dialog's buttons follow ~1,300 list rows."""
    from evals.planner_cases import fixture

    return fixture("te_open_panel")


def click(description: str, role: str = "AXButton") -> PlannerOutput:
    return PlannerOutput(done=False, reasoning="r", action="click", target={"role": role, "description": description})


def type_into(text: str, description: str) -> PlannerOutput:
    return PlannerOutput(done=False, reasoning="r", action="type", text=text,
                         target={"role": "AXTextField", "description": description})  # fmt: skip


def in_row(name: str, x: float | None = None, title: str | None = None) -> Callable[[UIElement], bool]:
    """The button in the row whose name starts with `name`, picked by its title or (icon-only buttons) its x position."""

    def check(e: UIElement) -> bool:
        return (e.role == "AXButton" and f'row "{name}' in e.context
                and (title is None or e.name() == title) and (x is None or e.frame.x == x))  # fmt: skip

    return check


def labeled(label: str, group: str = "") -> Callable[[UIElement], bool]:
    return lambda e: f'label "{label}"' in e.context and group in e.path and e.interactive


@dataclass
class SelectorCase:
    name: str
    goal: str
    snap: Callable[[], Snapshot]
    step: PlannerOutput
    expect: Callable[[UIElement], bool]
    # The tie at the cut-off is bigger than prefilter_max_k: success is reporting `truncated`, not the pick.
    expect_truncated: bool = False


CASES: list[SelectorCase] = [
    SelectorCase("invoice_delete_25", "Delete invoice-0425", invoice_list,
                 click("Delete button for invoice-0425"), in_row(invoice_name(24), title="Delete")),
    SelectorCase("invoice_delete_7", "Delete invoice-0407", invoice_list,
                 click("Delete button in the invoice-0407 row"), in_row(invoice_name(6), title="Delete")),
    SelectorCase("invoice_download_49", "Download the last invoice, invoice-0450", invoice_list,
                 click("Download button for invoice-0450"), in_row(invoice_name(49), title="Download")),
    SelectorCase("invoice_icon_trash_41", "Delete invoice-0441", lambda: invoice_list(icons=True),
                 click("trash button in the invoice-0441 row"), in_row(invoice_name(40), x=700)),
    SelectorCase("invoice_icon_delete_42", "Delete invoice-0442", lambda: invoice_list(icons=True),
                 click("delete button for invoice-0442"), in_row(invoice_name(41), x=700)),
    SelectorCase("form_billing_city", "Fill in Springfield as the billing city", checkout_form,
                 type_into("Springfield", "City field in the billing address"), labeled("City", "Billing")),
    SelectorCase("form_shipping_zip", "Shipping ZIP is 62704", checkout_form,
                 type_into("62704", "ZIP code field in shipping address"), labeled("ZIP code", "Shipping")),
    SelectorCase("form_confirm_email", "Confirm the email address", checkout_form,
                 type_into("me@example.com", "Confirm email field"), labeled("Confirm email")),
    SelectorCase("arc_close_github", "Close the GitHub pull requests tab in Arc", arc_sidebar,
                 click("close button on the GitHub pull requests tab"), in_row("GitHub - Pull requests")),
    SelectorCase("arc_new_tab", "Open a new tab in Arc", arc_sidebar,
                 click("New Tab button"), lambda e: e.label == "New Tab"),
    SelectorCase("open_panel_new_document", "Open a new document in TextEdit", open_panel,
                 click("New Document button"), lambda e: e.title == "New Document"),
    SelectorCase("unnamed_delete_200", "Delete an item", unnamed_rows,
                 click("Delete button"), lambda e: e.title == "Delete", expect_truncated=True),
    # --- web pages (evals/fixtures/web) ---
    SelectorCase("web_search_field", 'Search the docs for "llama"', lambda: web_snapshot("web_search_field"),
                 type_into("llama", "search field"), lambda e: e.role == "AXSearchField"),
    SelectorCase("web_form_billing_city", 'Type "Berlin" into the billing City field',
                 lambda: web_snapshot("web_form_labels"), type_into("Berlin", "City field in the billing address"),
                 lambda e: e.role == "AXTextField" and e.title == "City" and "Billing" in e.path),
    SelectorCase("web_icon_settings", "Open settings", lambda: web_snapshot("web_icon_buttons"),
                 click("Settings button"), lambda e: e.title == "Settings" and e.web_id != ""),
    SelectorCase("web_row_download_0425", "Download invoice-0425", lambda: web_snapshot("web_row_actions"),
                 click("Download button for invoice-0425"),
                 lambda e: e.title == "Download" and 'row "invoice-0425' in e.context),
    SelectorCase("web_pricing_link", "Open the Pricing page", lambda: web_snapshot("web_link_list"),
                 click("Pricing link", "AXLink"), lambda e: e.role == "AXLink" and e.title == "Pricing"),
    SelectorCase("web_cookie_reject", "Decline the cookies", lambda: web_snapshot("web_cookie_banner"),
                 click("Reject non-essential button"), lambda e: e.title == "Reject non-essential"),
    SelectorCase("web_modal_confirm", "Confirm the dialog", lambda: web_snapshot("web_modal_dialog"),
                 click("Confirm button in the dialog"), lambda e: e.title == "Confirm" and "AXDialog" in e.path),
    SelectorCase("web_signin_visible", "Click Sign in", lambda: web_snapshot("web_hidden_dupes"),
                 click("Sign in button"), lambda e: e.title == "Sign in" and e.role == "AXButton"),
    SelectorCase("web_spa_billing_tab", "Open the Billing tab", lambda: web_snapshot("web_spa_tabs"),
                 click("Billing tab", "AXTab"), lambda e: e.role == "AXTab" and e.title == "Billing"),
    SelectorCase("web_password_target", 'Type "x" into the password field', lambda: web_snapshot("web_password_field"),
                 type_into("x", "Password field"), lambda e: e.role == "AXSecureTextField"),
    SelectorCase("web_new_chat", "Open a new chat", lambda: web_snapshot("web_new_chat"),
                 click("New chat button"), lambda e: e.title == "New chat"),
]  # fmt: skip


# --- planner number picks: the ambiguity guard (selector/jev.py::planner_pick) -----------------------------------


@dataclass
class PickCase:
    """The planner answered with a number (`picked`) and a description. `expect_jev`: the guard should drop the
    number and let Jev choose; otherwise the number should stand."""

    name: str
    goal: str
    snap: Callable[[], Snapshot]
    step: Callable[[int], PlannerOutput]  # step for the picked id
    picked: Callable[[UIElement], bool]
    expect: Callable[[UIElement], bool]
    expect_jev: bool


def _with_id(step: PlannerOutput, el_id: int) -> PlannerOutput:
    return step.model_copy(update={"target": step.target.model_copy(update={"id": el_id})})


def numbered(step: PlannerOutput) -> Callable[[int], PlannerOutput]:
    return lambda el_id: _with_id(step, el_id)


PICK_CASES: list[PickCase] = [
    PickCase("wrong_row_delete", "Delete invoice-0425", invoice_list,
             numbered(click("Delete button for invoice-0425")),
             in_row(invoice_name(0), title="Delete"), in_row(invoice_name(24), title="Delete"), expect_jev=True),
    PickCase("right_row_delete", "Delete invoice-0425", invoice_list,
             numbered(click("Delete button for invoice-0425")),
             in_row(invoice_name(24), title="Delete"), in_row(invoice_name(24), title="Delete"), expect_jev=False),
    PickCase("vague_description_kept", "Delete an invoice", invoice_list, numbered(click("Delete button")),
             in_row(invoice_name(3), title="Delete"), in_row(invoice_name(3), title="Delete"), expect_jev=False),
    PickCase("wrong_city_field", "Fill in the billing city", checkout_form,
             numbered(type_into("Springfield", "City field in the billing address")),
             labeled("City", "Shipping"), labeled("City", "Billing"), expect_jev=True),
    PickCase("wrong_arc_tab", "Close the GitHub pull requests tab", arc_sidebar,
             numbered(click("close button on the GitHub pull requests tab")),
             in_row(arc_tab_name(0)), in_row(arc_tab_name(47)), expect_jev=True),
    PickCase("unique_new_tab", "Open a new tab in Arc", arc_sidebar, numbered(click("New Tab button")),
             lambda e: e.label == "New Tab", lambda e: e.label == "New Tab", expect_jev=False),
]  # fmt: skip
