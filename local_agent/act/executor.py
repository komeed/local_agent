"""Perform planner steps on the real desktop via AX actions and Quartz CGEvents."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from ApplicationServices import AXUIElementCopyAttributeValue, AXUIElementPerformAction, AXUIElementSetAttributeValue
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

from local_agent.act import browser
from local_agent.act.keys import parse_shortcut
from local_agent.apps import installed_apps, opened_app, rewrite_open
from local_agent.observe.models import SINGLE_LINE_ROLES, UIElement
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
    """Run a shell command. `open -a <App>` gets the app name resolved to an installed app ("Arc Browser" -> Arc)
    and the opened app (or, for `open <url>`, the browser) brought to the front, which `open` alone can't do on
    recent macOS when it's started from a background process."""
    from local_agent.observe.ax_tree import focus_after_open

    command, resolved = rewrite_open(command, installed_apps())
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ActionResult(False, f"timed out after {timeout:.0f}s")
    out = (proc.stdout + proc.stderr).strip()
    tail = out[-600:] if out else "(no output)"
    note = f"exit={proc.returncode} output: {tail}"
    if resolved:
        note = f"{resolved}; {note}"
    if proc.returncode == 0:
        focused = focus_after_open(command)
        if focused:
            note += f"; {focused}"
    return ActionResult(proc.returncode == 0, note)


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


def _ax_get(ref, name: str):
    err, value = AXUIElementCopyAttributeValue(ref, name, None)
    return value if err == 0 else None


def _ax_set(ref, name: str, value) -> bool:
    return AXUIElementSetAttributeValue(ref, name, value) == 0


def _raise_window(el: UIElement) -> None:
    """Bring the element's window to the front and make it the app's main window, so keystrokes land in it."""
    window = _ax_get(el.ref, "AXWindow")
    if window is not None:
        AXUIElementPerformAction(window, "AXRaise")
        _ax_set(window, "AXMain", True)


def type_text_into(el: UIElement, text: str) -> str:
    """Type into `el` through the accessibility API when it has an AXValue: insert at the cursor (AXSelectedText) and
    check the value took it. If not (or the element has no AXValue, e.g. web content), raise its window, focus it and
    send keystrokes. Keystrokes go to whatever window is in front, which is why the window is raised first."""
    if el.role in SINGLE_LINE_ROLES and el.ref is not None and _ax_get(el.ref, "AXValue") is not None:
        # One line: the typed text replaces whatever the field held ("Untitled" in a save sheet's name field).
        if _ax_set(el.ref, "AXValue", text) and _ax_get(el.ref, "AXValue") == text:
            return f'set field to "{text}" via AX'
        _raise_window(el)
        _focus(el)
        time.sleep(0.15)
        press_shortcut("cmd+a")
        type_text(text)
        now = _ax_get(el.ref, "AXValue")
        return f"typed {len(text)} chars over the old value" + ("" if now in (None, text) else f"; field now reads {now!r}")
    if el.ref is not None and _ax_get(el.ref, "AXValue") is not None:
        if _ax_set(el.ref, "AXSelectedText", text):
            time.sleep(0.05)
            now = _ax_get(el.ref, "AXValue")
            if isinstance(now, str) and text in now:
                return f"inserted {len(text)} chars via AX"
    if el.ref is not None:
        _raise_window(el)
    _focus(el)
    time.sleep(0.15)
    type_text(text)
    return f"typed {len(text)} chars"


def run_menu(cmd) -> ActionResult:
    """Run a menu command (observe/menus.py): its shortcut when it has one, else AXPress on the item (works on closed
    menus), else open its menu first and press again."""
    try:
        if cmd.keys:
            press_shortcut(cmd.keys)
            return ActionResult(True, f'menu "{cmd.path}" via {cmd.keys}')
        if cmd.ref is not None and AXUIElementPerformAction(cmd.ref, "AXPress") == 0:
            return ActionResult(True, f'menu "{cmd.path}" via AXPress')
        if cmd.bar_ref is not None and AXUIElementPerformAction(cmd.bar_ref, "AXPress") == 0:
            time.sleep(0.2)
            if cmd.ref is not None and AXUIElementPerformAction(cmd.ref, "AXPress") == 0:
                return ActionResult(True, f'menu "{cmd.path}" via the opened menu')
    except ValueError as e:
        return ActionResult(False, str(e))
    return ActionResult(False, f'could not run menu "{cmd.path}"')


GONE = "page changed: element gone; re-observe"
SECURE = "typing into password fields is not allowed; the user must do this"


