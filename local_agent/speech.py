"""Short spoken lines for events (`say` / `say_kind`), written by code from what actually happened; no model writes them.

Everything is plain English in the tables below. A line is at most 60 characters, names no ids or coordinates and never
repeats private text: typed text over 24 characters, and anything typed into a password or card field, is "your text".
`Speaker` (the `--speak` flag) hands lines to macOS `say`.
"""

from __future__ import annotations

import ast
import re
import subprocess
from urllib.parse import urlparse

from local_agent.forms import SENSITIVE
from local_agent.history import StepRecord

MAX_CHARS = 60
MAX_TYPED = 24
MAX_NAME_WORDS = 4
KINDS = ("step", "question", "problem", "done")

# --- templates --------------------------------------------------------------------------------------------------------
STEP = {
    "app": "Opened {}", "site": "Opened {}", "search": "Searched for {}", "tab": "Switched to your {} tab",
    "click": "Clicked {}", "click_anon": "Clicked a control", "select": "Selected {}", "type": "Typed {}",
    "type_private": "Typed your text", "key": "Pressed {}", "scroll": "Scrolled {}", "menu": "Chose {}",
    "command": "Ran a command",
}  # fmt: skip
PROBLEM = {
    "missing": "I couldn't find {}", "blocked": "Blocked: that would close the app", "unsure": "I'm not sure what to click",
    "failed": "That step didn't work", "error": "Something went wrong",
}  # fmt: skip
QUESTION = {
    "replace": "Should I replace {}?", "submit": "Should I submit the form?",
    "leave": "Should I leave this page and lose changes?", "command": "Should I run this command?",
}  # fmt: skip
RESULT = {
    "done": "Done", "declined": "Stopped: you declined", "needs": "I need {} to fill the form",
    "stuck": "I got stuck on {}", "stopped": "Stopped after {} steps",
}  # fmt: skip
KEY_NAMES = {"cmd": "Command", "command": "Command", "ctrl": "Control", "control": "Control", "alt": "Option", "option": "Option",
             "shift": "Shift", "return": "Return", "enter": "Return", "escape": "Escape", "esc": "Escape", "tab": "Tab",
             "space": "Space", "delete": "Delete", "backspace": "Delete", "up": "Up arrow", "down": "Down arrow",
             "left": "Left arrow", "right": "Right arrow"}  # fmt: skip


