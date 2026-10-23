"""Run fixed JavaScript in a browser's front tab through AppleScript (Phase 0 of docs/PLAN_browser.md).

Two dialects, chosen once per bundle id from the app's scripting dictionary (`sdef`), never by browser name:
chromium (`execute <tab> javascript`) and safari (`do JavaScript … in <tab>`). Only scripts shipped in this repo run.
"""

from __future__ import annotations

import json
import math
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

Dialect = Literal["chromium", "safari"]
ErrorKind = Literal["js_disabled", "no_permission", "no_window", "not_running", "timeout", "search_field", "other"]

# Only to answer "is this a browser" for doctor; the dialect always comes from the dictionary.
KNOWN_BROWSERS: dict[str, str] = {
    "com.apple.Safari": "Safari",
    "com.google.Chrome": "Chrome",
    "company.thebrowser.Browser": "Arc",
    "com.brave.Browser": "Brave",
    "com.microsoft.edgemac": "Edge",
    "com.vivaldi.Vivaldi": "Vivaldi",
    "com.operasoftware.Opera": "Opera",
}

FIX = {
    "js_disabled": {
        "safari": "Safari › Settings › Advanced › Show features for web developers, then Develop › Allow JavaScript from Apple Events",
        "chromium": "View › Developer › Allow JavaScript from Apple Events",
    },
    "no_permission": {
        "any": "System Settings › Privacy & Security › Automation: allow your terminal/app to control this browser",
    },
    "no_window": {"any": "open a browser window with a normal tab"},
    "not_running": {"any": "start the browser"},
}


class BrowserError(Exception):
    def __init__(self, kind: ErrorKind, message: str, number: int | None = None):
        super().__init__(message)
        self.kind, self.number = kind, number


def classify_error(info: dict | None) -> BrowserError:
    """Map an NSAppleScript error dict (`NSAppleScriptErrorNumber` / `…Message`) to a BrowserError."""
    info = info or {}
    num = info.get("NSAppleScriptErrorNumber")
    msg = str(info.get("NSAppleScriptErrorMessage") or info.get("NSAppleScriptErrorBriefMessage") or "error")
    low = msg.lower()
    if num == -1712 or "timed out" in low:
        kind: ErrorKind = "timeout"
    elif num == -1743 or "not authorized" in low or "not allowed assistive" in low:
        kind = "no_permission"
    elif "allow javascript from apple events" in low or "javascript through applescript is turned off" in low:
        kind = "js_disabled"
    elif num == -600 or "isn’t running" in low or "isn't running" in low or "application isn" in low:
        kind = "not_running"
    elif num == -1728 and re.search(r"(window|tab)", low) or "no window" in low or num == -1719:
        kind = "no_window"
    else:
        kind = "other"
    return BrowserError(kind, msg, num)


