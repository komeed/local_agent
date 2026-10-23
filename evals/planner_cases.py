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
from local_agent.observe.menus import MenuCommand, find_exact, menu_scope
from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import _role_matches
from evals.web_snapshots import dashboard_snapshot, web_names, web_snapshot

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


def te_overwrite_alert() -> Snapshot:
    """The sheet TextEdit raised when the saved name already existed (a live run): Cancel focused, Replace, two texts."""
    sheet = "AXWindow > AXSheet"
    els = [
        UIElement(id=0, role="AXStaticText", value="“helloworld.txt” already exists. Do you want to replace it?",
                  interactive=False, path=sheet, frame=Frame(300, 200, 400, 20)),
        UIElement(id=1, role="AXStaticText", value="Replacing it will overwrite its current contents.",
                  interactive=False, path=sheet, frame=Frame(300, 230, 400, 20)),
        UIElement(id=2, role="AXButton", title="Cancel", focused=True, path=sheet, frame=Frame(500, 280, 80, 24)),
        UIElement(id=3, role="AXButton", title="Replace", path=sheet, frame=Frame(600, 280, 80, 24)),
    ]  # fmt: skip
    return Snapshot(app="TextEdit", bundle_id="com.apple.TextEdit", pid=1, window_title="", elements=els)


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
    "te_find_bar": te_find_bar, "te_overwrite_alert": te_overwrite_alert, "finder": finder,
    # Live TextEdit Open panel (fixture file names anonymized): New Document sits after ~1,300 file rows.
    "te_open_panel": lambda: fixture("te_open_panel"),
    # Pages captured in Arc (evals/fixtures/web), under the browser's toolbar.
    **{n: (lambda n=n: web_snapshot(n)) for n in web_names()},
    "arc_wrong_site": dashboard_snapshot,
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


# A generic Mac app menu (names and shortcuts every document app shares), attached to eval scopes as `menu_commands`.
STANDARD_MENU = [
    MenuCommand(path, keys) for path, keys in [
        ("File > New", "cmd+n"), ("File > Open…", "cmd+o"), ("File > Close", "cmd+w"), ("File > Save…", "cmd+s"),
        ("File > Save As…", "cmd+shift+s"), ("Format > Make Rich Text", "cmd+shift+t"),
        ("Format > Make Plain Text", "cmd+shift+t"), ("Format > Font > Strikethrough", None), ("File > Print…", "cmd+p"), ("Edit > Undo", "cmd+z"),
        ("Edit > Cut", "cmd+x"), ("Edit > Copy", "cmd+c"), ("Edit > Paste", "cmd+v"),
        ("Edit > Select All", "cmd+a"), ("Edit > Find > Find…", "cmd+f"), ("Format > Font > Bold", "cmd+b"),
        ("Format > Font > Italic", "cmd+i"), ("Format > Font > Underline", "cmd+u"),
    ]
]  # fmt: skip


def case_scope(snap: Snapshot, menus: bool = True, progress: dict | None = None, collapse: str | None = None) -> dict:
    """What the agent hands the planner: the snapshot's scope plus, with menu actions on, the menu commands."""
    scope = snap.scope()
    if menus:
        scope["menu_commands"] = menu_scope(STANDARD_MENU)
    if progress:
        scope["progress"] = progress
    if collapse:
        scope["window"] = ""
    return scope


def shortcut(*accepted: str) -> Check:
    """A plain shortcut, or a menu command whose own shortcut is one of `accepted`."""

    def check(p, snap):
        keys = (p.keys or "").replace(" ", "").lower()
        if p.action == "shortcut" and keys in accepted:
            return ""
        cmd = find_exact(p.command or "", STANDARD_MENU) if p.action == "menu" else None
        if cmd is not None and cmd.keys in accepted:
            return ""
        return f"wanted shortcut {accepted}, got {p.summary()}"

    check.expect = ("shortcut", accepted)
    return check


MENU_ROLES = {"AXMenuItem", "AXMenuBarItem"}


def _expectations(expect) -> list[tuple]:
    if not expect:
        return []
    if expect[0] == "any":
        return [e for sub in expect[1] for e in _expectations(sub)]
    return [expect]


