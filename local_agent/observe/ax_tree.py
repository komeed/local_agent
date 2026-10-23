"""Walk the macOS Accessibility tree of the frontmost window and collect interactive elements."""

from __future__ import annotations

import os
import re
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
from local_agent.observe.context import add_context
from local_agent.observe.menus import read_commands
from local_agent.observe.models import Frame, Snapshot, UIElement, path_from_file_url

STAGE = None  # optional callable(name): reports the phase of a snapshot ("reading_page") to the event stream

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
# Scroll-bar parts: unnamed AXButtons nobody means to click (the planner scrolls with `scroll`).
SCROLL_PARTS = {"AXIncrementArrow", "AXDecrementArrow", "AXIncrementPage", "AXDecrementPage"}
# Containers whose children (rows) are walked after everything else, so a long list can't use up the walk's time
# before the window's buttons are reached (TextEdit's Open panel: 1,339 file rows before "New Document").
LIST_CONTAINERS = {"AXTable", "AXOutline", "AXList", "AXBrowser", "AXCollection", "AXGrid"}


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


def _child_text(el, limit: int = 4, depth: int = 1) -> str:
    """Name of an unlabeled row/cell/button from its first text children (e.g. Finder file names)."""
    parts = []
    for child in (_attr(el, "AXChildren") or [])[:limit]:
        role = _str(_attr(child, "AXRole"))
        if role in DISPLAY_TEXT_ROLES | {"AXTextField"}:
            text = _str(_attr(child, "AXValue")) or _str(_attr(child, "AXTitle"))
            if text:
                parts.append(text)
        elif role in ("AXCell", "AXGroup") and depth > 0:
            # Table rows keep their text one level down, in cells.
            text = _child_text(child, limit, depth - 1)
            if text:
                parts.append(text)
    return " ".join(parts)[:120]


def _words(identifier: str) -> str:
    """'NewDocumentButton' -> 'New Document Button'; '' unless the identifier reads like words (not '_NS:123')."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", identifier).replace("_", " ").strip()
    return text if re.fullmatch(r"[A-Za-z][A-Za-z ]{2,40}", text) and " " in text else ""


def _to_element(el, idx: int, role: str, path: str) -> UIElement | None:
    if _str(_attr(el, "AXSubrole")) in SCROLL_PARTS:
        return None
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
    if not (title or label):
        # Form fields point at their visible label through AXTitleUIElement.
        title_el = _attr(el, "AXTitleUIElement")
        if title_el is not None:
            label = _str(_attr(title_el, "AXValue")) or _str(_attr(title_el, "AXTitle"))
    if not (title or label) and role not in TEXT_ROLES:
        label = _child_text(el)
    if not (title or label) and role not in TEXT_ROLES:
        # Last resorts for icon-only controls: a developer identifier that reads like words. (AXHelp is read below
        # and counts as a name too.)
        label = _words(_str(_attr(el, "AXIdentifier")))
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


def _list_children(el) -> tuple[list, int]:
    """(children to walk, total row count) of a list container: just the visible rows when the container reports them
    (a 1,300-file Open panel would otherwise cost the whole walk timeout), else all its children. The total is a
    length only; the unwalked rows are never read."""
    for name in ("AXVisibleRows", "AXVisibleChildren"):
        visible = _attr(el, name)
        if visible:
            all_rows = _attr(el, "AXRows") or _attr(el, "AXChildren") or []
            return list(visible), max(len(all_rows), len(visible))
    children = list(_attr(el, "AXChildren") or [])
    return children, len(children)


def _walk(root, root_path: str, settings: Settings, out: list[UIElement], deadline: float,
          web_areas: list | None = None, urls: list | None = None) -> None:
    """_walk_tree, then flag each default button its sheet, window or dialog named (AXDefaultButton).
    With `web_areas`, web content isn't walked; each (AXWebArea, path) is appended instead."""
    defaults: list = []
    try:
        _walk_tree(root, root_path, settings, out, deadline, defaults, web_areas, urls)
    finally:
        for e in out:
            if e.role == "AXButton" and any(e.ref is d or e.ref == d for d in defaults):
                e.is_default = True


