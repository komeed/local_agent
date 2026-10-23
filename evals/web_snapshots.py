"""Snapshots of web pages for the eval cases: the captured outline (evals/fixtures/web/*.json, from
evals/capture_web_fixtures.py) merged into a browser's AX chrome the way ax_tree.snapshot does it."""

from __future__ import annotations

import json
from pathlib import Path

from local_agent.observe.models import Frame, Snapshot, UIElement
from local_agent.observe.web import outline_to_elements

DIR = Path(__file__).parent / "fixtures" / "web"
CHROME = "AXWindow:Arc > AXToolbar"


def web_names() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.json"))


def outline(name: str) -> dict:
    return json.loads((DIR / f"{name}.json").read_text())


def web_snapshot(name: str, offset: float = 90.0) -> Snapshot:
    """The page `name` under a browser toolbar (address field, back/reload). The web area starts `offset` points down."""
    o = outline(name)
    chrome = [
        UIElement(id=0, role="AXButton", label="Back", path=CHROME, frame=Frame(70, 20, 24, 24)),
        UIElement(id=1, role="AXButton", label="Reload", path=CHROME, frame=Frame(100, 20, 24, 24)),
        UIElement(id=2, role="AXTextField", title="Address", value=o["url"], path=CHROME, frame=Frame(300, 20, 600, 24)),
    ]
    area = Frame(0, offset, o["inner_width"], o["inner_height"])
    elements = chrome + outline_to_elements(o, area, len(chrome))
    return Snapshot(app="Arc", bundle_id="company.thebrowser.Browser", pid=1, window_title=o["title"], elements=elements,
                    url=o["url"], page_title=o["title"], web={"ms": 0.0, "candidates": o["candidates"],
                                                              "listed": o["listed"], "more": o["more"]})  # fmt: skip


def dashboard_snapshot(url: str = "https://learn.example.org/dashboard", rows: int = 40) -> Snapshot:
    """Arc in front on an unrelated, generic dashboard read through AX (page paths under AXWebArea): a sidebar of
    tabs, a toolbar, and a big page of rows with links and buttons."""
    chrome = "AXWindow:Course dashboard > AXToolbar"
    page = "AXWindow:Course dashboard > AXGroup > AXWebArea:Course dashboard"
    els: list[UIElement] = []

    def add(role, path, y, x=200, **kw):
        els.append(UIElement(id=len(els), role=role, path=path, frame=Frame(x, y, 120, 20),
                             interactive=role not in ("AXStaticText", "AXHeading"), **kw))  # fmt: skip

    for i, t in enumerate(["Inbox", "Calendar", "Notes", "Docs"]):
        add("AXButton", "AXWindow:Course dashboard > AXGroup", 100 + 30 * i, 20, title=t)
    add("AXButton", chrome, 20, 70, label="Back")
    add("AXTextField", chrome, 20, 300, title="Address", value=url)
    add("AXHeading", page, 140, value="Course dashboard")
    for i in range(rows):
        y = 180 + 24 * i
        add("AXStaticText", page, y, value=f"Student {i + 1} — module {i % 7 + 1} progress")
        add("AXLink", page, y, 400, title=f"Open record {i + 1}")
        add("AXButton", page, y, 560, title="Message")
    menubar = [UIElement(id=len(els) + i, role="AXMenuBarItem", title=t, path="AXMenuBar", frame=Frame(40 + 50 * i, 0, 40, 22))
               for i, t in enumerate(["Arc", "File", "Edit", "View"])]
    return Snapshot(app="Arc", bundle_id="company.thebrowser.Browser", pid=1, window_title="Course dashboard",
                    elements=els + menubar, url=url, page_title="Course dashboard")
