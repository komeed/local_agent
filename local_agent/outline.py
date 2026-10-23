"""Plan once, revise when stuck: a short numbered outline for goals the facts can't cover (docs/PLAN_browser.md side note).

One call to the Ollama planner model at the start turns the goal into 3-8 steps; each step becomes a checklist item.
A step that parses into a known fact (app, site, search, text, saved, new) gets that fact's check; any other step is an
`outline` item judged done by Jev (`goal_done` over the screen, p >= 0.9) or, without a judge, by a verified step whose
plan shares a word with it. When the run gets stuck the model is asked again for the remaining steps, at most
`MAX_REPLANS` times. With `Settings.low_memory` the model is unloaded after every call (`keep_alive` 0), so only JevK5
stays resident."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx

from local_agent.goal_facts import Fact, parse_facts
from local_agent.history import StepRecord

MAX_REPLANS = 3
MAX_STEPS = 8
OUTLINE_SYSTEM = (
    "You plan work on a Mac. Reply with a numbered list of 3 to 8 short steps (one action each, in order) that "
    "accomplish the user's goal. Name the app or website to use. Each step is one short line. No explanations, "
    "no extra text."
)
REPLAN_SYSTEM = (
    "You plan work on a Mac. The user's goal is only partly done. Reply with a numbered list of the steps still to do "
    "(1 to 8, one short line each, in order), changing the approach of the step that failed. No explanations."
)
NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*(.+?)\s*$")


def parse_outline(text: str) -> list[str]:
    steps = []
    for line in (text or "").splitlines():
        if m := NUMBERED.match(line):
            step = re.sub(r"\*\*|`", "", m.group(1)).strip().rstrip(".")
            if step:
                steps.append(step)
    return steps[:MAX_STEPS]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def validate_outline(steps: list[str], goal: str, apps: list[str], done_values: set[str] = frozenset(),
                     min_items: int = 2) -> tuple[list[str], str]:
    """(cleaned steps, "") or ([], why it was rejected). Drops exact and near duplicate lines (ratio >= 0.9) and lines that
    only repeat something already done ("Open Arc" once Arc is open). Rejects the whole list when more than half of it
    repeated itself, when fewer than `min_items` distinct lines remain, or when a line names an app that isn't installed
    ("the YouTube app") while the goal names a website."""
    from difflib import SequenceMatcher

    from local_agent.apps import resolve_app

    distinct: list[str] = []
    for step in steps:
        n = _norm(step)
        if any(n == _norm(d) or SequenceMatcher(None, n, _norm(d)).ratio() >= 0.9 for d in distinct):
            continue
        distinct.append(step)
    if steps and (len(steps) - len(distinct)) * 2 > len(steps):
        return [], f"{len(steps) - len(distinct)} of {len(steps)} lines repeated each other"
    names_site = any(f.kind == "site" for f in parse_facts(goal, apps))
    for step in distinct:
        m = re.search(r"\b((?:[A-Z][\w+.-]*\s+){0,2}[A-Z][\w+.-]*)\s+app\b", step)
        if m and names_site and not resolve_app(m.group(1), apps):
            return [], f'a line names an app that is not installed ("{m.group(1)} app") while the goal names a site'
    cleaned = []
    for step in distinct:
        facts = parse_facts(step, apps, progress=True)
        if facts and all(f.kind in ("app", "site") and f.value.lower() in done_values for f in facts):
            continue  # only repeats a done fact
        cleaned.append(step)
    if len(distinct) < min_items or not cleaned:
        return [], f"fewer than {min_items} distinct items"
    return cleaned, ""


def single_action(goal: str, facts: list[Fact]) -> bool:
    """One short clause on an app or site ("on X click Y", "open X and search Y"): the facts and the planner cover it."""
    from local_agent.goal_facts import rest_of_goal

    if not any(f.kind in ("app", "site") for f in facts):
        return False
    rest = rest_of_goal(goal, facts)
    return len(rest.split()) <= 8 and not re.search(r"\b(?:then|after|afterwards|next|finally)\b|[;,]", goal, re.I)


def items_from_steps(steps: list[str], apps: list[str]) -> list[Fact]:
    """Checklist facts for outline lines: the facts a line parses into, else an `outline` item for the line."""
    out: list[Fact] = []
    for i, step in enumerate(steps):
        facts = [f for f in parse_facts(step, apps, progress=True) if f.kind != "click"]  # clicks stay outline items
        for f in facts or [Fact("outline", step)]:
            if f not in out:
                out.append(Fact(f.kind, f.value, i, alias=f.alias, data=f.data, meta=f.meta))
    return out


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())} - {"the", "and", "for", "with", "into", "step"}


def plan_matches(step: str, rec: StepRecord) -> bool:
    """Cosine fallback: a verified step whose plan or element shares a word with the outline line."""
    return bool(words(step) & words(f"{rec.plan} {rec.element or ''}"))


def outline_backend(settings) -> str:
    """"planner" or "jev": the explicit setting, else jev only in jev + low-memory mode (one resident model)."""
    explicit = getattr(settings, "outline_model", None)
    if explicit in ("planner", "jev"):
        return explicit
    return "jev" if getattr(settings, "planner_mode", "") == "jev" and getattr(settings, "low_memory", False) else "planner"


@dataclass
class Outliner:
    settings: object
    client: httpx.Client | None = None
    last_seconds: float = 0.0
    last_text: str = ""  # the raw reply of the last call
    last_load_s: float = 0.0  # Ollama's load_duration: what a cold model costs per call under low_memory
    calls: list = field(default_factory=list)

    def _chat_jev(self, system: str, user: str) -> list[str]:
        """The same prompt as a plain chat completion on the JevK5 llama-server (thinking off, temperature 0)."""
        s = self.settings
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 300, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }  # fmt: skip
        t0 = time.monotonic()
        client = self.client or httpx.Client(base_url=s.jevk5_url, timeout=s.http_timeout_s)
        r = client.post("/v1/chat/completions", json=body)
        r.raise_for_status()
        self.last_seconds, self.last_load_s = time.monotonic() - t0, 0.0  # the server stays resident: no load
        self.last_text = r.json()["choices"][0]["message"]["content"]
        self.calls.append({"seconds": round(self.last_seconds, 2), "load_s": 0.0, "backend": "jev"})
        return parse_outline(self.last_text)

    def _chat(self, system: str, user: str) -> list[str]:
        s = self.settings
        if outline_backend(s) == "jev":
            return self._chat_jev(system, user)
        body = {
            "model": s.planner_model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_ctx": 4096},
        }  # fmt: skip
        if getattr(s, "low_memory", False):
            body["keep_alive"] = 0  # unload right after the answer: only JevK5 stays resident
        t0 = time.monotonic()
        client = self.client or httpx.Client(base_url=s.ollama_url, timeout=s.http_timeout_s)
        r = client.post("/api/chat", json=body)
        r.raise_for_status()
        data = r.json()
        self.last_seconds = time.monotonic() - t0
        self.last_text = data["message"]["content"]
        self.last_load_s = float(data.get("load_duration", 0)) / 1e9
        self.calls.append({"seconds": round(self.last_seconds, 2), "load_s": round(self.last_load_s, 2), "keep_alive": body.get("keep_alive")})
        return parse_outline(data["message"]["content"])

    def outline(self, goal: str, context: str = "") -> list[str]:
        return self._chat(OUTLINE_SYSTEM, f"Goal: {goal}" + (f"\n{context}" if context else ""))

    def replan(self, goal: str, done: list[str], failed: str, history_lines: str) -> list[str]:
        user = (f"Goal: {goal}\nDone: {'; '.join(done) or 'nothing yet'}\nFailed step: {failed}\n"
                f"Last steps and what happened:\n{history_lines}")  # fmt: skip
        return self._chat(REPLAN_SYSTEM, user)


def make_outliner(settings) -> Outliner | None:
    return Outliner(settings) if getattr(settings, "outline", True) else None
