"""Planner eval cases built on real TextEdit snapshots (evals/fixtures, from evals/capture_fixtures.py) plus a few
synthetic states (error sheet, find bar, a 300-file Finder window).

Each case gives the planner what the agent would (goal, Snapshot.scope(), optional screen view, history) and checks
the one step it returns. Checks resolve `target.id` against the snapshot, so a number pick and a description that Jev
would match both count. Nothing is executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Callable

from local_agent.history import StepRecord
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import _role_matches

FIXTURES = Path(__file__).parent / "fixtures"


@cache
def _load(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text()


def fixture(name: str) -> Snapshot:
    return Snapshot.from_dict(json.loads(_load(name)))


def _add(snap: Snapshot, role: str, path: str, y: float, x: float = 400, **kw) -> UIElement:
    e = UIElement(id=max(e.id for e in snap.elements) + 1, role=role, path=path, frame=Frame(x, y, 80, 20),
                  interactive=role not in ("AXStaticText", "AXHeading"), **kw)  # fmt: skip
    snap.elements.append(e)
    return e


def te_empty() -> Snapshot:
    snap = fixture("te_plain")
    for e in snap.elements:
        if e.role == "AXTextArea":
            e.value = e.selected = ""
    return snap


def te_save_named() -> Snapshot:
    snap = fixture("te_save_sheet")
    for e in snap.elements:
        if e.role == "AXTextField" and e.focused:
            e.value, e.selected = "notes", ""
    return snap


def te_error_sheet() -> Snapshot:
    snap = fixture("te_rich")
    sheet = "AXWindow:Untitled > AXSheet"
    _add(snap, "AXStaticText", sheet, 200, value="The document “Untitled” could not be saved as “notes”.")
    _add(snap, "AXStaticText", sheet, 230, value="You don’t have permission to save in the folder “Desktop”.")
    _add(snap, "AXButton", sheet, 280, title="OK")
    return snap


def te_find_bar() -> Snapshot:
    snap = fixture("te_rich")
    bar = "AXWindow:Untitled > AXGroup:find bar"
    _add(snap, "AXTextField", bar, 150, 160, placeholder="Find")
    _add(snap, "AXButton", bar, 150, 420, label="previous match")
    _add(snap, "AXButton", bar, 150, 450, label="next match")
    _add(snap, "AXButton", bar, 150, 520, title="Done")
    _add(snap, "AXTextField", bar, 175, 160, placeholder="Replace")
    _add(snap, "AXButton", bar, 175, 420, title="Replace")
    _add(snap, "AXButton", bar, 175, 500, title="All")
    return snap


def finder() -> Snapshot:
    snap = Snapshot("Finder", "com.apple.finder", 0, "Documents", [])
    snap.elements.append(UIElement(id=0, role="AXMenuBarItem", title="File", path="AXMenuBar", frame=Frame(100, 0, 30, 24)))
    tb = "AXWindow:Documents > AXToolbar"
    for i, name in enumerate(["Back", "Forward", "View", "Group", "Share", "Add Tags", "Action"]):
        _add(snap, "AXButton", tb, 40, 100 + 40 * i, label=name)
    _add(snap, "AXTextField", tb, 40, 500, placeholder="Search")
    names = [f"scan_{i:03d}.png" for i in range(300)]
    names[2], names[200] = "Q3 report.pdf", "invoice-0425.pdf"
    for i, n in enumerate(names):
        _add(snap, "AXRow", "AXWindow:Documents > AXScrollArea > AXOutline", 100 + 22 * i, 120, label=n)
    _add(snap, "AXStaticText", "AXWindow:Documents", 900, 300, value="302 items, 31.4 GB available")
    return snap


SNAPSHOTS: dict[str, Callable[[], Snapshot]] = {
    **{n: (lambda n=n: fixture(n)) for n in
       ("te_plain", "te_plain_format", "te_rich", "te_rich_format", "te_rich_font", "te_save_sheet")},
    "te_empty": te_empty, "te_save_named": te_save_named, "te_error_sheet": te_error_sheet,
    "te_find_bar": te_find_bar, "finder": finder,
}  # fmt: skip


# --- history helpers --------------------------------------------------------------------------------------------


def ok(n: int, plan: str, element: str | None = None, p: float | None = None, note: str = "") -> StepRecord:
    return StepRecord(n, plan, element=element, probability=p, executed=True, verified=True, verify_probability=0.97,
                      note=note)  # fmt: skip


def failed(n: int, plan: str, element: str | None = None, p: float | None = None) -> StepRecord:
    return StepRecord(n, plan, element=element, probability=p, executed=True, verified=False, verify_probability=0.08)


def skipped(n: int, plan: str, element: str, p: float) -> StepRecord:
    return StepRecord(n, plan, element=element, probability=p,
                      note=f"skipped: best match probability {p:.2f} below threshold 0.50 (8 candidates); "
                      "describe the target differently or use another action")  # fmt: skip


# --- checks: return "" on pass, else why it failed --------------------------------------------------------------

Check = Callable[[PlannerOutput, Snapshot], str]


def resolve(p: PlannerOutput, snap: Snapshot) -> tuple[str, str, UIElement | None]:
    """(role, searchable text, element) of the step's target: the numbered element if the id exists, else the
    planner's own role/description (what Jev would search for)."""
    t = p.target
    if t is None:
        return "", "", None
    el = next((e for e in snap.elements if t.id is not None and e.id == t.id), None)
    if el is not None:
        return el.role, f"{el.describe()} {el.name()} {t.description}".lower(), el
    return t.role, t.description.lower(), None


