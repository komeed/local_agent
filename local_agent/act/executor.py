"""Perform planner steps on the real desktop via AX actions and Quartz CGEvents."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from ApplicationServices import AXUIElementPerformAction, AXUIElementSetAttributeValue
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventRightMouseDown,
    kCGEventRightMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
    kCGMouseEventClickState,
    kCGScrollEventUnitLine,
)

from local_agent.act.keys import parse_shortcut
from local_agent.observe.models import UIElement
from local_agent.planner.schema import PlannerOutput

# Roles where AXPress is the native, occlusion-proof way to click.
AXPRESS_ROLES = {
    "AXButton", "AXMenuItem", "AXMenuBarItem", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton",
    "AXDisclosureTriangle", "AXLink",
}  # fmt: skip
SCROLL_LINES = 5


class ActionError(RuntimeError):
    pass


@dataclass
class ActionResult:
    ok: bool
    note: str = ""


def _post_mouse(kind, point, button, clicks: int = 1) -> None:
    ev = CGEventCreateMouseEvent(None, kind, point, button)
    if clicks > 1:
        CGEventSetIntegerValueField(ev, kCGMouseEventClickState, clicks)
    CGEventPost(kCGHIDEventTap, ev)


def click_at(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    down, up, btn = (
        (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight)
        if button == "right"
        else (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft)
    )
    point = (x, y)
    _post_mouse(kCGEventMouseMoved, point, btn)
    time.sleep(0.03)
    for n in range(1, clicks + 1):
        _post_mouse(down, point, btn, n)
        _post_mouse(up, point, btn, n)
        time.sleep(0.05)


def type_text(text: str) -> None:
    # Unicode events handle any character regardless of keyboard layout; send in small chunks.
    for i in range(0, len(text), 16):
        part = text[i : i + 16]
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, 0, down)
            CGEventKeyboardSetUnicodeString(ev, len(part), part)
            CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.02)


def press_shortcut(keys: str) -> None:
    code, flags = parse_shortcut(keys)
    for down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, code, down)
        CGEventSetFlags(ev, flags)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.02)


def scroll(direction: str, at: tuple[float, float] | None) -> None:
    if at is not None:
        _post_mouse(kCGEventMouseMoved, at, kCGMouseButtonLeft)
    dy = {"up": SCROLL_LINES, "down": -SCROLL_LINES}.get(direction, 0)
    dx = {"left": SCROLL_LINES, "right": -SCROLL_LINES}.get(direction, 0)
    CGEventPost(kCGHIDEventTap, CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2, dy, dx))


def run_terminal(command: str, timeout: float = 120.0) -> ActionResult:
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ActionResult(False, f"timed out after {timeout:.0f}s")
    out = (proc.stdout + proc.stderr).strip()
    tail = out[-600:] if out else "(no output)"
    return ActionResult(proc.returncode == 0, f"exit={proc.returncode} output: {tail}")


def _click_element(el: UIElement, button: str = "left", clicks: int = 1) -> str:
    if button == "left" and clicks == 1 and el.ref is not None and el.role in AXPRESS_ROLES and "AXPress" in el.actions:
        if AXUIElementPerformAction(el.ref, "AXPress") == 0:
            return "AXPress"
    if el.frame is None:
        raise ActionError(f"Element has no on-screen frame: {el.describe()}")
    click_at(*el.frame.center, button=button, clicks=clicks)
    return f"{button} click x{clicks} at {tuple(int(c) for c in el.frame.center)}"


def _focus(el: UIElement) -> None:
    if el.ref is not None and AXUIElementSetAttributeValue(el.ref, "AXFocused", True) == 0:
        return
    if el.frame is None:
        raise ActionError(f"Cannot focus element without frame: {el.describe()}")
    click_at(*el.frame.center)


def execute(step: PlannerOutput, element: UIElement | None) -> ActionResult:
    """Run one non-terminal step. `element` is the Jev-selected element (None when the step has no target)."""
    a = step.action
    try:
        if a in ("click", "double_click", "right_click"):
            assert element is not None
            how = _click_element(
                element, button="right" if a == "right_click" else "left", clicks=2 if a == "double_click" else 1
            )
            return ActionResult(True, how)
        if a == "type":
            if element is not None:
                _focus(element)
                time.sleep(0.15)
            type_text(step.text or "")
            return ActionResult(True, f"typed {len(step.text or '')} chars")
        if a == "shortcut":
            press_shortcut(step.keys or "")
            return ActionResult(True, f"pressed {step.keys}")
        if a == "scroll":
            scroll(step.direction or "down", element.frame.center if element and element.frame else None)
            return ActionResult(True, f"scrolled {step.direction}")
    except (ActionError, ValueError) as e:
        return ActionResult(False, str(e))
    return ActionResult(False, f"unsupported action {a!r}")