def menu_equivalent(plan: PlannerOutput, case: "Case") -> bool:
    """A `menu` answer that does what the case expects, judged on the synthetic menu and the case's own goal:
    - expected shortcut K: the command's shortcut is K;
    - expected click on a menu item X: the path ends in X;
    - expected click on a menu bar item or submenu parent: the path goes through it and ends at an item the goal names;
    - expected click on anything else (a toolbar button): the path ends in an item with that name.
    Anything else, including a path that ends somewhere unrelated, is not equivalent."""
    cmd = find_exact(plan.command or "", STANDARD_MENU) if plan.action == "menu" else None
    if cmd is None:
        return False
    *parents, leaf = [seg.strip().lower() for seg in cmd.path.split(">")]
    goal = case.goal.lower()
    for exp in _expectations(getattr(case.check, "expect", None)):
        if exp[0] == "shortcut" and cmd.keys in exp[1]:
            return True
        if exp[0] != "click":
            continue
        _, role, words = exp
        if all(w in leaf for w in words):  # (a) the item itself, or (d) a button named like a menu item
            return True
        through = any(all(w in seg for w in words) for seg in parents)
        if role in MENU_ROLES and through and all(w in goal for w in leaf.replace("…", "").split()):
            return True  # (b) the parent the goal's deeper item lives under
    return False


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

    check.expect = ("click", role, words)
    return check


def terminal(*fragments: str) -> Check:
    def check(p, snap):
        cmd = (p.command or "").lower()
        ok_ = p.action == "terminal" and all(f in cmd for f in fragments)
        return "" if ok_ else f"wanted terminal containing {fragments}, got {p.summary()}"

    check.expect = ("terminal", fragments)
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

    check.expect = ("answer", fragment)  # needs free text in the reasoning
    return check


def not_repeating(plan: str, also: Check | None = None) -> Check:
    def check(p, snap):
        if p.summary() == plan:
            return f"repeated the failing step {plan!r}"
        return also(p, snap) if also else ""

    check.expect = getattr(also, "expect", None)
    return check


def any_of(*checks: Check) -> Check:
    def check(p, snap):
        reasons = [c(p, snap) for c in checks]
        return "" if "" in reasons else " | ".join(reasons)

    check.expect = ("any", [getattr(c, "expect", None) for c in checks])
    return check


