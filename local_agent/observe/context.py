"""Surrounding text for elements whose own text doesn't say which one they are: the row a button sits in (every row of
a list has its own "Delete") and the visible label next to an unnamed form field. Works on frames only, so it applies
to any app and to synthetic eval snapshots alike."""

from __future__ import annotations

from bisect import bisect_right

from local_agent.observe.models import Frame, UIElement

ROW_ROLES = {"AXRow"}
DISPLAY_TEXT_ROLES = {"AXStaticText", "AXHeading"}
# Controls that usually get their name from a separate label element rather than AXTitle/AXDescription.
LABELED_ROLES = {
    "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXSecureTextField", "AXPopUpButton", "AXSlider",
    "AXDateField",
}  # fmt: skip
MAX_NAME = 80
LABEL_LEFT_PX = 300  # a label on the same line, left of the field
LABEL_ABOVE_PX = 40  # a label just above the field
OTHER_PATH_PENALTY = 50  # prefer labels in the same container as the field


def _contains(f: Frame, x: float, y: float) -> bool:
    return f.x <= x <= f.x + f.w and f.y <= y <= f.y + f.h


def _quote(text: str) -> str:
    text = text.replace('"', "'")
    return text if len(text) <= MAX_NAME else text[: MAX_NAME - 1] + "…"


def _row_names(rows: list[UIElement], texts: list[UIElement]) -> dict[int, str]:
    """Row id -> name: its own name, else the first visible texts inside its frame (cells of a table)."""
    names = {}
    for r in rows:
        name = r.name()
        if not name:
            inside = [t.value for t in texts if _contains(r.frame, *t.frame.center)]
            name = " ".join(inside[:3])
        if name:
            names[r.id] = _quote(name)
    return names


def _row_of(e: UIElement, rows: list[UIElement], starts: list[float], tallest: float) -> UIElement | None:
    """Smallest row whose frame holds e's center. Rows are sorted by top edge; only those starting within `tallest`
    above the center can contain it."""
    cx, cy = e.frame.center
    best = None
    i = bisect_right(starts, cy) - 1
    while i >= 0 and starts[i] >= cy - tallest:
        r = rows[i]
        if r.id != e.id and _contains(r.frame, cx, cy) and (best is None or r.frame.w * r.frame.h < best.frame.w * best.frame.h):
            best = r
        i -= 1
    return best


def _label_of(e: UIElement, texts: list[UIElement]) -> str:
    """Nearest visible text on the same line to the left of e, else just above it (a left label beats a section
    heading above)."""
    f = e.frame
    best = {"left": ("", float("inf")), "above": ("", float("inf"))}
    for t in texts:
        g = t.frame
        ty = g.center[1]
        if f.y <= ty <= f.y + f.h and g.x + g.w <= f.x + 4 and f.x - (g.x + g.w) <= LABEL_LEFT_PX:
            side, d = "left", max(0.0, f.x - (g.x + g.w))
        elif g.y + g.h <= f.y + 4 and f.y - (g.y + g.h) <= LABEL_ABOVE_PX and g.x < f.x + f.w and g.x + g.w > f.x:
            side, d = "above", max(0.0, f.y - (g.y + g.h))
        else:
            continue
        d += 0 if t.path == e.path else OTHER_PATH_PENALTY
        if d < best[side][1]:
            best[side] = (t.value, d)
    label = best["left"][0] or best["above"][0]
    return _quote(label) if label else ""


def add_context(elements: list[UIElement]) -> list[UIElement]:
    """Set `context` on interactive elements (in place) from their row and nearby label; returns `elements`."""
    framed = [e for e in elements if e.frame is not None]
    texts = [e for e in framed if e.role in DISPLAY_TEXT_ROLES and e.value]
    rows = sorted((e for e in framed if e.role in ROW_ROLES), key=lambda r: r.frame.y)
    starts = [r.frame.y for r in rows]
    tallest = max((r.frame.h for r in rows), default=0.0)
    row_names = _row_names(rows, texts)
    for e in framed:
        if not e.interactive:
            continue
        parts = []
        if e.role not in ROW_ROLES and rows:
            row = _row_of(e, rows, starts, tallest)
            name = row_names.get(row.id, "") if row else ""
            if name and name != e.name():
                parts.append(f'row "{name}"')
        if e.role in LABELED_ROLES and not (e.title or e.label):
            label = _label_of(e, texts)
            if label:
                parts.append(f'label "{label}"')
        e.context = " ".join(parts)
    return elements
