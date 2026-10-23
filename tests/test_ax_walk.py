"""The AX walker's order and naming, on a fake AX tree (no real accessibility calls)."""

import pytest

from local_agent.config import Settings
from local_agent.observe import ax_tree
from local_agent.observe.models import UIElement


class Node:
    def __init__(self, role, title="", children=(), **attrs):
        self.attrs = {"AXRole": role, "AXTitle": title, "AXChildren": list(children), **attrs}


@pytest.fixture
def fake_ax(monkeypatch):
    monkeypatch.setattr(ax_tree, "_attr", lambda el, name: el.attrs.get(name))
    monkeypatch.setattr(ax_tree, "_actions", lambda el: ["AXPress"] if el.attrs["AXRole"] == "AXButton" else [])
    monkeypatch.setattr(ax_tree, "_frame", lambda el: None)
    monkeypatch.setattr(ax_tree, "_child_text", lambda el: "")


def open_panel(rows: int) -> Node:
    """Like TextEdit's Open panel: a file list before the dialog's buttons."""
    files = Node("AXOutline", children=[Node("AXRow", children=[Node("AXStaticText", AXValue=f"f{i}")])
                                        for i in range(rows)])  # fmt: skip
    buttons = [Node("AXButton", t) for t in ("New Document", "Cancel", "Open")]
    return Node("AXWindow", "Open", [Node("AXSplitGroup", children=[Node("AXScrollArea", children=[files]), *buttons])])


def walk(root, max_elements=3000):
    out: list[UIElement] = []
    ax_tree._walk(root, "", Settings(max_elements=max_elements), out, deadline=float("inf"))
    return out


def test_list_rows_are_walked_after_the_dialog_buttons(fake_ax):
    out = walk(open_panel(2000), max_elements=50)  # the limit hits long before the 2,000 rows are done
    names = [e.name() for e in out]
    assert {"New Document", "Cancel", "Open"} <= set(names)
    assert names.index("New Document") < names.index("f0")


def test_every_row_still_walked_without_limits(fake_ax):
    out = walk(open_panel(30))
    assert sum(e.role == "AXRow" for e in out) == 30 and sum(e.role == "AXStaticText" for e in out) == 30


def test_scroll_bar_parts_are_dropped(fake_ax):
    bar = Node("AXScrollBar", children=[Node("AXButton", AXSubrole="AXIncrementPage"), Node("AXButton", "Done")])
    assert [e.name() for e in walk(Node("AXWindow", "w", [bar]))] == ["Done"]


@pytest.mark.parametrize("identifier,name", [("NewDocumentButton", "New Document Button"),
                                             ("Group or Sort By", "Group or Sort By"), ("_NS:123", ""),
                                             ("OKButton", ""), ("x", "")])  # fmt: skip
def test_unnamed_controls_named_from_identifier(fake_ax, identifier, name):
    el = ax_tree._to_element(Node("AXButton", AXIdentifier=identifier), 0, "AXButton", "")
    assert el.label == name


def test_long_list_walks_only_visible_rows_and_keeps_the_total(fake_ax):
    rows = [Node("AXRow", children=[Node("AXStaticText", AXValue=f"f{i}")]) for i in range(2000)]
    outline = Node("AXOutline", children=rows, AXVisibleRows=rows[:20], AXRows=rows)
    out = walk(Node("AXWindow", "w", [outline]))
    assert sum(e.role == "AXStaticText" for e in out) == 20
    (container,) = [e for e in out if e.role == "AXOutline"]
    assert container.row_count == 2000 and not container.interactive
    assert UIElement.from_dict(container.to_dict()).row_count == 2000


def test_visible_children_used_for_lists_and_all_children_without_visible_attrs(fake_ax):
    items = [Node("AXStaticText", AXValue=f"i{i}") for i in range(50)]
    lst = Node("AXList", children=items, AXVisibleChildren=items[:5])
    assert sum(e.role == "AXStaticText" for e in walk(Node("AXWindow", "w", [lst]))) == 5
    plain = Node("AXOutline", children=items)
    out = walk(Node("AXWindow", "w", [plain]))
    assert sum(e.role == "AXStaticText" for e in out) == 50 and all(e.row_count == 0 for e in out)


def test_render_counts_unwalked_rows():
    from local_agent.observe.models import Snapshot
    from local_agent.observe.screen_view import render

    els = [UIElement(id=0, role="AXOutline", interactive=False, row_count=2000, path="AXWindow:w > AXOutline:Files")]
    els += [UIElement(id=i, role="AXStaticText", value=f"f{i}", interactive=False, path="AXWindow:w > AXOutline:Files")
            for i in range(1, 21)]  # fmt: skip
    text = render(Snapshot(app="A", bundle_id="b", pid=1, window_title="w", elements=els), 3000)
    assert "1988 more not shown"  # 8 walked rows past the 12 shown + 1,980 never walked in text


def test_type_replaces_single_line_fields_and_appends_to_areas(monkeypatch):
    pytest.importorskip("ApplicationServices")
    from local_agent.act import executor

    store = {"v": "Untitled"}

    def get(ref, name):
        return store["v"] if name == "AXValue" else None

    def set_(ref, name, value):
        store["v"] = value if name == "AXValue" else store["v"] + value  # AXSelectedText inserts at the cursor (end)
        return True

    monkeypatch.setattr(executor, "_ax_get", get)
    monkeypatch.setattr(executor, "_ax_set", set_)
    field = UIElement(id=1, role="AXTextField", ref=object())
    assert executor.type_text_into(field, "helloworld.txt") == 'set field to "helloworld.txt" via AX'
    assert store["v"] == "helloworld.txt"
    store["v"] = "abc"
    executor.type_text_into(UIElement(id=2, role="AXTextArea", ref=object()), "def")
    assert store["v"] == "abcdef"
    # AX refuses the value: focus, select all, then keystrokes.
    calls = []
    monkeypatch.setattr(executor, "_ax_set", lambda *a: False)
    monkeypatch.setattr(executor, "_raise_window", lambda el: calls.append("raise"))
    monkeypatch.setattr(executor, "_focus", lambda el: calls.append("focus"))
    monkeypatch.setattr(executor, "press_shortcut", lambda k: calls.append(k))
    monkeypatch.setattr(executor, "type_text", lambda t: calls.append(("keys", t)))
    executor.type_text_into(field, "x")
    assert calls == ["raise", "focus", "cmd+a", ("keys", "x")]


def test_sheet_snapshot_uses_the_parent_window_and_lists_the_sheet_first(fake_ax):
    from local_agent.observe.models import Snapshot
    from local_agent.observe.screen_view import render

    name = Node("AXTextField", AXValue="Untitled")
    sheet = Node("AXSheet", children=[name, Node("AXButton", "Save")])
    area = Node("AXTextArea", AXValue="Hello world")
    window = Node("AXWindow", "Untitled", [area, sheet])
    sheet.attrs["AXParent"] = window
    assert ax_tree.document_window(sheet) is window
    assert ax_tree.document_window(window) is window
    out = walk(ax_tree.document_window(sheet))
    assert any(e.role == "AXTextArea" and e.value == "Hello world" for e in out)
    view = render(Snapshot(app="A", bundle_id="b", pid=1, window_title="Untitled", elements=out), 3000)
    assert view.index("Dialog:") < view.index("Window")
