"""Per-step execution log that is fed back to the planner."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass
class StepRecord:
    step: int
    plan: str  # PlannerOutput.summary()
    element: str | None = None  # chosen element description, if Jev was used
    probability: float | None = None  # Jev probability of the chosen option
    confidence: float | None = None  # Jev confidence (1 - normalized entropy)
    executed: bool = False
    verified: bool | None = None  # None when no verification ran
    verify_probability: float | None = None
    # Jev's probability that the screen after this step shows the whole goal done (None when not checked).
    goal_probability: float | None = None
    # Jev's probability that this step's terminal command was safe to run unasked (None: not checked / turbo).
    safety_probability: float | None = None
    note: str = ""  # error / skip reason / terminal output tail
    # Who decided `verified`: "code" (evidence code saw), "jev", "heuristic" (cosine's word-overlap check); "" when no check ran.
    verify_source: str = ""

    @property
    def succeeded(self) -> bool:
        return self.executed and self.verified is not False

    def for_planner(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v not in (None, "")}
        for k in ("probability", "confidence", "verify_probability", "goal_probability", "safety_probability"):
            if k in d:
                d[k] = round(d[k], 3)
        d.pop("verify_source", None)  # for events and logs, not for the planner's prompt
        d["result"] = "success" if self.succeeded else "failed"
        return d


# Steps that legitimately repeat: moving through a list or page one keypress / scroll at a time.
NAVIGATION_KEYS = {"up", "down", "left", "right", "tab", "shift+tab", "pageup", "pagedown", "page_up", "page_down",
                   "space", "home", "end"}  # fmt: skip
MAX_CYCLE = 3


def _navigation(plan: str) -> bool:
    return plan.startswith("scroll") or (plan.startswith("shortcut keys=") and
                                         plan.removeprefix("shortcut keys=").lower() in NAVIGATION_KEYS)  # fmt: skip


SCROLL_STUCK = "scrolled but the screen did not change"  # added to a scroll record that revealed nothing new
STUCK_SCROLLS = 4


def step_key(h: StepRecord) -> str:
    """A step by what it did, not by the numbers of the moment: the plan without [ids], plus the chosen element's role
    and name. `click [30] Dream` and `click [20] Dream` are one step; so are two different ids for the same control."""
    plan = re.sub(r"\[\d+\]\s*", "", h.plan)
    if not h.element:
        return plan
    role = h.element.split(None, 1)[0]
    title = re.search(r'title="([^"]*)"', h.element)
    label = re.search(r'label="([^"]*)"', h.element)
    return f"{plan}|{role}|{title.group(1) if title else ''}|{label.group(1) if label else ''}"


def stuck_scroll(history: list[StepRecord]) -> tuple[list[str], int]:
    """Scrolling the same way again and again while nothing new shows up counts as a repeat."""
    tail = history[-STUCK_SCROLLS:]
    if len(tail) == STUCK_SCROLLS and all(h.plan.startswith("scroll") and h.plan == tail[0].plan and h.succeeded
                                          and SCROLL_STUCK in h.note for h in tail):  # fmt: skip
        return [tail[0].plan], STUCK_SCROLLS
    return [], 0


def repeated_cycle(history: list[StepRecord]) -> tuple[list[str], int]:
    """The shortest block of up to MAX_CYCLE plans that the trailing steps repeat, all of them succeeding, and how
    many times in a row; ([], 0) when the last block wasn't done before. Catches a completed step being redone
    ("open Gmail" again) and a completed sequence being redone (cmd+t, type the URL, return; then again)."""
    if (stuck := stuck_scroll(history))[1]:
        return stuck
    for period in range(1, MAX_CYCLE + 1):
        block = [step_key(h) for h in history[-period:]]
        if len(block) < period or all(_navigation(h.plan) for h in history[-period:]):
            continue  # moving through a page or list one step at a time is not a loop (unless it shows nothing new)
        times = 0
        while len(history) >= (times + 1) * period:
            chunk = history[len(history) - (times + 1) * period: len(history) - times * period]
            if [step_key(h) for h in chunk] != block or not all(h.succeeded for h in chunk):
                break
            times += 1
        if times >= 2:
            return [h.plan for h in history[-period:]], times
    return [], 0


def loop_warning(history: list[StepRecord]) -> str | None:
    """What to tell the planner when it is going in circles: the same plan failing, or completed work being redone."""
    failed = 0
    for h in reversed(history):
        if h.succeeded or h.plan != history[-1].plan:
            break
        failed += 1
    if failed >= 2:
        return (f"Your last {failed} steps were the same plan ({history[-1].plan!r}) and all failed. "
                "Do NOT repeat it: choose a different action type, target, or a keyboard shortcut.")  # fmt: skip
    block, times = repeated_cycle(history)
    if times:
        what = repr(block[0]) if len(block) == 1 else "the steps " + ", ".join(map(repr, block))
        return (f"You already did {what} successfully {times} times in a row. The goal is probably reached: check "
                "SCREEN and answer done, or do something different. Doing it again ends the run.")  # fmt: skip
    return None