def _walk_tree(root, root_path: str, settings: Settings, out: list[UIElement], deadline: float, defaults: list,
               web_areas: list | None = None, urls: list | None = None) -> None:
    """Depth-first in document order, except that list rows (children of LIST_CONTAINERS) wait until everything else
    is walked; when the time or element limit hits, only rows are lost."""
    stack = [(root, 0, root_path)]
    deferred: list[tuple] = []
    while stack or deferred:
        if len(out) >= settings.max_elements or time.monotonic() > deadline:
            return
        if not stack:
            stack, deferred = list(reversed(deferred)), []
        el, depth, path = stack.pop()
        role = _str(_attr(el, "AXRole"))
        if role == "AXWebArea" and urls is not None and (u := _url(_attr(el, "AXURL"))):
            urls.append((u, _str(_attr(el, "AXTitle"))))  # the page's address without any script
        if role == "AXWebArea" and web_areas is not None:
            web_areas.append((el, path))
            continue
        found = _to_element(el, len(out), role, path)
        if found is not None:
            out.append(found)
        if role in ("AXSheet", "AXWindow", "AXDialog") and (d := _attr(el, "AXDefaultButton")) is not None:
            defaults.append(d)
        if depth >= settings.max_depth:
            continue
        title = _str(_attr(el, "AXTitle")) or _str(_attr(el, "AXDescription"))
        child_path = f"{path} > {role}:{title}" if title else f"{path} > {role}"
        if role in LIST_CONTAINERS:
            children, total = _list_children(el)
            if total > len(children):  # the rest of a long list is never walked; keep only its size
                if found is not None:
                    found.row_count = total
                else:
                    out.append(UIElement(id=len(out), role=role, interactive=False, row_count=total,
                                         path=child_path.lstrip(" >")))  # fmt: skip
            deferred.extend((child, depth + 1, child_path.lstrip(" >")) for child in children)
            continue
        # Reverse so the stack pops children in visual (document) order.
        for child in reversed(list(_attr(el, "AXChildren") or [])):
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


def controller_app() -> str | None:
    """The app this agent was launched from (Terminal, iTerm2, the LocalAgent app, …): the nearest ancestor process
    that is a regular Dock app. Not simply the frontmost app, which is whatever the user was looking at when the
    agent was started in the background."""
    pid = os.getppid()
    for _ in range(20):
        if pid <= 1:
            return None
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.activationPolicy() == NSApplicationActivationPolicyRegular:
            return _str(app.localizedName()) or None
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not out.isdigit():
            return None
        pid = int(out)
    return None


def focus_after_open(command: str) -> str | None:
    """After a successful `open` command, bring what it opened to the front: the app of `open -a <App>`, or the
    default browser for `open <url>`. Returns a note for the step record, or None when nothing was done."""
    from local_agent.apps import opened_app

    name = opened_app(command)
    if name is None:
        url = next((t for t in command.split()[1:] if "://" in t), None) if command.startswith("open ") else None
        if url is None:
            return None
        from AppKit import NSURL

        app_url = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.URLWithString_(url))
        if app_url is None:
            return None
        name = app_url.path().rsplit("/", 1)[-1].removesuffix(".app")
    try:
        activate_app(name)
    except (AccessibilityError, subprocess.CalledProcessError) as e:
        return f"could not bring {name} to the front: {e}"
    time.sleep(0.5)  # let its window finish appearing before the next observation
    return f"{name} is now frontmost"


def document_window(window):
    """The window a sheet or attached dialog belongs to (it holds the document and the sheet as a child), else the
    window itself. The focused window is the sheet while one is open, which would hide the document behind it."""
    if window is None:
        return None
    attached = _str(_attr(window, "AXRole")) == "AXSheet" or _str(_attr(window, "AXSubrole")) == "AXDialog"
    parent = _attr(window, "AXParent") if attached else None
    return parent if parent is not None and _str(_attr(parent, "AXRole")) == "AXWindow" else window


