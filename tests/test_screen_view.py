import json
from pathlib import Path

from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.observe.screen_view import _tokens, line, render, view_budget

EVAL_FIXTURES = Path(__file__).parent.parent / "evals" / "fixtures"


def load(name):
    return Snapshot.from_dict(json.loads((EVAL_FIXTURES / f"{name}.json").read_text()))


def test_real_textedit_view():
    view = render(load("te_rich"), 3000)
    assert view.splitlines()[1].startswith("Focused: [0] text area")
    assert '[6] checkbox "strikethrough" off' in view
    assert '[11] combo box "font size" = "12"' in view
    assert 'menu "Format"' in view


def test_open_menus_come_before_window_and_mark_submenus():
    view = render(load("te_rich_font"), 3000)
    assert view.index("Open menu Format:") < view.index("Open menu Format > Font:") < view.index('Window "Untitled"')
    assert 'item "Font" ▸' in view and 'item "Bold"' in view
    assert "(disabled)" not in view.split("Window")[0]  # separators dropped


def test_dialog_text_and_buttons():
    view = render(load("te_save_sheet"), 3000)
    dialog = view.split("Dialog:")[1].split("Menu bar:")[0]
    assert '"Where:"' in dialog and 'button "Save"' in dialog and 'popup "Where:" = "Desktop"' in dialog


def test_names_mode_has_no_numbers():
    view = render(load("te_rich"), 3000, numbered=False)
    assert "[" not in view and 'checkbox "bold" off' in view


def finder(n):
    rows = [UIElement(id=i, role="AXRow", label=f"file {i}.pdf", path="AXWindow:Docs > AXOutline",
                      frame=Frame(0, 100 + 20 * i, 300, 20)) for i in range(n)]  # fmt: skip
    rows.append(UIElement(id=n, role="AXButton", label="Back", path="AXWindow:Docs > AXToolbar", frame=Frame(0, 40, 20, 20)))
    return Snapshot("Finder", "com.apple.finder", 1, "Docs", rows)


def test_long_lists_collapse():
    view = render(finder(300), 3000)
    assert view.count("row ") == 12 and "288 more not shown" in view
    assert view.index("Toolbar:") < view.index("List:")


def test_budget_is_respected():
    snap = finder(300)
    snap.elements += [UIElement(id=400 + i, role="AXButton", label=f"Action number {i}", path="AXWindow:Docs",
                                frame=Frame(i, 60, 10, 10)) for i in range(200)]  # fmt: skip
    view = render(snap, 400)
    assert _tokens(view) <= 400 + 40  # trailing note may slightly exceed
    assert "more not shown" in view or "more elements not shown" in view


def test_view_budget_fits_context():
    assert view_budget(8192, 3000) == 3000
    assert view_budget(4096, 3000) == 596
    assert view_budget(2048, 3000) == 400


def test_line_flags():
    e = UIElement(id=7, role="AXTextField", placeholder="Search", focused=True, enabled=False)
    assert line(e) == '[7] text field (placeholder "Search") (focused, disabled)'