def shortcut(*accepted: str) -> Check:
    def check(p, snap):
        keys = (p.keys or "").replace(" ", "").lower()
        return "" if p.action == "shortcut" and keys in accepted else f"wanted shortcut {accepted}, got {p.summary()}"

    return check


def click(role: str, *words: str, forbid: tuple[str, ...] = (), actions=("click",)) -> Check:
    def check(p, snap):
        if p.action not in actions or p.target is None:
            return f"wanted {actions[0]} [{role}] {words}, got {p.summary()}"
        got_role, text, el = resolve(p, snap)
        if got_role != "any" and not _role_matches(role, got_role):
            return f"wanted role {role}, got {got_role}" + (f" ([{el.id}] {el.describe(60)})" if el else "")
        if not all(w in text for w in words):
            return f"target {text[:80]!r} missing {words}"
        if any(w in text for w in forbid):
            return f"target {text[:80]!r} names {forbid}, which isn't visible yet"
        return ""

    return check


def terminal(*fragments: str) -> Check:
    def check(p, snap):
        cmd = (p.command or "").lower()
        ok_ = p.action == "terminal" and all(f in cmd for f in fragments)
        return "" if ok_ else f"wanted terminal containing {fragments}, got {p.summary()}"

    return check


def typed(fragment: str, into: tuple[str, ...] = (), focused_ok: bool = False) -> Check:
    """Type `fragment`; with `into`, the target must resolve to an element naming all those words (or be null when
    the focused element is already the right field and focused_ok)."""

    def check(p, snap):
        if p.action != "type" or fragment not in (p.text or "").lower():
            return f"wanted type {fragment!r}, got {p.summary()}"
        if not into:
            return ""
        if p.target is None:
            return "" if focused_ok else f"wanted it typed into {into}, got no target"
        _, text, _ = resolve(p, snap)
        return "" if all(w in text for w in into) else f"typed into {text[:80]!r}, wanted {into}"

    return check


def done(p, snap) -> str:
    return "" if p.done else f"wanted done, got {p.summary()}"


def done_saying(fragment: str) -> Check:
    def check(p, snap):
        return "" if p.done and fragment in p.reasoning else f"wanted done mentioning {fragment!r}, got {p.summary()}"

    return check


def not_repeating(plan: str, also: Check | None = None) -> Check:
    def check(p, snap):
        if p.summary() == plan:
            return f"repeated the failing step {plan!r}"
        return also(p, snap) if also else ""

    return check


def any_of(*checks: Check) -> Check:
    def check(p, snap):
        reasons = [c(p, snap) for c in checks]
        return "" if "" in reasons else " | ".join(reasons)

    return check


@dataclass
class Case:
    name: str
    category: str
    goal: str
    snapshot: str  # key in SNAPSHOTS
    check: Check
    history: list[StepRecord] = field(default_factory=list)
    needs_screen: bool = False  # only solvable by reading the screen (a view-off planner is expected to miss it)

    def snap(self) -> Snapshot:
        return SNAPSHOTS[self.snapshot]()


BOLD_GOAL = "Select all the text, then use the Format menu (not a keyboard shortcut) to make it bold"
RICH_GOAL = "Use the Format menu to make this document rich text, then make all of its text bold using the Format menu"
SAVE_GOAL = "Save this document as notes"
CLICK_TEXT = "click [AXTextArea] Hello world"
FORMAT = 'AXMenuBarItem title="Format" in AXMenuBar at (203,0)'
OPEN = ("click", "double_click")

