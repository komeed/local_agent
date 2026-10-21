"""Plain data types for a UI snapshot. Kept free of PyObjC so planner/selector logic is testable anywhere."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any


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
    enabled: bool = True
    focused: bool = False
    frame: Frame | None = None
    actions: list[str] = field(default_factory=list)
    # Ancestor roles/titles, e.g. "AXWindow:Inbox > AXGroup > AXToolbar".
    path: str = ""
    # Live AXUIElementRef; never serialized.
    ref: Any = field(default=None, repr=False, compare=False)

    def name(self) -> str:
        return self.title or self.label or self.placeholder or self.help or self.value

    def describe(self, max_len: int = 160) -> str:
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
        if self.help and not (self.title or self.label):
            parts.append(f'help="{self.help[:40]}"')
        if not self.enabled:
            parts.append("disabled")
        if self.focused:
            parts.append("focused")
        if self.path:
            parts.append(f"in {self.path.split(' > ')[-1]}")
        if self.frame:
            parts.append(f"at ({int(self.frame.x)},{int(self.frame.y)})")
        text = " ".join(parts)
        return text if len(text) <= max_len else text[: max_len - 1] + "…"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("ref", None)
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

    def scope(self) -> dict:
        """Compact summary given to the planner instead of the full element list."""
        counts = Counter(e.role for e in self.elements)
        focused = next((e for e in self.elements if e.focused), None)
        return {
            "app": self.app,
            "window": self.window_title,
            "element_counts_by_role": dict(counts.most_common()),
            "focused_element": focused.describe() if focused else None,
        }

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "bundle_id": self.bundle_id,
            "pid": self.pid,
            "window_title": self.window_title,
            "elements": [e.to_dict() for e in self.elements],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        return cls(
            app=d["app"],
            bundle_id=d.get("bundle_id", ""),
            pid=d.get("pid", 0),
            window_title=d.get("window_title", ""),
            elements=[UIElement.from_dict(e) for e in d["elements"]],
        )
