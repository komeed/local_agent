"""Steps that close, quit, hide or switch away from apps or windows, which a goal must ask for in words.

General by structure: key chords and menu/button names (Close…, Quit…, Hide…, Minimize), and shell commands that kill
processes. No app names."""

from __future__ import annotations

import re

from local_agent.act.keys import parse_shortcut

CHORDS = {
    "close": ["cmd+w", "cmd+shift+w", "cmd+option+w"],
    "quit": ["cmd+q", "cmd+option+q"],
    "switch": ["cmd+tab", "cmd+shift+tab", "ctrl+left", "ctrl+right"],
    "hide": ["cmd+h", "cmd+option+h"],
    "minimize": ["cmd+m"],
}
# What the goal has to say (whole words) for a family to be allowed.
GOAL_WORDS = {
    "close": r"close|closing",
    "quit": r"quit|exit|kill|terminate",
    "switch": r"switch|switching|go back to|alt-tab",
    "hide": r"hide|hiding",
    "minimize": r"minimi[sz]e|minimi[sz]ing",
}
NAME_START = {
    "close": r"close\b",
    "quit": r"quit\b",
    "hide": r"hide\b",
    "minimize": r"minimi[sz]e\b",
}
KILL_COMMAND = re.compile(r"\b(killall|pkill|kill)\b|\bosascript\b.*\bquit\b", re.I)

_BY_CHORD = {}
for _fam, _keys in CHORDS.items():
    for _k in _keys:
        _BY_CHORD[parse_shortcut(_k)] = _fam


def chord_family(keys: str) -> str | None:
    try:
        return _BY_CHORD.get(parse_shortcut(keys))
    except ValueError:
        return None


def name_family(name: str) -> str | None:
    """"Close Window ⌘W", "Quit Safari", "Hide Others", "Minimize" -> the family; None for anything else."""
    text = re.split(r"[⌘⌥⇧⌃]", name)[0].replace("…", "").replace("...", "").strip().lower()
    for fam, pattern in NAME_START.items():
        if re.match(pattern, text):
            return fam
    return None


def command_family(command: str) -> str | None:
    return "quit" if KILL_COMMAND.search(command or "") else None


def goal_allows(goal: str, family: str) -> bool:
    return re.search(rf"\b(?:{GOAL_WORDS[family]})\b", goal.lower()) is not None
