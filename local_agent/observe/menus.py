"""The front app's menu bar as a list of commands: what the app can do, with the shortcut for each.

Every Mac app lists its commands in the menu bar. Closed menus still expose their items over accessibility (with 0x0
frames, which the snapshot ignores), so the planner can pick "File > Save As…" without opening anything. Code then
presses the command's shortcut, or AXPresses the item."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from local_agent.act.keys import KEYCODES

MAX_DEPTH = 2  # submenus below a top-level menu
TIME_CAP_S = 0.5
# AXMenuItemCmdModifiers bits (Apple's kMenu*Modifier values); 0 means cmd alone.
SHIFT, OPTION, CONTROL, NO_CMD = 1, 2, 4, 8
KEY_NAMES: dict[int, str] = {}
for _name, _code in KEYCODES.items():
    KEY_NAMES.setdefault(_code, _name)


@dataclass
class MenuCommand:
    path: str  # "File > Save As…"
    keys: str | None = None  # "cmd+shift+s"
    enabled: bool = True
    ref: Any = field(default=None, repr=False, compare=False)  # the AXMenuItem
    bar_ref: Any = field(default=None, repr=False, compare=False)  # its top-level AXMenuBarItem


def decode_keys(char: str | None, modifiers: int | None, virtual_key: int | None) -> str | None:
    """'cmd+shift+s' from AXMenuItemCmdChar / CmdModifiers / CmdVirtualKey; None when the item has no shortcut."""
    key = (char or "").strip().lower()
    if not key and virtual_key is not None:
        key = KEY_NAMES.get(int(virtual_key), "")
    if not key:
        return None
    mods = int(modifiers or 0)
    parts = ["cmd"] if not mods & NO_CMD else []
    parts += [name for bit, name in ((CONTROL, "ctrl"), (OPTION, "option"), (SHIFT, "shift")) if mods & bit]
    return "+".join([*parts, key])


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def read_commands(menubar, attr: Callable[[Any, str], Any] | None = None, cap_s: float = TIME_CAP_S) -> list[MenuCommand]:
    """Every leaf item of the menu bar (submenus to MAX_DEPTH), enabled or not, in menu order. `attr(el, name)` reads
    an AX attribute (injectable for tests); the walk stops at the time cap and returns what it has."""
    if attr is None:
        from local_agent.observe.ax_tree import _attr as attr
    deadline = time.monotonic() + cap_s
    out: list[MenuCommand] = []

    def items_of(menu_holder) -> list:
        for child in attr(menu_holder, "AXChildren") or []:
            if _text(attr(child, "AXRole")) == "AXMenu":
                return list(attr(child, "AXChildren") or [])
        return []

    def walk(holder, prefix: str, depth: int, bar) -> None:
        for item in items_of(holder):
            if time.monotonic() > deadline:
                return
            title = _text(attr(item, "AXTitle"))
            if not title:  # separator
                continue
            path = f"{prefix} > {title}" if prefix else title
            sub = items_of(item)
            if sub:
                if depth < MAX_DEPTH:
                    walk(item, path, depth + 1, bar)
                continue
            enabled = attr(item, "AXEnabled")
            out.append(MenuCommand(path, decode_keys(attr(item, "AXMenuItemCmdChar"), attr(item, "AXMenuItemCmdModifiers"),
                                                    attr(item, "AXMenuItemCmdVirtualKey")),
                                   True if enabled is None else bool(enabled), item, bar))  # fmt: skip

    for bar_item in attr(menubar, "AXChildren") or []:
        if time.monotonic() > deadline:
            break
        title = _text(attr(bar_item, "AXTitle"))
        if title:
            walk(bar_item, title, 1, bar_item)
    return out


def _norm(path: str) -> str:
    path = re.sub(r"\s*\[[^\[\]]*\]\s*$", "", path)  # the planner may echo the "[cmd+s]" of the scope line
    return " > ".join(re.sub(r"[\s…]+|\.{3}", " ", p).strip().lower() for p in path.split(">"))


def find_exact(path: str, commands: list[MenuCommand]) -> MenuCommand | None:
    """The one enabled command named `path` ("File > Save As…", with or without the ellipsis or the menu name when
    the item name alone is unique), else None."""
    want = _norm(path)
    enabled = [c for c in commands if c.enabled]
    full = [c for c in enabled if _norm(c.path) == want]
    if len(full) == 1:
        return full[0]
    leaf = [c for c in enabled if _norm(c.path).rsplit(" > ", 1)[-1] == want.rsplit(" > ", 1)[-1] and ">" not in want]
    return leaf[0] if len(leaf) == 1 else None


def menu_scope(commands: list[MenuCommand], goal: str = "", limit: int = 120) -> list[str]:
    """The enabled commands as short lines for the planner: "File > Save As… [cmd+shift+s]". Over `limit` commands,
    only the ones that match the goal's words (prefilter), the menu names, and shortcut commands within the limit."""
    from local_agent.observe.models import UIElement
    from local_agent.selector.prefilter import rank

    enabled = [c for c in commands if c.enabled]

    def line(c: MenuCommand) -> str:
        return f"{c.path} [{c.keys}]" if c.keys else c.path

    if len(enabled) <= limit:
        return [line(c) for c in enabled]
    menus = list(dict.fromkeys(c.path.split(" > ")[0] for c in enabled))
    pseudo = [UIElement(id=i, role="AXMenuItem", title=c.path) for i, c in enumerate(enabled)]
    keep = {e.id for e in rank(goal, pseudo, max(limit - len(menus), 1)).kept}
    lines = [f"{m} [menu]" for m in menus] + [line(c) for i, c in enumerate(enabled) if i in keep]
    return lines[:limit]