def escape_applescript(s: str) -> str:
    """Body of an AppleScript string literal: backslash and double quote are the only specials."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def wrap(src: str, args: dict | None) -> str:
    """The JS the browser runs: `src` is a function body that sees `args`; the result comes back as the JSON string of {r: result} (an envelope, so a result that is itself a string is unambiguous)."""
    payload = json.dumps(args or {}, ensure_ascii=False).replace("</", "<\\/")
    # U+2028/2029 are legal in JSON but ended string literals before ES2019.
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"JSON.stringify({{r: (function(args){{{src}\n}})({payload})}})"


def script_for(dialect: Dialect, bundle_id: str, js: str) -> str:
    lit = escape_applescript(js)
    b = escape_applescript(bundle_id)
    if dialect == "safari":
        return f'tell application id "{b}" to do JavaScript "{lit}" in current tab of front window'
    return f'tell application id "{b}" to execute active tab of front window javascript "{lit}"'


def dialect_from_sdef(xml: str) -> Dialect | None:
    if re.search(r'<command name="do JavaScript"', xml):
        return "safari"
    if re.search(r'<command name="execute"[^>]*>', xml) and re.search(r'name="javascript"', xml):
        return "chromium"
    return None


def app_path(bundle_id: str) -> str | None:
    from AppKit import NSWorkspace

    url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle_id)
    return str(url.path()) if url else None


def is_running(bundle_id: str) -> bool:
    from AppKit import NSWorkspace

    return any(a.bundleIdentifier() == bundle_id for a in NSWorkspace.sharedWorkspace().runningApplications())


@lru_cache(maxsize=None)
def sdef_xml(bundle_id: str) -> str:
    path = app_path(bundle_id)
    if not path:
        raise BrowserError("other", f"{bundle_id} is not installed")
    return subprocess.run(["sdef", path], capture_output=True, text=True, timeout=10).stdout


@lru_cache(maxsize=None)
def dialect(bundle_id: str) -> Dialect:
    xml = sdef_xml(bundle_id)
    found = dialect_from_sdef(xml)
    if not found:
        raise BrowserError("other", f"{bundle_id} has no JavaScript command in its scripting dictionary")
    return found


# Seconds before a call gives up: AppleScript's own `with timeout` (the app didn't answer) and, a second later, a hard
# limit in Python (NSAppleScript itself blocked). "read" = status, list_tabs, select and the page outline.
TIMEOUTS = {"read": 3.0, "action": 5.0}


def configure(read_s: float | None = None, action_s: float | None = None) -> None:
    if read_s:
        TIMEOUTS["read"] = read_s
    if action_s:
        TIMEOUTS["action"] = action_s


class _Worker:
    """One thread that runs every AppleScript, so NSAppleScript always sees the same thread. If a call blocks past its
    limit the thread is abandoned (a daemon, it dies with the process) and the next call gets a fresh one, instead of
    queueing behind the stuck call forever."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="applescript").start()

    def _loop(self):
        while True:
            fn, box, done = self.jobs.get()
            try:
                box.append((True, fn()))
            except BaseException as e:  # handed back to the caller
                box.append((False, e))
            done.set()


_worker: _Worker | None = None
_worker_lock = threading.Lock()


def _on_worker(fn, timeout_s: float):
    global _worker
    with _worker_lock:
        w = _worker = _worker or _Worker()
    box: list = []
    done = threading.Event()
    w.jobs.put((fn, box, done))
    if not done.wait(timeout_s):
        with _worker_lock:
            if _worker is w:
                _worker = None
        raise BrowserError("timeout", f"the browser did not answer within {timeout_s:.0f}s")
    ok, value = box[0]
    if not ok:
        raise value
    return value


def _run_applescript(source: str):
    from Foundation import NSAppleScript

    script = NSAppleScript.alloc().initWithSource_(source)
    result, err = script.executeAndReturnError_(None)
    if result is None:
        raise classify_error(dict(err) if err else None)
    return result.stringValue()


def _execute(source: str, kind: str = "read"):
    limit = TIMEOUTS[kind]
    wrapped = f"with timeout of {max(1, math.ceil(limit))} seconds\n{source}\nend timeout"
    return _on_worker(lambda: _run_applescript(wrapped), limit + 1.0)


def run_js(bundle_id: str, src: str, args: dict | None = None, kind: str = "read") -> Any:
    """Run `src` (a function body; `args` in scope) in the front tab and return its JSON-decoded result."""
    if not is_running(bundle_id):
        raise BrowserError("not_running", f"{bundle_id} is not running")
    out = _execute(script_for(dialect(bundle_id), bundle_id, wrap(src, args)), kind)
    if out is None or out == "":
        return None
    try:
        env = json.loads(out)
        if isinstance(env, str):  # Arc JSON-encodes the returned string once more
            env = json.loads(env)
        return env.get("r") if isinstance(env, dict) else None
    except ValueError as e:
        raise BrowserError("other", f"unparseable result: {out[:80]!r}") from e


def status(bundle_id: str) -> str:
    """"ok" or the BrowserError kind."""
    try:
        return "ok" if run_js(bundle_id, "return 1+1;") == 2 else "other"
    except BrowserError as e:
        return e.kind


def installed_browsers() -> list[tuple[str, str]]:
    """(bundle id, name) of every KNOWN_BROWSERS app that is installed."""
    return [(b, n) for b, n in KNOWN_BROWSERS.items() if app_path(b)]


