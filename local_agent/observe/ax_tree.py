"""Walk the macOS Accessibility tree of the frontmost window and collect interactive elements."""

from __future__ import annotations

import subprocess
import time

from AppKit import (
    NSApplicationActivateAllWindows,
    NSApplicationActivationPolicyRegular,
    NSDate,
    NSRunLoop,
    NSRunningApplication,
    NSWorkspace,
)
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
    kAXTrustedCheckOptionPrompt,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)

from local_agent.config import Settings
from local_agent.observe.models import Frame, Snapshot, UIElement

INTERACTIVE_ROLES = {
    "AXButton",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXMenuButton",
    "AXComboBox",
    "AXTextField",
    "AXTextArea",
    "AXSecureTextField",
    "AXSearchField",
    "AXSlider",
    "AXIncrementor",
    "AXLink",
    "AXMenuItem",
    "AXMenuBarItem",
    "AXTab",
    "AXDisclosureTriangle",
    "AXColorWell",
    "AXDateField",
    "AXCell",
    "AXRow",
    "AXSegmentedControl",
}
INTERACTIVE_ACTIONS = {"AXPress", "AXConfirm", "AXPick", "AXIncrement", "AXOpen"}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
DISPLAY_TEXT_ROLES = {"AXStaticText", "AXHeading"}


class AccessibilityError(RuntimeError):
    pass


def ensure_trusted(prompt: bool = True) -> bool:
    """True if this process may use the AX API. With prompt=True macOS shows the permission dialog."""
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))


