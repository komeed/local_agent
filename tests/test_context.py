from local_agent.observe.context import add_context
from local_agent.observe.models import Frame, UIElement


def el(i, role, x, y, w=80, h=20, path="", **kw):
    return UIElement(id=i, role=role, frame=Frame(x, y, w, h), path=path,
                     interactive=role not in ("AXStaticText", "AXHeading"), **kw)  # fmt: skip


def test_button_takes_row_name():
    els = [el(0, "AXRow", 0, 0, 500, 30, label="invoice-0425.pdf"), el(1, "AXButton", 400, 5, title="Delete"),
           el(2, "AXRow", 0, 30, 500, 30, label="invoice-0426.pdf"), el(3, "AXButton", 400, 35, title="Delete")]  # fmt: skip
    add_context(els)
    assert els[1].context == 'row "invoice-0425.pdf"' and els[3].context == 'row "invoice-0426.pdf"'
    assert els[0].context == ""  # a row isn't its own context


def test_unnamed_row_named_by_its_cells():
    els = [el(0, "AXRow", 0, 0, 500, 30), el(1, "AXStaticText", 10, 5, value="report.pdf"),
           el(2, "AXStaticText", 200, 5, value="12 KB"), el(3, "AXButton", 400, 5, title="Share")]  # fmt: skip
    add_context(els)
    assert els[3].context == 'row "report.pdf 12 KB"'


def test_smallest_row_wins():
    els = [el(0, "AXRow", 0, 0, 600, 200, label="Folder"), el(1, "AXRow", 20, 40, 560, 30, label="child.txt"),
           el(2, "AXButton", 500, 45, title="Delete")]  # fmt: skip
    add_context(els)
    assert els[2].context == 'row "child.txt"'


def test_element_named_like_row_gets_no_repeat():
    els = [el(0, "AXRow", 0, 0, 500, 30, label="Inbox"), el(1, "AXCell", 10, 5, label="Inbox")]
    add_context(els)
    assert els[1].context == ""


def test_label_left_beats_heading_above():
    els = [el(0, "AXHeading", 100, 0, 200, value="Billing address"), el(1, "AXStaticText", 100, 33, 80, 16, value="City"),
           el(2, "AXTextField", 200, 30, 250, 22)]  # fmt: skip
    add_context(els)
    assert els[2].context == 'label "City"'


def test_label_above_and_named_fields_untouched():
    els = [el(0, "AXStaticText", 100, 0, 120, 16, value="Confirm email"), el(1, "AXTextField", 100, 20, 300, 22),
           el(2, "AXStaticText", 100, 60, 80, 16, value="Note"), el(3, "AXTextField", 100, 80, 300, 22, title="Memo")]  # fmt: skip
    add_context(els)
    assert els[1].context == 'label "Confirm email"'
    assert els[3].context == ""


def test_far_text_is_not_a_label():
    els = [el(0, "AXStaticText", 0, 0, 50, 16, value="Far"), el(1, "AXTextField", 500, 0, 200, 22)]
    add_context(els)
    assert els[1].context == ""