def fingerprint():
    """A cheap reading of the front window to see when an action has finished: the app, the window title and number of
    children, and the focused element (role, title, a bit of its value). None when it can't be read; two equal
    readings in a row mean the screen has settled."""
    try:
        app = frontmost_app()
        app_el = AXUIElementCreateApplication(int(app.processIdentifier()))
        window = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow")
        focused = _attr(app_el, "AXFocusedUIElement")
        kids = _attr(window, "AXChildren") if window is not None else None
        return (
            int(app.processIdentifier()),
            _str(_attr(window, "AXTitle")) if window is not None else "",
            len(kids) if kids is not None else -1,
            (_str(_attr(focused, "AXRole")), _str(_attr(focused, "AXTitle")), _str(_attr(focused, "AXValue"))[:60])
            if focused is not None else None,
            len(_attr(app_el, "AXChildren") or []),  # open menus and popups hang off the application element
        )
    except Exception:  # noqa: BLE001 - any AX failure just means "settle by sleeping"
        return None


def snapshot(settings: Settings | None = None, include_menubar: bool = True) -> Snapshot:
    """Snapshot the focused window of the frontmost app (plus its menu bar and any open menus)."""
    settings = settings or Settings()
    if not ensure_trusted(prompt=False):
        raise AccessibilityError(
            "Accessibility permission missing. Grant it to your terminal app in "
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
    window = document_window(window)

    deadline = time.monotonic() + settings.walk_timeout_s
    elements: list[UIElement] = []
    window_title, document_path, menu_commands = "", "", []
    bundle_id = _str(app.bundleIdentifier())
    page_urls: list = []
    window_title = _str(_attr(window, "AXTitle")) if window is not None else ""
    dialog_up = settings.web_reading and bundle_id in _browser_bundles() and browser_dialog_up(bundle_id)
    ready, web_note = (_web_ready(bundle_id, window_title, dialog_up) if settings.web_reading else (False, ""))
    web_areas: list | None = [] if ready else None
    if window is not None:
        document_path = path_from_file_url(_str(_attr(window, "AXDocument")))
        _walk(window, "", settings, elements, deadline, web_areas, page_urls)

    # Open context/popup menus hang off the application element, not the window.
    for child in _attr(app_el, "AXChildren") or []:
        if _str(_attr(child, "AXRole")) == "AXMenu":
            _walk(child, "AXMenu", settings, elements, deadline)

    if include_menubar:
        menubar = _attr(app_el, "AXMenuBar")
        menu_commands = read_commands(menubar) if menubar is not None else []
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

    add_context(elements)
    url, page_title, web = "", "", {}
    if web_areas:
        url, page_title, web = _merge_web(bundle_id, web_areas, settings, elements, deadline)
    if web_note and not web:
        web = {"error": web_note}
    if not url and page_urls:  # no script reading: the web area's own AXURL
        url, ax_title = page_urls[0]
        page_title = page_title or ax_title
    return Snapshot(
        app=_str(app.localizedName()),
        bundle_id=bundle_id,
        pid=pid,
        window_title=window_title,
        elements=elements,
        document_path=document_path,
        menu_commands=menu_commands,
        url=url,
        page_title=page_title,
        web=web,
    )


def _url(value) -> str:
    """AXURL arrives as an NSURL (or a string); "" for anything else."""
    if value is None:
        return ""
    return str(value.absoluteString()) if hasattr(value, "absoluteString") else str(value)


DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog", "AXSystemFloatingWindow"}


def _browser_bundles():
    from local_agent.act.browser import KNOWN_BROWSERS

    return KNOWN_BROWSERS


def dialog_roles_present(window_subroles: list[str], window_children_roles: list[str]) -> bool:
    """A window with a dialog subrole, or a sheet on a window. (Browser-native dialogs such as "Changes you made may not
    be saved" are not in the page: while one is up, page JavaScript blocks.)"""
    return bool(DIALOG_SUBROLES & set(window_subroles)) or "AXSheet" in window_children_roles


def browser_dialog_up(bundle_id: str) -> bool:
    """Is a dialog or sheet open in this app? Reads only AX (no page JavaScript)."""
    app = _running(bundle_id)
    if app is None:
        return False
    app_el = AXUIElementCreateApplication(int(app.processIdentifier()))
    for win in _attr(app_el, "AXWindows") or []:
        children = [_str(_attr(c, "AXRole")) for c in (_attr(win, "AXChildren") or [])]
        if dialog_roles_present([_str(_attr(win, "AXSubrole"))], children):
            return True
    return False


def _web_ready(bundle_id: str, window_title: str = "", dialog_up: bool = False) -> tuple[bool, str]:
    """(ready, note): the front app is a browser whose JavaScript from Apple Events works AND whose AppleScript `front
    window` is the window AX says is frontmost (a Little Arc or popup window is not: then the page comes from AX)."""
    from local_agent.act import browser

    if bundle_id not in browser.KNOWN_BROWSERS:
        return False, ""
    if dialog_up:  # page JavaScript blocks behind a native dialog: don't even ask
        return False, "dialog"
    st = browser.status(bundle_id)
    if st != "ok":
        return False, "timeout" if st == "timeout" else ""
    try:
        same = browser.window_matches(browser.front_window_name(bundle_id), window_title)
    except browser.BrowserError as e:
        if e.kind == "timeout":
            return False, "timeout"
        same = False
    return (True, "") if same else (False, "front window not readable")


def _merge_web(bundle_id, web_areas, settings, elements, deadline):
    """Replace the unwalked page with the web outline. If the outline fails, walk the page through AX after all."""
    from local_agent.act import browser
    from local_agent.observe import web

    if STAGE:
        STAGE("reading_page")
    area, area_path = max(web_areas, key=lambda a: (lambda f: f.w * f.h if f else 0)(_frame(a[0])))
    try:
        found, info = web.timed_outline(bundle_id, _frame(area), len(elements), settings.web_max_elements)
    except browser.BrowserError as e:  # the page script failed or timed out: read the page through AX after all
        for el, path in web_areas:
            _walk(el, path, settings, elements, deadline)
        return "", "", {"error": e.kind}
    elements.extend(found)
    url, title = info.pop("url"), info.pop("title")
    return url, title, info


def web_area_frame(max_depth: int = 12) -> Frame | None:
    """Frame of the front window's page (its largest AXWebArea), for mapping page points to the screen."""
    app = frontmost_app()
    app_el = AXUIElementCreateApplication(int(app.processIdentifier()))
    window = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow")
    best, stack = None, [(window, 0)] if window is not None else []
    while stack:
        el, depth = stack.pop()
        if _str(_attr(el, "AXRole")) == "AXWebArea":
            f = _frame(el)
            if f is not None and (best is None or f.w * f.h > best.w * best.h):
                best = f
            continue
        if depth < max_depth:
            stack.extend((c, depth + 1) for c in (_attr(el, "AXChildren") or []))
    return best


def focused_ui(bundle_id: str):
    """The browser's focused UI element as a browser.FocusedField (None when nothing is focused or the app isn't running)."""
    from local_agent.act.browser import FocusedField

    app = _running(bundle_id)
    if app is None:
        return None
    el = _attr(AXUIElementCreateApplication(int(app.processIdentifier())), "AXFocusedUIElement")
    if el is None:
        return None
    return FocusedField(el, _str(_attr(el, "AXRole")), _str(_attr(el, "AXSubrole")), _str(_attr(el, "AXIdentifier")),
                        _str(_attr(el, "AXDescription")))