@dataclass
class BrowserLine:
    name: str
    status: str
    fix: str = ""

    def text(self) -> str:
        label = {"ok": "ok", "js_disabled": "JS off", "no_permission": "no permission", "not_running": "not running",
                 "no_window": "no window", "other": "error"}.get(self.status, self.status)
        return f"{self.name}: {label}" + (f" — {self.fix}" if self.fix else "")


def fix_text(kind: str, dialect_name: str | None) -> str:
    table = FIX.get(kind, {})
    return table.get(dialect_name or "") or table.get("any", "")


def doctor_lines(browsers: list[tuple[str, str]] | None = None, probe=status, dialect_of=dialect, running=is_running) -> list[BrowserLine]:
    """One line per installed browser. Browsers that are not running are reported, never launched."""
    lines = []
    for bundle, name in browsers if browsers is not None else installed_browsers():
        st = probe(bundle) if running(bundle) else "not_running"
        d = None
        if st == "js_disabled":
            try:
                d = dialect_of(bundle)
            except BrowserError:
                pass
        lines.append(BrowserLine(name, st, "" if st == "ok" else fix_text(st, d)))
    return lines


# --- acting on the page (Phase 2) -------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _actions_js() -> str:
    from pathlib import Path

    return Path(__file__).with_name("web_actions.js").read_text()


def web_action(bundle_id: str, op: str, web_id: str = "", text: str | None = None, direction: str | None = None) -> dict:
    """Run the fixed page-action script. `op` is click / type / scroll / rect / settle; returns its result dict
    (`ok`, `gone` when the element left the DOM, `secure` for a password field, `fallback` when typing needs keys)."""
    args = {"op": op, "web_id": web_id}
    if text is not None:
        args["text"] = text
    if direction is not None:
        args["direction"] = direction
    out = run_js(bundle_id, _actions_js(), args, "read" if op in ("settle", "rect") else "action")
    return out if isinstance(out, dict) else {"ok": False, "error": "no result"}


def settle(bundle_id: str, quiet_ms: int = 300, cap_s: float = 3.0, poll_s: float = 0.1) -> float:
    """Wait until the page is `complete` and has had no DOM mutations for `quiet_ms` (capped at `cap_s`); returns
    the seconds waited. A page that can't be asked (navigating, no window) just ends the wait."""
    import time

    t0 = time.monotonic()
    while time.monotonic() - t0 < cap_s:
        try:
            st = web_action(bundle_id, "settle")
        except BrowserError:
            time.sleep(poll_s)
            continue
        if st.get("ready") and st.get("quiet_ms", 0) >= quiet_ms:
            break
        time.sleep(poll_s)
    return time.monotonic() - t0


# --- tabs: find, select, open (AppleScript dictionary only, no page JavaScript) ---------------------------------------


@dataclass(frozen=True)
class TabInfo:
    window: int  # AppleScript window index, 1 = front
    index: int
    id: str  # "" when the browser's tabs have no id (Safari)
    url: str


def select_style(bundle_id: str) -> str:
    """How a tab becomes the active one, from the dictionary: a `select` command on tabs (Arc), an `active tab index`
    property (Chrome and relatives) or `current tab` (Safari)."""
    xml = sdef_xml(bundle_id)
    if dialect(bundle_id) == "safari":
        return "current"
    return "select" if re.search(r'<command name="select"', xml) else "index"


_LIST_TABS = """set sep to (ASCII character 9)
set out to ""
tell application id "%(b)s"
	repeat with w from 1 to (count of windows)
		set us to {}
		try
			set us to URL of tabs of window w
		end try
		set ids to {}
		try
			set ids to id of tabs of window w
		end try
		repeat with i from 1 to (count of us)
			set tid to ""
			try
				set tid to (item i of ids) as text
			end try
			set u to ""
			try
				set u to (item i of us) as text
			end try
			set out to out & w & sep & i & sep & tid & sep & u & linefeed
		end repeat
	end repeat
end tell
return out"""


def parse_tabs(text: str) -> list[TabInfo]:
    tabs = []
    for line in (text or "").splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            tabs.append(TabInfo(int(parts[0]), int(parts[1]), parts[2], parts[3]))
    return tabs


def list_tabs(bundle_id: str) -> list[TabInfo]:
    return parse_tabs(_execute(_LIST_TABS % {"b": escape_applescript(bundle_id)}) or "")


