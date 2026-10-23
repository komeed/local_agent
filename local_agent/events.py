"""JSON Lines events for `local-agent run --events` and `doctor --json`. The contract is docs/APP_INTEGRATION.md."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from typing import IO, Callable

from local_agent.history import StepRecord
from local_agent.observe.models import UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import Selection

EVENTS_VERSION = 1


class EventWriter:
    """Writes one JSON object per line and flushes, so a reader sees each event as it happens."""

    def __init__(self, stream: IO[str] | None = None, clock: Callable[[], float] = time.monotonic):
        self.stream = stream or sys.stdout
        self.clock = clock
        self.t0 = clock()

    def elapsed(self) -> float:
        return round(self.clock() - self.t0, 3)

    def emit(self, type_: str, **fields) -> None:
        line = json.dumps({"type": type_, "t": self.elapsed(), **fields}, ensure_ascii=False)
        self.stream.write(line + "\n")
        self.stream.flush()


def _secs(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def plan_payload(plan: PlannerOutput) -> dict:
    d = plan.model_dump(mode="json")
    d["summary"] = plan.summary()
    return d


def element_info(el: UIElement) -> dict:
    return {
        "id": el.id,
        "role": el.role,
        "name": el.name(),
        "description": el.describe(),
        "frame": asdict(el.frame) if el.frame else None,
    }


def record_payload(rec: StepRecord) -> dict:
    return asdict(rec)


def plan_event(step: int, plan: PlannerOutput, screen: str | None, messages: list[dict] | None,
               prompt_tokens: int | None, planner_s: float | None, web: dict | None = None) -> dict:
    return {
        "step": step,
        "plan": plan_payload(plan),
        "screen": screen,
        "messages": messages or None,
        "prompt_tokens": prompt_tokens or None,
        "planner_s": _secs(planner_s),
        "web": web or None,
    }


def stage_event(step: int, stage: str, previous_elapsed_s: float | None) -> dict:
    """The start of a phase of step `step`; `elapsed_s` is how long the previous phase took (null for the first)."""
    return {"step": step, "stage": stage, "elapsed_s": _secs(previous_elapsed_s)}


def outline_event(items: list[str], seconds: float, load_s: float, rejected: str = "") -> dict:
    """The numbered steps the planner model proposed at the start (`items` is empty and `rejected` says why when the outline
    was thrown away); `load_s` is its model load time."""
    return {"items": items, "seconds": _secs(seconds), "load_s": _secs(load_s), "rejected": rejected or None}


def replan_event(reason: str, items: list[str], rejected: str = "") -> dict:
    """The steps still to do after the run got stuck (`reason`); `rejected` when the revision was discarded."""
    return {"reason": reason, "items": items, "rejected": rejected or None}


def select_event(step: int, sel: Selection, select_s: float | None) -> dict:
    return {
        "step": step,
        "source": sel.source,
        "element": element_info(sel.element),
        "probability": sel.probability,
        "confidence": sel.confidence,
        "candidates": sel.candidates,
        "rounds": sel.rounds,
        "margin": sel.margin,
        "overrode_id": sel.overrode_id,
        "select_s": _secs(select_s),
    }


def record_event(rec: StepRecord, verify_s: float | None, goal_s: float | None = None) -> dict:
    return {**record_payload(rec), "verify_s": _secs(verify_s), "goal_s": _secs(goal_s)}


def result_event(success: bool, reason: str, history: list[StepRecord], elapsed_s: float) -> dict:
    return {
        "success": success,
        "reason": reason,
        "steps": len(history),
        "elapsed_s": round(elapsed_s, 3),
        "history": [record_payload(r) for r in history],
    }
