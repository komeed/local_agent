"""Find the installed app a planner means by name: `open -a "Arc Browser"` should open Arc, `open -a chrome` Google
Chrome. Pure filesystem and string matching, so it's testable without PyObjC."""

from __future__ import annotations

import re
import shlex
from difflib import SequenceMatcher
from pathlib import Path

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path("~/Applications").expanduser(),
]
# Words planners add to app names ("Arc Browser", "the Notes app").
FILLER = {"app", "application", "browser", "the", "macos", "mac", "apple"}
# Old or nick names that share no word with the app's real name.
ALIASES = {"system preferences": "System Settings", "settings": "System Settings", "vscode": "Visual Studio Code",
           "vs code": "Visual Studio Code", "itunes": "Music", "ical": "Calendar", "address book": "Contacts",
           "word": "Microsoft Word", "excel": "Microsoft Excel", "powerpoint": "Microsoft PowerPoint"}  # fmt: skip


def installed_apps(dirs: list[Path] = APP_DIRS) -> list[str]:
    names = []
    for d in dirs:
        try:
            names += [p.stem for p in d.iterdir() if p.suffix == ".app"]
        except OSError:
            continue
    return sorted(set(names))


def _key(name: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower().removesuffix(".app")) if w not in FILLER]
    return " ".join(words)


def resolve_app(name: str, apps: list[str]) -> str | None:
    """The installed app `name` most likely means, or None. Exact (case-insensitive) beats filler-stripped match
    beats a whole-word match beats fuzzy similarity."""
    want = name.strip().removesuffix(".app")
    lower = {a.lower(): a for a in apps}
    if want.lower() in lower:
        return lower[want.lower()]
    alias = ALIASES.get(want.lower())
    if alias and alias in apps:
        return alias
    key = _key(want)
    if not key:
        return None
    keyed = {_key(a): a for a in apps}
    if key in keyed:
        return keyed[key]
    # "chrome" -> "Google Chrome", "vscode" won't match here but "visual studio code" would.
    words = [a for a in apps if set(key.split()) <= set(_key(a).split())]
    if len(words) == 1:
        return words[0]
    best = max(apps, key=lambda a: SequenceMatcher(None, key, _key(a)).ratio(), default=None)
    # Typos only: "gmail" vs "mail" is 0.89 and must not open Mail.
    if best is not None and SequenceMatcher(None, key, _key(best)).ratio() >= 0.9:
        return best
    return None


def opened_app(command: str) -> str | None:
    """The app name in a single `open -a <App> [args]` command, or None for anything else."""
    if any(c in command for c in ";&|\n`$("):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) >= 3 and tokens[0] == "open" and "-a" in tokens:
        i = tokens.index("-a")
        return tokens[i + 1] if i + 1 < len(tokens) else None
    return None


def rewrite_open(command: str, apps: list[str]) -> tuple[str, str | None]:
    """(command with the app name resolved to an installed app, note), for `open -a` commands naming an app that
    isn't installed under that exact name. Unchanged (with no note) otherwise."""
    name = opened_app(command)
    if name is None or name.lower() in {a.lower() for a in apps}:
        return command, None
    real = resolve_app(name, apps)
    if real is None:
        return command, None
    tokens = shlex.split(command)
    tokens[tokens.index("-a") + 1] = real
    return shlex.join(tokens), f"opened {real!r} for {name!r}"
