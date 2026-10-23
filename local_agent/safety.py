"""Terminal command safety. The planner's commands run without asking unless one of these holds:

1. the command matches ALWAYS_ASK (no model gets to wave these through: admin rights, recursive deletes, disk
   erasure, piping downloads into a shell, ...);
2. Jev rates it below `safety_threshold` on SAFETY_QUESTION, given the goal, the planner's reason and the screen;
3. the Jev backend can't judge (cosine) or errors.

Then the user is asked. `--turbo` skips all of it.
"""

from __future__ import annotations

import re

ALWAYS_ASK: list[tuple[str, str]] = [
    (r"(^|[;&|(]\s*|\s)(sudo|su|doas)\s", "needs administrator rights"),
    (r"\brm\s+(-\w*[rR]\w*|--recursive)", "deletes folders recursively"),
    (r"\brm\b.*\s(/|~|\$HOME)/?(\*|\s|$)", "deletes from the root or home folder"),
    (r"\b(mkfs|fdisk|newfs)\b|\bdiskutil\s+(erase|partition|reformat|zero|secureErase|apfs\s+delete)", "erases disks"),
    (r"\bdd\s+.*\bof=", "writes raw data with dd"),
    (r"\b(shutdown|reboot|halt)\b|\bpmset\s", "shuts down or changes power settings"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?|perl|ruby)\b", "runs a downloaded script"),
    (r"\b(chmod|chown|chgrp)\s+(-\w*R|--recursive)", "changes permissions recursively"),
    (r">\s*/(etc|usr|bin|sbin|System|Library|private)/", "writes into system folders"),
    (r"\b(killall|pkill)\b|\bkill\s+-(9|KILL)\b", "stops processes"),
    (r"\b(csrutil|spctl|tccutil|launchctl|crontab|systemsetup|networksetup|scutil)\b", "changes system configuration"),
    (r"\bsecurity\s+(find-\w*-password|dump-keychain|delete-)", "reads or deletes keychain secrets"),
    (r"~/\.ssh|\$HOME/\.ssh|/\.ssh/", "touches SSH keys"),
    (r"\bgit\b.*\s(push\b.*\s(-f\b|--force)|reset\s+--hard|clean\s+-\w*f)", "rewrites or discards git history/files"),
    (r":\(\)\s*\{", "fork bomb"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in ALWAYS_ASK]

SAFETY_QUESTION = (
    "Is it safe to run this shell command without asking the user? Answer true only if it just opens an app, file "
    "or URL, only reads or lists information, or makes a small, easily undone change that the user's goal clearly "
    "asks for (for example creating a folder the goal names, or moving a file the goal names to the Trash). Answer "
    "false if it deletes or overwrites files, changes system settings, permissions or other apps' data, installs, "
    "downloads or runs software, sends data over the network, stops processes, needs administrator rights, or does "
    "anything the goal does not ask for."
)


def always_ask(command: str) -> str | None:
    """Why this command needs the user's OK whatever Jev says, or None."""
    for rx, why in _COMPILED:
        if rx.search(command):
            return why
    return None


def safety_state(goal: str, command: str, reasoning: str, app: str, window: str, recent: list[str]) -> str:
    steps = "\n".join(f"- {p}" for p in recent) or "- (none yet)"
    return (
        f"User goal: {goal}\n"
        f"Planner's reason for this step: {reasoning}\n"
        f"App on screen: {app} — window {window!r}\n"
        f"Steps so far:\n{steps}\n"
        f"Command to run (zsh, as the user, from the home folder):\n{command}"
    )
