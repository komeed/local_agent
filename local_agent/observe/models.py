"""Plain data types for a UI snapshot. Kept free of PyObjC so planner/selector logic is testable anywhere."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from typing import Any


MAX_SCOPE_MENU_ITEMS = 40


# Fields whose whole value one typed string replaces (a text area, by contrast, takes text at the cursor).
SINGLE_LINE_ROLES = {"AXTextField", "AXSearchField", "AXComboBox", "AXSecureTextField"}


@dataclass
class Frame:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)


@dataclass
class UIElement:
    id: int
    role: str
    subrole: str = ""
    title: str = ""
    label: str = ""  # AXDescription
    value: str = ""
    placeholder: str = ""
    help: str = ""
    selected: str = ""  # AXSelectedText, for text roles
    enabled: bool = True
    focused: bool = False
    frame: Frame | None = None
    actions: list[str] = field(default_factory=list)
    # Ancestor roles/titles, e.g. "AXWindow:Inbox > AXGroup > AXToolbar".
    path: str = ""
    # False for visible text (AXStaticText/AXHeading) kept only for the planner's screen view.
    interactive: bool = True
    # AXMenuItem that opens a submenu (e.g. Format > Font).
    submenu: bool = False
    # Surrounding text the path doesn't carry (observe/context.py): 'row "invoice-0425.pdf"', 'label "City"'.
    context: str = ""
    # The button its sheet/window/dialog reports as AXDefaultButton (the one Return presses).
    is_default: bool = False
    # Rows in a long list container (AXTable, AXOutline...) of which only the visible ones were walked; 0 otherwise.
    row_count: int = 0
    # data-la-id of the page element (observe/web.py); "" for native controls.
    web_id: str = ""
    # Where a page link points (observe/web.py); "" for everything else.
    href: str = ""
    # Form fields (web): required / aria-required, and autocomplete + name + type words ("cc-number", "email", "submit").
    required: bool = False
    hint: str = ""
    # Live AXUIElementRef; never serialized.
    ref: Any = field(default=None, repr=False, compare=False)

    def name(self) -> str:
        return self.title or self.label or self.placeholder or self.help or self.value

    def containers(self) -> list[str]:
        """Titles of enclosing containers, nearest first, without the window (every element shares it)."""
        out = []
        for seg in reversed(self.path.split(" > ")):
            role, _, title = seg.partition(":")
            if title and role != "AXWindow":
                out.append(title)
        return out

    def surroundings(self) -> str:
        """Text around the element that can identify it: its row, form label and named containers."""
        # Only the quoted names from `context`, not its 'row'/'label' markers (every row's button would match "row").
        return " ".join([*re.findall(r'"([^"]*)"', self.context), *self.containers()])

    def describe(self, max_len: int = 200) -> str:
        """One-line description used as a Jev choice option."""
        parts = [self.role + (f"/{self.subrole}" if self.subrole else "")]
        if self.title:
            parts.append(f'title="{self.title}"')
        if self.label and self.label != self.title:
            parts.append(f'label="{self.label}"')
        if self.placeholder:
            parts.append(f'placeholder="{self.placeholder}"')
        if self.value and self.value not in (self.title, self.label):
            parts.append(f'value="{self.value[:40]}"')
        if self.selected:
            parts.append(f'selected="{self.selected[:30]}"')
        if self.help and not (self.title or self.label):
            parts.append(f'help="{self.help[:40]}"')
        if not self.enabled:
            parts.append("disabled")
        if self.focused:
            parts.append("focused")
        if self.context:
            parts.append(self.context)
        if self.path:
            # The nearest named container says more than the last segment (often a bare "AXGroup").
            segs = self.path.split(" > ")
            role, _, title = next((s for s in reversed(segs) if ":" in s), segs[-1]).partition(":")
            parts.append(f'in {role.removeprefix("AX")} "{title}"' if title else f"in {segs[-1]}")
        if self.frame:
            parts.append(f"at ({int(self.frame.x)},{int(self.frame.y)})")
        text = " ".join(parts)
        return text if len(text) <= max_len else text[: max_len - 1] + "…"

    def to_dict(self) -> dict:
        # Not asdict(): it deep-copies `ref`, and AXUIElementRefs can't be copied.
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "ref"}
        d["actions"] = list(self.actions)
        if self.frame is not None:
            d["frame"] = asdict(self.frame)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "UIElement":
        d = dict(d)
        if d.get("frame"):
            d["frame"] = Frame(**d["frame"])
        d.pop("ref", None)
        return cls(**d)


@dataclass
class Snapshot:
    app: str
    bundle_id: str
    pid: int
    window_title: str
    elements: list[UIElement]
    # File the front window's document is saved as (AXDocument, decoded); "" when the app doesn't report one.
    document_path: str = ""
    # The app's menu commands (observe/menus.py); live refs, so never serialized.
    menu_commands: list = field(default_factory=list, repr=False, compare=False)
    # The front tab, when the page was read through the browser (observe/web.py); "" otherwise.
    url: str = ""
    page_title: str = ""
    # {ms, candidates, listed, more} of that read; empty when the snapshot is AX-only.
    web: dict = field(default_factory=dict)

    def scope(self) -> dict:
        """Compact summary given to the planner instead of the full element list."""
        counts = Counter(e.role for e in self.elements if e.interactive)
        focused = next((e for e in self.elements if e.focused), None)
        scope = {
            "app": self.app,
            "window": self.window_title,
            "element_counts_by_role": dict(counts.most_common()),
            "focused_element": focused.describe() if focused else None,
        }
        # Open menus are short and the planner can't navigate them blind (e.g. Bold lives under Format > Font).
        menu_items = [e.name() for e in self.elements if e.role == "AXMenuItem" and e.name()]
        if menu_items:
            scope["open_menu_items"] = menu_items[:MAX_SCOPE_MENU_ITEMS]
        return scope

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "bundle_id": self.bundle_id,
            "pid": self.pid,
            "window_title": self.window_title,
            "elements": [e.to_dict() for e in self.elements],
            "document_path": self.document_path,
            "url": self.url,
            "page_title": self.page_title,
            "web": dict(self.web),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        return cls(
            app=d["app"],
            bundle_id=d.get("bundle_id", ""),
            pid=d.get("pid", 0),
            window_title=d.get("window_title", ""),
            elements=[UIElement.from_dict(e) for e in d["elements"]],
            document_path=d.get("document_path", ""),
            url=d.get("url", ""),
            page_title=d.get("page_title", ""),
            web=dict(d.get("web") or {}),
        )


def path_from_file_url(url: str) -> str:
    """'file:///Users/a/My%20Doc.txt' -> '/Users/a/My Doc.txt'; "" for anything that isn't a file URL."""
    from urllib.parse import unquote, urlparse

    u = urlparse(url or "")
    return unquote(u.path) if u.scheme == "file" else ""