def _front_browser() -> str:
    from local_agent.observe.ax_tree import frontmost_app

    bundle = str(frontmost_app().bundleIdentifier())
    if bundle not in browser.KNOWN_BROWSERS:
        raise ActionError("the browser showing this page is not in front")
    return bundle


DIALOG_UP = "a browser dialog is open; answer it first (click its button)"


def _dialog_up(bundle: str) -> bool:
    from local_agent.observe.ax_tree import browser_dialog_up

    return browser_dialog_up(bundle)


def execute_web(step: PlannerOutput, element: UIElement, bundle: str) -> ActionResult:
    """click / type / scroll on a page element through the fixed script in act/web_actions.js (a data-la-id, never
    planner-written JS). Typing falls back to keystrokes when a rich editor refuses insertText."""
    a = step.action
    if _dialog_up(bundle):  # page JavaScript would block behind it
        return ActionResult(False, DIALOG_UP)
    try:
        if a == "type" and (element.role == "AXSecureTextField"):
            return ActionResult(False, SECURE)
        r = browser.web_action(bundle, {"click": "click", "type": "type", "scroll": "scroll"}[a], element.web_id,
                               text=step.text if a == "type" else None,
                               direction=(step.direction or "down") if a == "scroll" else None)  # fmt: skip
    except browser.BrowserError as e:
        return ActionResult(False, f"page action failed ({e.kind}): {e}")
    if r.get("gone"):
        return ActionResult(False, GONE)
    if r.get("secure"):
        return ActionResult(False, SECURE)
    if a == "click":
        return ActionResult(bool(r.get("ok")), "clicked in the page" if r.get("ok") else f"click failed: {r.get('error', '')}")
    if a == "scroll":
        where = "the page" if r.get("where") == "window" else "the list"
        return ActionResult(True, f"scrolled {where} {step.direction or 'down'}"
                            + ("" if r.get("moved") else "; the position did not change (already at the end?)"))  # fmt: skip
    if r.get("ok"):
        return ActionResult(True, f"{r.get('how', 'set')} {len(step.text or '')} chars via the page")
    if r.get("fallback"):  # the editor is focused by the script; send keystrokes
        time.sleep(0.15)
        type_text(step.text or "")
        return ActionResult(True, f"insertText did not land; typed {len(step.text or '')} chars by keystrokes")
    return ActionResult(False, f"typing failed: {r.get('error', 'unknown')}")


def trusted_click(element: UIElement) -> ActionResult:
    """A real mouse click at the element's current place: scroll it into view, re-read its rect and map it to screen
    points through the AXWebArea frame and innerWidth (for single-page apps that ignore synthetic clicks)."""
    from local_agent.observe.ax_tree import web_area_frame

    try:
        bundle = _front_browser()
        if _dialog_up(bundle):
            return ActionResult(False, DIALOG_UP)
        r = browser.web_action(bundle, "rect", element.web_id)
        if r.get("gone"):
            return ActionResult(False, GONE)
        area = web_area_frame()
        if area is None or not r.get("inner_width"):
            return ActionResult(False, "no page area to map the click to")
        x, y, w, h = r["rect"]
        scale = area.w / r["inner_width"]
        px, py = area.x + (x + w / 2) * scale, area.y + (y + h / 2) * scale
        time.sleep(0.15)  # the scroll above is animated in some pages
        click_at(px, py)
        return ActionResult(True, f"trusted click at ({int(px)},{int(py)})")
    except (ActionError, browser.BrowserError) as e:
        return ActionResult(False, str(e))


def settle_web() -> float:
    """After a page action: wait for the page to stop changing (readyState complete, 300 ms without DOM mutations)."""
    try:
        bundle = _front_browser()
        return 0.0 if _dialog_up(bundle) else browser.settle(bundle)
    except ActionError:
        return 0.0


def execute(step: PlannerOutput, element: UIElement | None) -> ActionResult:
    """Run one non-terminal step. `element` is the Jev-selected element (None when the step has no target)."""
    a = step.action
    if element is not None and element.web_id and a in ("click", "type", "scroll"):
        try:
            return execute_web(step, element, _front_browser())
        except ActionError as e:
            return ActionResult(False, str(e))
    try:
        if a in ("click", "double_click", "right_click"):
            assert element is not None
            how = _click_element(
                element, button="right" if a == "right_click" else "left", clicks=2 if a == "double_click" else 1
            )
            return ActionResult(True, how)
        if a == "type":
            if element is not None:
                return ActionResult(True, type_text_into(element, step.text or ""))
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
