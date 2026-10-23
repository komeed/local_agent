"""Goal-completion eval cases: a goal, the screen after some step, and whether that screen shows the goal done.

`JevClient.goal_done` ends a run at goal_done_threshold, so the costly mistake is a high probability on a screen
that is NOT done (a false stop: half-done multi-part goals, pages still loading, a sign-in page, the wrong site). A low
probability on a done screen only costs extra steps (the planner and the loop guard still end the run).

Arc / Gmail / Settings / Finder screens are synthetic, shaped like their accessibility trees (web content under an
AXWebArea, the page title as the window title); TextEdit ones reuse the recorded fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from evals.planner_cases import fixture, te_empty
from evals.selector_cases import _el, _menubar
from local_agent.observe.context import add_context
from local_agent.observe.models import Snapshot

GMAIL = "Open my Gmail in Arc"
COMPOSE = "Open Gmail in Arc and start a new email to bob@example.com"


def arc(title: str, url: str, page: Callable[[Snapshot, str], None], *, command_bar: str | None = None,
        tabs: tuple[str, ...] = ("Google Calendar", "Hacker News")) -> Snapshot:  # fmt: skip
    """Arc with `title` in the selected tab and `page` filling its web area. command_bar: the open new-tab bar's text."""
    snap = Snapshot("Arc", "company.thebrowser.Browser", 0, title, [])
    _menubar(snap, "Arc", "File", "Edit", "View", "Spaces", "Tabs", "Archive", "Extensions", "Window", "Help")
    side = f"AXWindow:{title} > AXGroup:Sidebar"
    for i, name in enumerate(["Toggle Sidebar", "Back", "Forward", "Reload"]):
        _el(snap, "AXButton", f"{side} > AXToolbar", 10 + 30 * i, 40, 24, label=name)
    _el(snap, "AXTextField", side, 10, 70, 220, 26, value=url, placeholder="Search or Enter URL…")
    _el(snap, "AXButton", side, 10, 100, 220, 26, label="New Tab")
    for i, name in enumerate((title, *tabs) if title != "New Tab" else tabs):
        _el(snap, "AXRow", f"{side} > AXOutline:Tabs", 10, 140 + 30 * i, 220, 28, label=name)
    web = f"AXWindow:{title} > AXGroup > AXScrollArea > AXWebArea:{title}"
    page(snap, web)
    if command_bar is not None:
        bar = f"AXWindow:{title} > AXGroup:Command Bar"
        _el(snap, "AXTextField", bar, 400, 200, 500, 30, value=command_bar, placeholder="Search or Enter URL…",
            focused=True)  # fmt: skip
        for i, s in enumerate(["mail.google.com — Gmail", "gmail.com", "Search Google for “gmail”"]):
            _el(snap, "AXRow", f"{bar} > AXTable", 400, 240 + 30 * i, 500, 28, label=s)
    add_context(snap.elements)
    return snap


def gmail_inbox(snap: Snapshot, web: str) -> None:
    _el(snap, "AXButton", web, 270, 80, 140, 40, title="Compose")
    _el(snap, "AXTextField", web, 500, 20, 600, 40, placeholder="Search mail")
    for i, name in enumerate(["Inbox 3", "Starred", "Snoozed", "Sent", "Drafts 1", "More"]):
        _el(snap, "AXLink", web, 270, 140 + 32 * i, 160, 28, title=name)
    for i, (who, subject) in enumerate([("Shop", "Your order has shipped"), ("Dana", "Team lunch Friday?"),
                                        ("GitHub", "[local_llm] CI passed"), ("Calendar", "Invitation: 1:1")]):  # fmt: skip
        y = 140 + 40 * i
        _el(snap, "AXRow", f"{web} > AXTable", 460, y, 900, 38)
        _el(snap, "AXStaticText", f"{web} > AXTable > AXRow", 470, y + 10, 150, value=who)
        _el(snap, "AXStaticText", f"{web} > AXTable > AXRow", 640, y + 10, 500, value=subject)


def gmail_compose(snap: Snapshot, web: str, to: str = "bob@example.com") -> None:
    gmail_inbox(snap, web)
    box = f"{web} > AXGroup:New Message"
    _el(snap, "AXHeading", box, 900, 400, 200, value="New Message")
    _el(snap, "AXTextField", box, 900, 440, 400, 26, label="To recipients", value=to)
    _el(snap, "AXTextField", box, 900, 470, 400, 26, placeholder="Subject")
    _el(snap, "AXTextArea", box, 900, 500, 400, 200, label="Message Body")
    _el(snap, "AXButton", box, 900, 720, 80, 30, title="Send")


def gmail_loading(snap: Snapshot, web: str) -> None:
    _el(snap, "AXStaticText", web, 600, 400, 300, value="Loading you@gmail.com…")


def google_sign_in(snap: Snapshot, web: str) -> None:
    _el(snap, "AXHeading", web, 600, 200, 200, value="Sign in")
    _el(snap, "AXStaticText", web, 600, 240, 200, value="to continue to Gmail")
    _el(snap, "AXTextField", web, 600, 300, 300, 40, label="Email or phone")
    _el(snap, "AXLink", web, 600, 350, 160, title="Forgot email?")
    _el(snap, "AXButton", web, 800, 420, 80, 36, title="Next")


def hacker_news(snap: Snapshot, web: str) -> None:
    _el(snap, "AXLink", web, 270, 20, 120, title="Hacker News")
    for i, story in enumerate(["Show HN: A local desktop agent", "The unreasonable effectiveness of ties",
                               "Ask HN: What are you working on?"]):  # fmt: skip
        _el(snap, "AXLink", web, 300, 60 + 30 * i, 500, title=story)