def find_tab(bundle_id: str, matches) -> TabInfo | None:
    """The first tab whose URL `matches` (a predicate), the front window's tabs before other windows'."""
    return next((t for t in list_tabs(bundle_id) if matches(t.url)), None)


def select_script(style: str, bundle_id: str, tab: TabInfo) -> str:
    b, w = escape_applescript(bundle_id), tab.window
    front = f"\n\t\ttry\n\t\t\tset index of window {w} to 1\n\t\tend try" if w != 1 else ""
    if style == "select":
        pick = f'tell window {w} to tell tab id "{escape_applescript(tab.id)}" to select'
    elif style == "index":
        pick = f"set active tab index of window {w} to {tab.index}"
    else:
        pick = f"set current tab of window {w} to tab {tab.index} of window {w}"
    return f'tell application id "{b}"{front}\n\t{pick}\nend tell'


def select_tab(bundle_id: str, tab: TabInfo) -> None:
    _execute(select_script(select_style(bundle_id), bundle_id, tab), "action")


def check_url(url: str, allow_blank: bool = False) -> str:
    if allow_blank and url == "about:blank":
        return url
    if not re.fullmatch(r"(?:https?|file)://[^\s\"\\]+", url):
        raise BrowserError("other", f"not a plain web or file URL: {url!r}")
    return url


def open_script(style: str, bundle_id: str, url: str, allow_blank: bool = False) -> str:
    """ONE new tab with `url` in the front window; a window is made only when there is none. The `make new tab` is sent
    without waiting for the browser's answer (`ignoring application responses`): the caller polls for the tab."""
    b, u = escape_applescript(bundle_id), escape_applescript(check_url(url, allow_blank))
    new_window = "make new document" if style == "current" else "make new window"
    head = f'tell application id "{b}"\n\tif (count of windows) = 0 then {new_window}\nend tell\nignoring application responses\n'
    if style == "select":
        make = f'\ttell application id "{b}" to tell front window to make new tab with properties {{URL:"{u}"}}\n'
    else:
        make = f'\ttell application id "{b}" to make new tab at end of tabs of front window with properties {{URL:"{u}"}}\n'
    return head + make + "end ignoring"


def tab_key(t: TabInfo) -> str:
    return t.id or f"{t.window}:{t.index}:{t.url}"


def open_tab(bundle_id: str, url: str, wait_s: float = 5.0, poll_s: float = 0.3, stage=None,
             sleep=time.sleep, clock=time.monotonic, allow_blank: bool = False) -> TabInfo | None:
    """Send `make new tab` and return at once, then poll the tab list every `poll_s` for up to `wait_s` until a tab
    that wasn't there before shows a real URL (not blank). Chrome and Safari then select it. None: not seen in time."""
    style = select_style(bundle_id)
    try:
        before = {tab_key(t) for t in list_tabs(bundle_id)}
    except BrowserError:
        before = set()
    _execute(open_script(style, bundle_id, url, allow_blank), "action")
    if stage:
        stage("waiting_for_page")
    deadline = clock() + wait_s
    while True:
        sleep(poll_s)
        try:
            new = [t for t in list_tabs(bundle_id) if tab_key(t) not in before and (allow_blank or t.url not in ("", "about:blank"))]
        except BrowserError:
            new = []
        if new:
            if style != "select":
                select_tab(bundle_id, new[0])
            return new[0]
        if clock() >= deadline:
            return None


def navigate_script(style: str, bundle_id: str, url: str) -> str:
    """Point the ACTIVE tab at `url` (no new tab, no new window)."""
    b, u = escape_applescript(bundle_id), escape_applescript(check_url(url))
    tab = "current tab" if style == "current" else "active tab"
    return f'tell application id "{b}" to set URL of {tab} of front window to "{u}"'


def navigate_tab(bundle_id: str, url: str) -> None:
    _execute(navigate_script(select_style(bundle_id), bundle_id, url), "action")


def close_script(style: str, bundle_id: str, tab: TabInfo) -> str:
    b, w = escape_applescript(bundle_id), tab.window
    if style == "select":
        return f'tell application id "{b}" to tell window {w} to tell tab id "{escape_applescript(tab.id)}" to close'
    return f'tell application id "{b}" to close tab {tab.index} of window {w}'