CASES: list[Case] = [
    # --- from live TextEdit runs ---------------------------------------------------------------------------------
    Case("select_all", "shortcuts", "Select all the text in this document", "te_plain", shortcut("cmd+a")),
    Case("select_all_first", "shortcuts", BOLD_GOAL, "te_plain", shortcut("cmd+a")),
    Case("new_document", "shortcuts", "Create a new TextEdit document", "te_plain", shortcut("cmd+n")),
    Case("close_menu", "shortcuts", "Close this menu without choosing anything", "te_rich_format",
         shortcut("escape", "esc"), [ok(1, "click [AXMenuBarItem] Format", FORMAT, 0.99)]),
    Case("open_format_menu", "menus", BOLD_GOAL, "te_rich", click("AXMenuBarItem", "format"),
         [ok(1, "shortcut keys=cmd+a")]),
    Case("submenu_parent", "menus", BOLD_GOAL, "te_rich_format", click("AXMenuItem", "font", forbid=("bold",)),
         [ok(1, "shortcut keys=cmd+a"), ok(2, "click [AXMenuBarItem] Format", FORMAT, 0.99)]),
    Case("submenu_item", "menus", BOLD_GOAL, "te_rich_font", click("AXMenuItem", "bold"),
         [ok(1, "shortcut keys=cmd+a"), ok(2, "click [AXMenuBarItem] Format", FORMAT, 0.99),
          ok(3, "click [AXMenuItem] Font", 'AXMenuItem title="Font" in AXMenu', 0.83)]),
    Case("make_rich_text", "menus", RICH_GOAL, "te_plain_format", click("AXMenuItem", "rich"),
         [ok(1, "click [AXMenuBarItem] Format", FORMAT, 0.99)]),
    Case("recover_repeated_click", "recovery", BOLD_GOAL, "te_plain", not_repeating(CLICK_TEXT, shortcut("cmd+a")),
         [failed(1, CLICK_TEXT, "AXTextArea", 0.77), failed(2, CLICK_TEXT, "AXTextArea", 0.77)]),
    Case("recover_hidden_item", "recovery", BOLD_GOAL, "te_rich", click("AXMenuBarItem", "format"),
         [ok(1, "shortcut keys=cmd+a"), skipped(2, "click [AXMenuItem] Bold", FORMAT, 0.41)]),
    Case("recover_failed_shortcut", "recovery", "Select all the text in this document", "te_plain",
         not_repeating("shortcut keys=cmd+a"), [failed(1, "shortcut keys=cmd+a"), failed(2, "shortcut keys=cmd+a")]),
    Case("list_downloads", "terminal", "List the files in my Downloads folder", "te_plain", terminal("ls", "downloads")),
    Case("make_folder", "terminal", "Create a folder called reports on my Desktop", "te_plain",
         terminal("mkdir", "desktop", "reports")),
    Case("open_app", "terminal", "Open the Calculator app", "te_plain", terminal("open", "calculator")),
    Case("done_after_typing", "completion", "Type hello world into the document", "te_plain", done,
         [ok(1, "type text='hello world'", note="typed 11 chars")]),
    Case("done_after_terminal", "completion", "How many files are in my Downloads folder?", "te_plain", done,
         [StepRecord(1, "terminal command='ls ~/Downloads | wc -l'", executed=True, verified=True,
                     note="exit=0 output: 42")]),
    Case("not_done_yet", "completion", "Make a new document and type hi there", "te_empty", typed("hi there"),
         [ok(1, "shortcut keys=cmd+n")]),
    # --- need what's on screen -----------------------------------------------------------------------------------
    Case("toolbar_strikethrough", "screen", "Strike through the selected text", "te_rich",
         click("AXCheckBox", "strikethrough"), needs_screen=True),
    Case("font_size", "screen", "Change the font size of the selected text to 18", "te_rich",
         any_of(typed("18", into=("font size",)), click("AXComboBox", "font size")), needs_screen=True),
    Case("save_where", "screen", "Save this document in my Documents folder", "te_save_sheet",
         click("AXPopUpButton", "where"), [ok(1, "shortcut keys=cmd+s")], needs_screen=True),
    Case("save_name", "screen", SAVE_GOAL, "te_save_sheet", typed("notes", into=("untitled",), focused_ok=True),
         [ok(1, "shortcut keys=cmd+s")]),
    Case("save_confirm", "screen", SAVE_GOAL, "te_save_named",
         any_of(click("AXButton", "save"), shortcut("return", "enter")),
         [ok(1, "shortcut keys=cmd+s"), ok(2, "type text='notes'", note="typed 5 chars")]),
    Case("error_dismiss", "screen", SAVE_GOAL, "te_error_sheet", click("AXButton", "ok"),
         [ok(1, "shortcut keys=cmd+s"), ok(2, "type text='notes'", note="typed 5 chars"),
          ok(3, "click [AXButton] Save", 'AXButton title="Save" in AXSheet', 0.98)], needs_screen=True),
    Case("find_field", "screen", "Replace every 'cat' in the document with 'dog'", "te_find_bar",
         any_of(typed("cat", into=("find",)), click("AXTextField", "find")),
         [ok(1, "shortcut keys=cmd+f")], needs_screen=True),
    Case("finder_open_visible", "screen", "Open the file Q3 report.pdf", "finder",
         any_of(click("AXRow", "q3 report", actions=OPEN), terminal("q3 report"))),
    Case("finder_open_hidden", "screen", "Open the file invoice-0425.pdf", "finder",
         any_of(click("AXRow", "invoice-0425", actions=OPEN), terminal("invoice-0425"))),
    Case("finder_back", "screen", "Go back to the previous folder", "finder",
         any_of(click("AXButton", "back"), shortcut("cmd+[", "cmd+up"))),
    Case("read_free_space", "screen", "How much free disk space is available? Answer in your reasoning and finish.",
         "finder", any_of(done_saying("31.4"), terminal("df")), needs_screen=True),
]  # fmt: skip