def blank(snap: Snapshot, web: str) -> None:
    pass


def textedit_bold(on: bool) -> Snapshot:
    snap = fixture("te_rich")
    for e in snap.elements:
        if e.label == "bold":
            e.value = "1" if on else "0"
        if e.label == "style":
            e.value = "Bold" if on else "Regular"
    return snap


def settings_appearance(dark: bool) -> Snapshot:
    snap = Snapshot("System Settings", "com.apple.systempreferences", 0, "Appearance", [])
    _menubar(snap, "System Settings", "Edit", "View", "Window", "Help")
    side = "AXWindow:Appearance > AXGroup > AXScrollArea > AXOutline:Sidebar"
    for i, name in enumerate(["Wi‑Fi", "Bluetooth", "General", "Appearance", "Displays", "Wallpaper"]):
        _el(snap, "AXRow", side, 10, 100 + 32 * i, 200, 30, label=name)
    pane = "AXWindow:Appearance > AXGroup > AXScrollArea > AXGroup"
    _el(snap, "AXStaticText", pane, 260, 100, 120, value="Appearance")
    for i, name in enumerate(["Auto", "Light", "Dark"]):
        selected = (name == "Dark") == dark if name != "Auto" else False
        _el(snap, "AXRadioButton", pane, 400 + 90 * i, 100, 80, 60, title=name, value="1" if selected else "0")
    _el(snap, "AXStaticText", pane, 260, 200, 120, value="Accent color")
    _el(snap, "AXPopUpButton", pane, 400, 200, 160, value="Multicolor")
    add_context(snap.elements)
    return snap


def finder(folder: str) -> Snapshot:
    snap = Snapshot("Finder", "com.apple.finder", 0, folder, [])
    _menubar(snap, "Finder", "File", "Edit", "View", "Go", "Window", "Help")
    side = f"AXWindow:{folder} > AXSplitGroup > AXScrollArea > AXOutline:Sidebar"
    for i, name in enumerate(["Recents", "Applications", "Desktop", "Documents", "Downloads"]):
        _el(snap, "AXRow", side, 10, 80 + 28 * i, 150, 26, label=name)
    files = {"Downloads": ["invoice-0425.pdf", "setup.dmg", "photo.heic"], "Documents": ["Notes.txt", "Taxes 2025"]}
    for i, name in enumerate(files[folder]):
        _el(snap, "AXRow", f"AXWindow:{folder} > AXSplitGroup > AXScrollArea > AXOutline", 200, 80 + 24 * i, 500, 22,
            label=name)  # fmt: skip
    add_context(snap.elements)
    return snap


@dataclass
class GoalCase:
    name: str
    goal: str
    snap: Callable[[], Snapshot]
    done: bool  # does this screen show the goal accomplished?


GOAL_CASES: list[GoalCase] = [
    GoalCase("gmail_inbox", GMAIL, lambda: arc("Inbox (3) - you@gmail.com - Gmail", "mail.google.com/mail/u/0/#inbox",
                                                gmail_inbox), True),
    GoalCase("gmail_new_tab", GMAIL, lambda: arc("New Tab", "", blank, command_bar=""), False),
    GoalCase("gmail_typed_not_opened", GMAIL, lambda: arc("New Tab", "", blank, command_bar="gmail.com"), False),
    GoalCase("gmail_loading", GMAIL, lambda: arc("Gmail", "mail.google.com/mail/u/0/", gmail_loading), False),
    GoalCase("gmail_sign_in", GMAIL, lambda: arc("Sign in - Google Accounts", "accounts.google.com/signin",
                                                 google_sign_in), False),
    GoalCase("gmail_wrong_site", GMAIL, lambda: arc("Hacker News", "news.ycombinator.com", hacker_news), False),
    GoalCase("compose_only_inbox", COMPOSE, lambda: arc("Inbox (3) - you@gmail.com - Gmail",
                                                        "mail.google.com/mail/u/0/#inbox", gmail_inbox), False),
    GoalCase("compose_no_recipient", COMPOSE, lambda: arc("Inbox (3) - you@gmail.com - Gmail",
                                                          "mail.google.com/mail/u/0/#inbox?compose=new",
                                                          lambda s, w: gmail_compose(s, w, to="")), False),
    GoalCase("compose_to_bob", COMPOSE, lambda: arc("Inbox (3) - you@gmail.com - Gmail",
                                                    "mail.google.com/mail/u/0/#inbox?compose=new", gmail_compose), True),
    GoalCase("textedit_bold_on", "Make the text Hello world bold in TextEdit", lambda: textedit_bold(True), True),
    GoalCase("textedit_bold_off", "Make the text Hello world bold in TextEdit", lambda: textedit_bold(False), False),
    GoalCase("textedit_typed", "Type Hello world into the TextEdit document", lambda: fixture("te_rich"), True),
    GoalCase("textedit_not_typed", "Type Hello world into the TextEdit document", te_empty, False),
    GoalCase("settings_dark_on", "Turn on dark mode", lambda: settings_appearance(True), True),
    GoalCase("settings_dark_off", "Turn on dark mode", lambda: settings_appearance(False), False),
    GoalCase("finder_downloads", "Open the Downloads folder in Finder", lambda: finder("Downloads"), True),
    GoalCase("finder_documents", "Open the Downloads folder in Finder", lambda: finder("Documents"), False),
]  # fmt: skip