def close_tab(bundle_id: str, tab: TabInfo) -> None:
    """Close a tab we made (by id where the browser has ids)."""
    _execute(close_script(select_style(bundle_id), bundle_id, tab), "action")


@dataclass
class FocusedField:
    ref: object
    role: str
    subrole: str = ""
    identifier: str = ""
    description: str = ""


ADDRESS_WORDS = re.compile(r"address|url|omnibox|location|search", re.I)
SINGLE_LINE = {"AXTextField", "AXComboBox", "AXSearchField"}
FOCUS_WAIT_S, FOCUS_POLL_S = 1.5, 0.1


def is_address_like(f: FocusedField) -> bool:
    """A focused single-line text field, or one that calls itself an address / URL / search field."""
    return f.role in SINGLE_LINE or bool(ADDRESS_WORDS.search(" ".join([f.subrole, f.identifier, f.description])))


def blank_url() -> str:
    """A local empty page to open our tab on: Arc refuses `about:blank` ("provide a valid URL"), so every browser gets a
    real file URL (a one-line HTML file in the temp directory)."""
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "local-agent-blank.html"
    if not path.exists():
        path.write_text("<!doctype html><meta charset=utf-8><title>local-agent</title>")
    return path.as_uri()


class _RealIO:
    """The AX, keyboard and tab primitives `fill_address` uses; tests replace this whole object."""

    sleep = staticmethod(time.sleep)
    clock = staticmethod(time.monotonic)

    opened = None

    def open_blank(self, bundle, stage):
        self.opened = open_tab(bundle, blank_url(), stage=stage)
        return self.opened

    def activate(self, bundle):
        from local_agent.observe.ax_tree import activate_app

        activate_app(KNOWN_BROWSERS.get(bundle, bundle))

    def is_front(self, bundle):
        from local_agent.observe.ax_tree import frontmost_app

        return str(frontmost_app().bundleIdentifier()) == bundle

    def shortcut(self, keys):
        from local_agent.act import executor

        executor.press_shortcut(keys)

    def keys(self, text):
        from local_agent.act import executor

        executor.type_text(text)

    def focused(self, bundle):
        from local_agent.observe.ax_tree import focused_ui

        return focused_ui(bundle)

    def same(self, a, b):
        return a is b or a == b

    def value(self, field):
        from local_agent.act import executor

        v = executor._ax_get(field.ref, "AXValue")
        return None if v is None else str(v)

    def set_value(self, field, text):
        from local_agent.act import executor

        return executor._ax_set(field.ref, "AXValue", text)

    def close(self, bundle, tab):
        close_tab(bundle, tab)


def _wait_for_field(io, bundle, before):
    """The focused editable single-line field that wasn't the focus before cmd+l, within FOCUS_WAIT_S."""
    deadline = io.clock() + FOCUS_WAIT_S
    while True:
        f = io.focused(bundle)
        if f is not None and is_address_like(f) and (before is None or not io.same(f.ref, before.ref)):
            return f
        if io.clock() >= deadline:
            return None
        io.sleep(FOCUS_POLL_S)


def _fill(io, bundle, field, query) -> str | None:
    """Put `query` in the field and check it. "verified" / "unverified: ..." on success, None when it didn't take."""
    want = query.strip().lower()
    if io.set_value(field, query) and (io.value(field) or "").strip().lower() == want:
        return "verified via AX value"
    io.shortcut("cmd+a")  # replace whatever the field held
    io.keys(query)
    io.sleep(0.15)
    got = io.value(field)
    if got is None:  # AX can't read it: accept only if the field still has focus
        again = io.focused(bundle)
        return "unverified: AX could not read the field" if again is not None and io.same(again.ref, field.ref) else None
    return "verified after typing" if got.strip().lower() == want else None