def _attr(el, name: str):
    err, value = AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def _str(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s.replace("\n", " ")[:200]


def _frame(el) -> Frame | None:
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    ok_p, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok_s, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (ok_p and ok_s):
        return None
    return Frame(x=float(p.x), y=float(p.y), w=float(s.width), h=float(s.height))


def _actions(el) -> list[str]:
    err, names = AXUIElementCopyActionNames(el, None)
    return list(names) if err == 0 and names else []


def _child_text(el, limit: int = 4) -> str:
    """Name of an unlabeled row/cell/button from its first text children (e.g. Finder file names)."""
    parts = []
    for child in (_attr(el, "AXChildren") or [])[:limit]:
        if _str(_attr(child, "AXRole")) in DISPLAY_TEXT_ROLES | {"AXTextField"}:
            text = _str(_attr(child, "AXValue")) or _str(_attr(child, "AXTitle"))
            if text:
                parts.append(text)
    return " ".join(parts)[:120]


def _to_element(el, idx: int, role: str, path: str) -> UIElement | None:
    actions = _actions(el)
    interactive = role in INTERACTIVE_ROLES or bool(INTERACTIVE_ACTIONS & set(actions))
    if not interactive and role not in DISPLAY_TEXT_ROLES:
        return None
    frame = _frame(el)
    if frame is not None and (frame.w <= 1 or frame.h <= 1):
        return None  # hidden / collapsed
    if not interactive:
        # Visible text for the planner's screen view (labels, dialog messages, headings); never a Jev candidate.
        text = _str(_attr(el, "AXValue")) or _str(_attr(el, "AXTitle")) or _str(_attr(el, "AXDescription"))
        if not text:
            return None
        return UIElement(id=idx, role=role, value=text, frame=frame, path=path, interactive=False, ref=el)
    enabled = _attr(el, "AXEnabled")
    title, label = _str(_attr(el, "AXTitle")), _str(_attr(el, "AXDescription"))
    if not (title or label) and role not in TEXT_ROLES:
        label = _child_text(el)
    return UIElement(
        id=idx,
        role=role,
        subrole=_str(_attr(el, "AXSubrole")),
        title=title,
        label=label,
        value=_str(_attr(el, "AXValue")),
        placeholder=_str(_attr(el, "AXPlaceholderValue")),
        help=_str(_attr(el, "AXHelp")),
        selected=_str(_attr(el, "AXSelectedText")) if role in TEXT_ROLES else "",
        enabled=True if enabled is None else bool(enabled),
        focused=bool(_attr(el, "AXFocused")),
        frame=frame,
        actions=actions,
        path=path,
        submenu=role == "AXMenuItem" and bool(_attr(el, "AXChildren")),
        ref=el,
    )


def _walk(root, root_path: str, settings: Settings, out: list[UIElement], deadline: float) -> None:
    stack = [(root, 0, root_path)]
    while stack:
        if len(out) >= settings.max_elements or time.monotonic() > deadline:
            return
        el, depth, path = stack.pop()
        role = _str(_attr(el, "AXRole"))
        found = _to_element(el, len(out), role, path)
        if found is not None:
            out.append(found)
        if depth >= settings.max_depth:
            continue
        children = _attr(el, "AXChildren") or []
        title = _str(_attr(el, "AXTitle")) or _str(_attr(el, "AXDescription"))
        child_path = f"{path} > {role}:{title}" if title else f"{path} > {role}"
        # Reverse so the stack pops children in visual (document) order.
        for child in reversed(list(children)):
            stack.append((child, depth + 1, child_path.lstrip(" >")))


def frontmost_app():
    """The app with keyboard focus. NSWorkspace.frontmostApplication() is KVO-backed and goes stale in a process
    without a run loop (like this CLI), so ask the AX system-wide element first."""
    err, focused = AXUIElementCopyAttributeValue(AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
    if err == 0 and focused is not None:
        err, pid = AXUIElementGetPid(focused, None)
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if err == 0 else None
        if app is not None:
            return app
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        raise AccessibilityError("No frontmost application")
    return app


def _running(name: str):
    """The running regular (Dock) app named `name` or with that bundle id; skips helpers and CLI processes."""
    want = name.lower().removesuffix(".app")
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != NSApplicationActivationPolicyRegular:
            continue
        if want in (_str(app.localizedName()).lower(), _str(app.bundleIdentifier()).lower()):
            return app
    return None


def activate_app(name: str, timeout_s: float = 10.0) -> None:
    """Launch `name` if needed and bring it to the front. `open -a` alone launches but, on recent macOS, does
    not take focus from the app the user is in; NSRunningApplication.activate does."""
    app = _running(name)
    if app is None:
        subprocess.run(["open", "-g", "-a", name], check=True, capture_output=True)
        deadline = time.monotonic() + timeout_s
        while (app := _running(name)) is None:
            if time.monotonic() > deadline:
                raise AccessibilityError(f"{name} did not start within {timeout_s:.0f}s")
            time.sleep(0.2)
    app.activateWithOptions_(NSApplicationActivateAllWindows)
    pid = app.processIdentifier()
    deadline = time.monotonic() + timeout_s
    while frontmost_app().processIdentifier() != pid:
        if time.monotonic() > deadline:
            raise AccessibilityError(f"Could not bring {name} to the front")
        time.sleep(0.1)


def snapshot(settings: Settings | None = None, include_menubar: bool = True) -> Snapshot:
    """Snapshot the focused window of the frontmost app (plus its menu bar and any open menus)."""
    settings = settings or Settings()
    if not ensure_trusted(prompt=False):
        raise AccessibilityError(
            "Accessibility permission missing. Grant it to the app that runs local-agent (your terminal, or the "
            "LocalAgent app) in "
            "System Settings > Privacy & Security > Accessibility, then retry."
        )
    app = frontmost_app()
    pid = int(app.processIdentifier())
    app_el = AXUIElementCreateApplication(pid)
    # Electron/Chromium apps only build their AX tree once asked; harmless elsewhere.
    AXUIElementSetAttributeValue(app_el, "AXManualAccessibility", True)
    window =_attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow")
    if window is None:
        windows = _attr(app_el, "AXWindows") or []
        window = windows[0] if windows else None

    deadline = time.monotonic() + settings.walk_timeout_s
    elements: list[UIElement] = []
    window_title = ""
    if window is not None:
        window_title = _str(_attr(window, "AXTitle"))
        _walk(window, "", settings, elements, deadline)

    # Open context/popup menus hang off the application element, not the window.
    for child in _attr(app_el, "AXChildren") or []:
        if _str(_attr(child, "AXRole")) == "AXMenu":
            _walk(child, "AXMenu", settings, elements, deadline)

    if include_menubar:
        menubar = _attr(app_el, "AXMenuBar")
        open_menus = []
        for item in (_attr(menubar, "AXChildren") or []) if menubar is not None else []:
            el = _to_element(item, len(elements), _str(_attr(item, "AXRole")), "AXMenuBar")
            if el is not None:
                elements.append(el)
            if _attr(item, "AXSelected"):
                open_menus.append((item, f"AXMenuBar > AXMenuBarItem:{_str(_attr(item, 'AXTitle'))}"))
        # Items of an open menu (and its open submenus) have real frames; closed ones are 0x0 and get skipped.
        for item, path in open_menus:
            for menu in _attr(item, "AXChildren") or []:
                _walk(menu, path, settings, elements, deadline)

    return Snapshot(
        app=_str(app.localizedName()),
        bundle_id=_str(app.bundleIdentifier()),
        pid=pid,
        window_title=window_title,
        elements=elements,
    )
