"""JSON Lines event stream for `local-agent run --events`, the interface the macOS app reads.

One JSON object per line on stdout, flushed as it happens. See docs/APP_INTEGRATION.md for the schema; bump
EVENTS_VERSION when an existing field changes meaning or is removed (adding fields is fine).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from typing import Callable, TextIO

from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import Selection

EVENTS_VERSION = 1


def step_status(rec: StepRecord) -> str:
    """"ok", "failed" or "dry-run" — the same word the CLI prints."""
    if rec.note.startswith("dry-run"):
        return "dry-run"
    return "ok" if rec.succeeded else "failed"


def element_info(e: UIElement) -> dict:
    return {"id": e.id, "role": e.role, "name": e.name(), "description": e.describe(), "path": e.path,
            "frame": asdict(e.frame) if e.frame else None}  # fmt: skip


class TimedPlanner:
    """Wraps a planner to time each call; the inner planner's last prompt/reply are read from it."""

    def __init__(self, inner):
        self.inner = inner
        self.seconds: float | None = None
        self.screen: str | None = None  # the screen view the planner was given

    def next_step(self, goal, scope, history, screen=None) -> PlannerOutput:
        self.screen = screen
        t = time.monotonic()
        try:
            return self.inner.next_step(goal, scope, history, screen)
        finally:
            self.seconds = time.monotonic() - t


class TimedSelector:
    def __init__(self, inner):
        self.inner = inner
        self.select_seconds: float | None = None
        self.verify_seconds: float | None = None

    def select(self, goal, snap, step) -> Selection:
        t = time.monotonic()
        try:
            return self.inner.select(goal, snap, step)
        finally:
            self.select_seconds = time.monotonic() - t

    def verify(self, goal, step, element, before, after) -> float:
        t = time.monotonic()
        try:
            return self.inner.verify(goal, step, element, before, after)
        finally:
            self.verify_seconds = time.monotonic() - t


class EventStream:
    def __init__(self, out: TextIO | None = None, clock: Callable[[], float] = time.monotonic):
        self.out = out or sys.stdout
        self.clock = clock
        self.t0 = clock()

    def emit(self, type_: str, **fields) -> None:
        event = {"type": type_, "t": round(self.clock() - self.t0, 3), **fields}
        self.out.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        self.out.flush()

    def start(self, goal: str, s: Settings, dry_run: bool, target_app: str | None) -> None:
        self.emit("start", version=EVENTS_VERSION, goal=goal, dry_run=dry_run, target_app=target_app,
                  settings={k: getattr(s, k) for k in ("planner_model", "planner_mode", "planner_view",
                            "planner_num_ctx", "jev_backend", "threshold", "verify_threshold", "max_steps",
                            "max_consecutive_failures")})  # fmt: skip

    def plan(self, n: int, snap: Snapshot, plan: PlannerOutput, planner: TimedPlanner) -> None:
        inner = planner.inner
        self.emit("plan", step=n, app=snap.app, window=snap.window_title, elements=len(snap.elements),
                  plan=plan.model_dump(), summary=plan.summary(), planner_s=_round(planner.seconds),
                  prompt_tokens=getattr(inner, "last_prompt_tokens", None), screen=planner.screen,
                  messages=getattr(inner, "last_messages", None))  # fmt: skip

    def select(self, n: int, sel: Selection, selector: TimedSelector) -> None:
        self.emit("select", step=n, source=sel.source, element=element_info(sel.element),
                  probability=round(sel.probability, 4), confidence=round(sel.confidence, 4),
                  candidates=sel.candidates, rounds=sel.rounds,
                  select_s=None if sel.source == "planner" else _round(selector.select_seconds))  # fmt: skip

    def confirm(self, n: int, command: str) -> None:
        self.emit("confirm_terminal", step=n, command=command)

    def record(self, rec: StepRecord, selector: TimedSelector) -> None:
        verify_s = _round(selector.verify_seconds) if rec.verify_probability is not None else None
        self.emit("record", **asdict(rec), status=step_status(rec), verify_s=verify_s)

    def result(self, success: bool, reason: str, history: list[StepRecord]) -> None:
        self.emit("result", success=success, reason=reason, steps=len(history),
                  history=[{**asdict(r), "status": step_status(r)} for r in history])  # fmt: skip

    def error(self, message: str) -> None:
        self.emit("error", message=message)


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 3)