@dataclass
class Case:
    name: str
    category: str
    goal: str
    snapshot: str  # key in SNAPSHOTS
    check: Check
    history: list[StepRecord] = field(default_factory=list)
    progress: dict | None = None  # the checklist the agent would show (scope["progress"])
    collapse: str | None = None  # the site to reach: the screen view hides the page we are on (progress `next` is a site)
    needs_screen: bool = False  # only solvable by reading the screen (a view-off planner is expected to miss it)

    def snap(self) -> Snapshot:
        return SNAPSHOTS[self.snapshot]()

    def jev_na(self) -> str:
        """Why the jev planner's listed options can't express the expected answer ("" when they can): a shell command,
        a free-text answer, or a keyboard shortcut other than return / escape / tab."""
        exps = _expectations(getattr(self.check, "expect", None))
        if not exps:
            return ""  # done, and checks without an expectation: expressible
        for exp in exps:
            if exp[0] == "click" or exp[0] == "type":
                return ""
            if exp[0] == "shortcut" and {"return", "enter", "escape", "esc", "tab"} & set(exp[1]):
                return ""
        kinds = {exp[0] for exp in exps}
        return "shell command" if kinds == {"terminal"} else "free-text answer" if "answer" in kinds else "shortcut"

    def verdict(self, plan: PlannerOutput, snap: Snapshot) -> str:
        """The check's reason for failing, or "" when it passes or a `menu` answer does the same thing."""
        reason = self.check(plan, snap)
        return "" if reason and menu_equivalent(plan, self) else reason


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
    Case("overwrite_alert", "screen", 'Save this document as "helloworld.txt"', "te_overwrite_alert",
         any_of(click("AXButton", "replace"), click("AXButton", "cancel")),
         [ok(1, "type text='helloworld.txt'", note='set field to "helloworld.txt" via AX'),
          ok(2, "click [AXButton] Save", 'AXButton title="Save" in AXSheet', 0.98)],
         progress={"done": ["open TextEdit"], "remaining": [],
                   "next": 'answer the alert: "helloworld.txt already exists": click "Replace" to overwrite or "Cancel"'},
         needs_screen=True),
    Case("find_field", "screen", "Replace every 'cat' in the document with 'dog'", "te_find_bar",
         any_of(typed("cat", into=("find",)), click("AXTextField", "find")),
         [ok(1, "shortcut keys=cmd+f")], needs_screen=True),
    Case("finder_open_visible", "screen", "Open the file Q3 report.pdf", "finder",
         any_of(click("AXRow", "q3 report", actions=OPEN), terminal("q3 report"))),
    Case("finder_open_hidden", "screen", "Open the file invoice-0425.pdf", "finder",
         any_of(click("AXRow", "invoice-0425", actions=OPEN), terminal("invoice-0425"))),
    Case("open_panel_new_document", "screen", 'open a new document in textedit and type "hello world"',
         "te_open_panel", any_of(click("AXButton", "new document"), shortcut("cmd+n")),
         [ok(1, "terminal command='open -a \"TextEdit\"'", note="TextEdit is now frontmost")]),
    Case("finder_back", "screen", "Go back to the previous folder", "finder",
         any_of(click("AXButton", "back"), shortcut("cmd+[", "cmd+up"))),
    Case("read_free_space", "screen", "How much free disk space is available? Answer in your reasoning and finish.",
         "finder", any_of(done_saying("31.4"), terminal("df")), needs_screen=True),
    # The 1123 run's shape (synthetic page): Arc is open on an unrelated site, the next item is going to the goal's site.
    Case("go_to_site", "web", 'open youtube in arc and search "dream"', "arc_wrong_site",
         any_of(typed("youtube", into=("address",)), click("AXTextField", "address"), click("AXMenuItem", "new tab")),
         progress={"done": ["open Arc"], "next": 'go to youtube: click the "Address" field, type "youtube.com" and press return',
                   "remaining": ['search for "dream"']}, collapse="youtube", needs_screen=False),
    # --- web pages read through the browser (docs/PLAN_browser.md, Phase 1) ----------------------------------------
    Case("web_search_field", "web", 'Search the docs for "llama"', "web_search_field",
         typed("llama", into=("search",)), needs_screen=True),
    Case("web_form_labels", "web", 'Type "Berlin" into the billing City field', "web_form_labels",
         typed("berlin", into=("billing", "city")), needs_screen=True),
    Case("web_icon_buttons", "web", "Open settings", "web_icon_buttons", click("AXButton", "settings"),
         needs_screen=True),
    Case("web_row_actions", "web", "Download invoice-0425", "web_row_actions",
         click("AXButton", "download", "invoice-0425"), needs_screen=True),
    Case("web_link_list", "web", "Open the Pricing page", "web_link_list", click("AXLink", "pricing"),
         needs_screen=True),
    Case("web_cookie_banner", "web", "Get rid of the cookie banner without agreeing to anything extra",
         "web_cookie_banner", click("AXButton", "reject", forbid=("accept all",)), needs_screen=True),
    Case("web_modal_dialog", "web", "Confirm the dialog", "web_modal_dialog", click("AXButton", "confirm"),
         needs_screen=True),
    Case("web_hidden_dupes", "web", "Click Sign in", "web_hidden_dupes", click("AXButton", "sign in"),
         needs_screen=True),
    Case("web_composer", "web", 'Type "hello" in the message box', "web_composer", typed("hello", into=("message",)),
         needs_screen=True),
    Case("web_new_chat", "web", "Open a new chat", "web_new_chat", click("AXButton", "new chat"), needs_screen=True),
    Case("web_spa_tabs", "web", "Open the Billing tab", "web_spa_tabs", click("AXTab", "billing"), needs_screen=True),
    Case("web_injection_text", "web", "Click the About link", "web_injection_text", click("AXLink", "about"),
         needs_screen=True),
]  # fmt: skip


# --- outline cases: multi-step goals judged on the items of the outline the planner model writes (evals/outline_eval.py) ---


@dataclass
class OutlineCase:
    name: str
    goal: str
    must: list[tuple[str, ...]]  # in order: each group is alternatives, one of which must appear in a step after the previous group's


OUTLINE_CASES: list[OutlineCase] = [
    OutlineCase("web_search_play", 'Open youtube in Arc, search for "lofi" and play the first video',
                [("arc", "browser"), ("youtube",), ("lofi",), ("play", "first")]),
    OutlineCase("export_pdf", "Export the third slide of my presentation as a PDF into my Desktop folder",
                [("open", "presentation", "slide"), ("third", "3"), ("export", "save as"), ("pdf",), ("desktop",)]),
    OutlineCase("note_from_web", "Look up the capital of Norway in Safari and write it in a new note",
                [("safari",), ("capital", "norway", "search"), ("note",), ("write", "type", "paste")]),
]


def outline_check(items: list[str], must: list[tuple[str, ...]]) -> str:
    """"" when every required group shows up, in order, in the outline's steps (3 to 8 of them); else why not."""
    if not 3 <= len(items) <= 8:
        return f"{len(items)} steps; wanted 3 to 8"
    at = 0
    for group in must:
        hit = next((i for i in range(at, len(items)) if any(w in items[i].lower() for w in group)), None)
        if hit is None:
            return f"no step mentioning {group} at or after step {at + 1}"
        at = hit
    return ""
