"""Record real TextEdit snapshots used by evals/planner_cases.py.

    uv run python -m evals.capture_fixtures

Drives TextEdit on this Mac (creates a scratch document, opens menus and the Save sheet, then closes the document
without saving) and writes evals/fixtures/*.json. Needs Accessibility permission.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ApplicationServices import AXUIElementPerformAction

from local_agent.act.executor import press_shortcut
from local_agent.config import Settings
from local_agent.observe.ax_tree import activate_app, snapshot
from local_agent.observe.models import Snapshot

FIXTURES = Path(__file__).parent / "fixtures"
SETTLE = 0.7


def osa(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True).stdout.strip()


def take(name: str, s: Settings) -> Snapshot:
    time.sleep(SETTLE)
    snap = snapshot(s)
    data = snap.to_dict()
    data["pid"] = 0
    (FIXTURES / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"{name}: {len(snap.elements)} elements ({sum(not e.interactive for e in snap.elements)} text)")
    return snap


def press(snap: Snapshot, role: str, title: str) -> None:
    el = next(e for e in snap.elements if e.role == role and e.title == title)
    AXUIElementPerformAction(el.ref, "AXPress")


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    s = Settings()
    activate_app("TextEdit")
    osa('tell application "TextEdit" to make new document with properties {text:"Hello world"}')
    activate_app("TextEdit")
    try:
        plain = take("te_plain", s)
        press(plain, "AXMenuBarItem", "Format")
        plain_format = take("te_plain_format", s)
        assert any(e.title == "Make Rich Text" for e in plain_format.elements), "new TextEdit docs should be plain"
        press(plain_format, "AXMenuItem", "Make Rich Text")
        time.sleep(SETTLE)
        press_shortcut("cmd+a")
        rich = take("te_rich", s)
        press(rich, "AXMenuBarItem", "Format")
        rich_format = take("te_rich_format", s)
        press(rich_format, "AXMenuItem", "Font")
        take("te_rich_font", s)
        press_shortcut("escape")
        press_shortcut("escape")
        time.sleep(SETTLE)
        press_shortcut("cmd+s")
        take("te_save_sheet", s)
        press_shortcut("escape")
    finally:
        time.sleep(SETTLE)
        osa('tell application "TextEdit" to close document 1 saving no')


if __name__ == "__main__":
    main()
