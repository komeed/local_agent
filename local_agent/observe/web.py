"""Read the page in a browser's front tab as UIElements (Phase 1 of docs/PLAN_browser.md).

`web_outline.js` is the one fixed script that runs in the page; `outline_to_elements` is pure Python so the mapping to
AX-style roles, screen frames and paths is tested without a browser.
"""

from __future__ import annotations

import time
from functools import cache
from pathlib import Path

from local_agent.observe.models import Frame, UIElement

MAX_ELEMENTS = 400
ROLE_MAP = {
    "link": "AXLink", "button": "AXButton", "textbox": "AXTextField", "searchbox": "AXSearchField",
    "password": "AXSecureTextField", "textarea": "AXTextArea", "checkbox": "AXCheckBox", "switch": "AXCheckBox",
    "radio": "AXRadioButton", "tab": "AXTab", "menuitem": "AXMenuItem", "option": "AXMenuItem", "slider": "AXSlider",
    "heading": "AXHeading", "text": "AXStaticText",
}  # fmt: skip


@cache
def outline_js() -> str:
    return (Path(__file__).with_name("web_outline.js")).read_text()


def read_outline(bundle_id: str, max_elements: int = MAX_ELEMENTS) -> dict:
    """Run the outline script in the front tab; raises browser.BrowserError."""
    from local_agent.act import browser

    out = browser.run_js(bundle_id, outline_js(), {"max": max_elements})
    if not isinstance(out, dict):
        raise browser.BrowserError("other", "page outline returned nothing")
    return out


def _role(item: dict) -> str:
    if item.get("role") == "combobox":
        return "AXPopUpButton" if item.get("tag") == "select" else "AXComboBox"
    return ROLE_MAP.get(item.get("role", ""), "AXGroup")


def outline_to_elements(outline: dict, web_area: Frame | None, start_id: int = 0) -> list[UIElement]:
    """UIElements for an outline. Page points map to screen points through the AXWebArea frame and the page's
    innerWidth, so zoom and the browser's own offset (Arc's sidebar, toolbars) are covered. Ends with a non-interactive
    placeholder carrying the number of controls beyond the viewport (like `row_count` for long lists)."""
    iw = outline.get("inner_width") or 0
    scale = web_area.w / iw if web_area and iw else 1.0
    ox, oy = (web_area.x, web_area.y) if web_area else (0.0, 0.0)
    out: list[UIElement] = []
    for item in outline.get("elements", []):
        role = _role(item)
        x, y, w, h = item.get("rect") or (0, 0, 0, 0)
        frame = Frame(ox + x * scale, oy + y * scale, w * scale, h * scale)
        path = "AXWebArea" + (f" > {item['path']}" if item.get("path") else "")
        interactive = bool(item.get("interactive", True))
        if not interactive:
            out.append(UIElement(id=start_id + len(out), role=role, value=item.get("value", ""), frame=frame, path=path,
                                 interactive=False))
            continue
        value = item.get("value", "")
        if role in ("AXCheckBox", "AXRadioButton") and item.get("checked") is not None:
            value = "1" if item["checked"] else "0"
        if role == "AXSecureTextField":
            value = ""  # the script never sends it; belt and braces
        out.append(UIElement(
            id=start_id + len(out), role=role, title=item.get("name", ""), value=value,
            placeholder=item.get("placeholder", ""), enabled=bool(item.get("enabled", True)),
            focused=bool(item.get("focused", False)), frame=frame, path=path, context=item.get("context", ""),
            web_id=item.get("web_id", ""), href=item.get("href", ""), required=bool(item.get("required")),
            hint=item.get("hint", ""), is_default=bool(item.get("submit")),
        ))  # fmt: skip
    more = int(outline.get("more") or 0)
    if more:
        out.append(UIElement(id=start_id + len(out), role="AXWebArea", interactive=False, row_count=more,
                             path="AXWebArea"))  # fmt: skip
    return out


def timed_outline(bundle_id: str, web_area: Frame | None, start_id: int, max_elements: int = MAX_ELEMENTS):
    """(elements, info) where info = {ms, url, title, candidates, listed, more}."""
    t0 = time.perf_counter()
    outline = read_outline(bundle_id, max_elements)
    ms = (time.perf_counter() - t0) * 1000
    info = {"ms": round(ms, 1), "url": outline.get("url", ""), "title": outline.get("title", ""),
            "candidates": outline.get("candidates", 0), "listed": outline.get("listed", 0),
            "more": outline.get("more", 0)}  # fmt: skip
    return outline_to_elements(outline, web_area, start_id), info