def fill_address(io, bundle: str, query: str, stage=None) -> str:
    """New blank tab -> cmd+l -> wait until an address field has focus -> set its text and check it -> return. Anything
    short of that closes our blank tab and raises BrowserError("search_field"). Return is never sent unchecked."""
    note = lambda name: stage(name) if stage else None  # noqa: E731
    tab = io.open_blank(bundle, stage)
    name = KNOWN_BROWSERS.get(bundle, bundle)
    try:
        io.activate(bundle)
        io.sleep(0.3)
        if not io.is_front(bundle):  # keystrokes go to the front app: never type into something else
            raise BrowserError("search_field", f"{name} is not in front, so the address bar can't be typed into")
        for attempt in range(2):
            before = io.focused(bundle)
            note("focusing_address_bar")
            io.shortcut("cmd+l")
            field = _wait_for_field(io, bundle, before)
            if field is None:
                continue
            note("typing_query")
            if (result := _fill(io, bundle, field, query)) is not None:
                io.shortcut("return")
                return result
        raise BrowserError("search_field", f"couldn't focus/fill {name}'s address field")
    except BaseException:
        if tab is not None:
            try:
                io.close(bundle, tab)  # leave no stray tab
            except BrowserError:
                pass
        raise


@dataclass
class SearchOutcome:
    note: str  # how the typed text was checked
    tab: TabInfo | None  # the tab we opened for the search (None: not identified)


def search_in_tab(bundle_id: str, query: str, stage=None, io=None) -> SearchOutcome:
    """Search the web for `query` the way a user does (the browser's own omnibox, so the user's engine; no engine URL is
    written here), in a tab we open ourselves."""
    io = io or _RealIO()
    note = fill_address(io, bundle_id, query, stage)
    return SearchOutcome(note, getattr(io, "opened", None))


def active_tab_script(style: str, bundle_id: str) -> str:
    b = escape_applescript(bundle_id)
    if style == "select":
        return f'tell application id "{b}" to get (id of active tab of front window) & linefeed & (URL of active tab of front window)'
    if style == "index":
        return (f'tell application id "{b}" to get (active tab index of front window as text) & linefeed & '
                "(URL of active tab of front window)")
    return (f'tell application id "{b}" to get (index of current tab of front window as text) & linefeed & '
            "(URL of current tab of front window)")


def active_tab(bundle_id: str) -> TabInfo | None:
    """The front window's active tab (window 1)."""
    style = select_style(bundle_id)
    out = _execute(active_tab_script(style, bundle_id)) or ""
    first, _, url = out.partition("\n")
    if not first.strip():
        return None
    if style == "select":
        return next((t for t in list_tabs(bundle_id) if t.window == 1 and t.id == first.strip()),
                    TabInfo(1, 0, first.strip(), url.strip()))
    return TabInfo(1, int(first), "", url.strip())


def front_window_name(bundle_id: str) -> str:
    return _execute(f'tell application id "{escape_applescript(bundle_id)}" to get name of front window') or ""


def window_matches(script_name: str, ax_title: str) -> bool:
    """Is the AX front window the scripting `front window`? Titles agree (browsers may add " - AppName")."""
    a, b = script_name.strip().lower(), ax_title.strip().lower()
    return a == b or (bool(a) and bool(b) and (a in b or b in a))


class Bridge:
    """What the agent loop uses to navigate the front browser window (a test double replaces it)."""

    def bundle_for(self, name_or_bundle: str) -> str | None:
        for bundle, name in KNOWN_BROWSERS.items():
            if name_or_bundle == bundle or name.lower() in name_or_bundle.lower():
                return bundle if app_path(bundle) else None
        return None

    def find_tab(self, bundle: str, site: str) -> TabInfo | None:
        from local_agent.goal_facts import _on_site

        return find_tab(bundle, lambda url: _on_site(site, url) is True)

    def select_tab(self, bundle: str, tab: TabInfo) -> None:
        select_tab(bundle, tab)

    def open_tab(self, bundle: str, url: str, stage=None) -> TabInfo | None:
        return open_tab(bundle, url, stage=stage)

    def search_in_tab(self, bundle: str, query: str, stage=None) -> SearchOutcome:
        return search_in_tab(bundle, query, stage=stage)

    def active_tab(self, bundle: str) -> TabInfo | None:
        return active_tab(bundle)

    def navigate_tab(self, bundle: str, url: str) -> None:
        navigate_tab(bundle, url)