def clamp(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) <= MAX_CHARS:
        return line
    cut = line[:MAX_CHARS]
    return (cut.rsplit(" ", 1)[0] if " " in cut[MAX_CHARS // 2:] else cut).rstrip(" ,;:")


def short_name(name: str) -> str:
    """The name cut at the first ":", " - ", "•" or "(" and capped at 4 words: "Model: Opus 5.5 High" -> "Model"."""
    head = re.split(r":|\s[-–—]\s|•|\(|…", name or "", maxsplit=1)[0]
    return " ".join(head.split()[:MAX_NAME_WORDS])


def spoken_keys(keys: str) -> str:
    parts = [p for p in re.split(r"\+", keys.strip().lower()) if p]
    return " ".join(KEY_NAMES.get(p, p.upper() if len(p) == 1 else p.capitalize()) for p in parts)


def _say(line: str, kind: str) -> tuple[str, str]:
    return clamp(line), kind


def _element_name(rec: StepRecord) -> str:
    """The clicked control's name: its title, else label, else placeholder, else what the plan called it."""
    desc = rec.element or ""
    for key in ("title", "label", "placeholder"):
        if m := re.search(rf'{key}="([^"]*)"', desc):
            return m.group(1)
    rest = re.split(r" (?:text|keys|command|menu)=", rec.plan, maxsplit=1)[0]
    rest = re.sub(r"^\w+|\[\d+\]|\[AX\w+\]", " ", rest)
    return " ".join(rest.split())


def _plan_text(plan: str) -> str:
    m = re.search(r"text=('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", plan)
    try:
        return str(ast.literal_eval(m.group(1))) if m else ""
    except (ValueError, SyntaxError):
        return ""


def _command(plan: str) -> str:
    m = re.search(r"(?:command|menu)=('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", plan)
    try:
        return str(ast.literal_eval(m.group(1))) if m else ""
    except (ValueError, SyntaxError):
        return ""


def _problem(rec: StepRecord) -> tuple[str, str]:
    note = rec.note
    if m := re.search(r'no element named "([^"]+)"', note):
        return _say(PROBLEM["missing"].format(short_name(m.group(1)) or m.group(1)), "problem")
    if "would close/quit/switch" in note:
        return _say(PROBLEM["blocked"], "problem")
    if note.startswith("unsure"):
        return _say(PROBLEM["unsure"], "problem")
    return _say(PROBLEM["failed"], "problem")


def record_say(rec: StepRecord) -> tuple[str, str] | None:
    """(line, kind) for a step record, or None for dry-run and skipped records."""
    plan, note = rec.plan, rec.note
    if note.startswith("dry-run") or plan in ("done", "leave", "overwrite"):
        return None  # dry runs speak nothing; a rejected done and the agent's own dialog answers are not steps
    if plan == "planner error":
        return _problem(rec)
    action = plan.split(" ", 1)[0]
    if not rec.succeeded:
        if not rec.executed and not note:
            return None
        return _problem(rec)
    command = _command(plan)
    if action == "terminal":
        if note.startswith("opened by the agent"):
            if m := re.match(r'open -a "([^"]+)"', command):
                return _say(STEP["app"].format(m.group(1)), "step")
            if m := re.search(r'names the tab "([^"]+)"', note):
                return _say(STEP["tab"].format(m.group(1)), "step")
            if m := re.match(r'\(search for "([^"]+)"', command):
                return _say(STEP["search"].format(m.group(1)), "step")
            if m := re.search(r"goal names site ([^\s);]+)", note):
                return _say(STEP["site"].format(_site_label(m.group(1))), "step")
        if m := re.match(r'\(switch to the "?([^")]+?)"? tab\)', command):
            return _say(STEP["tab"].format(short_name(m.group(1))), "step")
        return _say(STEP["command"], "step")
    if action in ("click", "double_click"):
        if (rec.element or "").startswith("AXMenuItem"):
            return _say(STEP["select"].format(_element_name(rec)), "step")
        name = short_name(_element_name(rec))
        return _say(STEP["click"].format(name) if name else STEP["click_anon"], "step")
    if action == "menu":
        return _say(STEP["menu"].format(command.split(">")[-1].strip()), "step")
    if action == "type":
        text = _plan_text(plan)
        private = SENSITIVE.search(rec.element or "") or "AXSecureTextField" in (rec.element or "")
        if private or not text or len(text) > MAX_TYPED:
            return _say(STEP["type_private"], "step")
        return _say(STEP["type"].format(text), "step")
    if action == "shortcut":
        m = re.search(r"keys=(\S+)", plan)
        return _say(STEP["key"].format(spoken_keys(m.group(1))), "step") if m else None
    if action == "scroll":
        return _say(STEP["scroll"].format("up" if plan.endswith("up") else "down"), "step")
    return None


def _site_label(site: str) -> str:
    host = (urlparse(site if "//" in site else "//" + site).hostname or site).removeprefix("www.")
    return host.split(".")[0] if "." in host else host


def question_say(command: str, reason: str = "") -> tuple[str, str]:
    """(line, "question") for a confirm_terminal event."""
    if m := re.match(r'replace existing "(.+)"', command):
        return _say(QUESTION["replace"].format(m.group(1)), "question")
    if command.startswith("submit the form"):
        return _say(QUESTION["submit"], "question")
    if command.startswith("leave the page"):
        return _say(QUESTION["leave"], "question")
    return _say(QUESTION["command"], "question")


def result_say(success: bool, reason: str, steps: int) -> tuple[str, str] | None:
    if reason == "cancelled":
        return None
    if success:
        return _say(RESULT["done"], "done")
    if reason.startswith("needs_input:"):
        labels = [x.strip() for x in reason.split(":", 1)[1].split(",") if x.strip()]
        shown = labels[:1]
        for label in labels[1:]:  # as many labels as fit in the line
            if len(RESULT["needs"].format(", ".join([*shown, label]))) <= MAX_CHARS:
                shown.append(label)
        return _say(RESULT["needs"].format(", ".join(shown)), "problem")
    if "declined" in reason:
        return _say(RESULT["declined"], "problem")
    if m := re.match(r"(?:stuck|unsure): (.+)", reason):
        item = m.group(1)
        room = MAX_CHARS - len(RESULT["stuck"].format(""))
        return _say(RESULT["stuck"].format(item if len(item) <= room else item[: room - 1].rstrip() + "…"), "problem")
    return _say(RESULT["stopped"].format(steps), "problem")


def error_say() -> tuple[str, str]:
    return _say(PROBLEM["error"], "problem")


def fields(pair: tuple[str, str] | None) -> dict:
    """The optional event fields for a (line, kind) pair; empty when there is no line."""
    return {} if pair is None else {"say": pair[0], "say_kind": pair[1]}


class Speaker:
    """`--speak`: macOS `say`, never blocking. A new step line stops a step line still being spoken; questions, problems
    and the final line are never cut off by a step line."""

    def __init__(self, rate: int = 200, popen=None):
        self.rate, self.popen, self._step = rate, popen, None

    def speak(self, pair: tuple[str, str] | None) -> None:
        if pair is None:
            return
        line, kind = pair
        if kind == "step" and self._step is not None and self._step.poll() is None:
            self._step.terminate()
        try:
            proc = (self.popen or subprocess.Popen)(["say", "-r", str(self.rate), line])
        except OSError:
            return  # no `say` (not macOS): stay silent
        if kind == "step":
            self._step = proc
