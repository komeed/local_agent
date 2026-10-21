"""The agent loop: observe -> plan (Qwen) -> select (Jev) -> act -> verify (Jev) -> feed back to Qwen."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.qwen import PlannerError
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import JevError, Selection


class Planner(Protocol):
    def next_step(self, goal: str, scope: dict, history: list[StepRecord]) -> PlannerOutput: ...


class Selector(Protocol):
    def select(self, goal: str, snap: Snapshot, step: PlannerOutput) -> Selection: ...

    def verify(
        self, goal: str, step: PlannerOutput, element: UIElement | None, before: Snapshot, after: Snapshot
    ) -> float: ...


class ActOutcome(Protocol):
    """Satisfied by act.executor.ActionResult."""

    ok: bool
    note: str


@dataclass
class Hooks:
    """Side-effecting dependencies, injectable for tests and --dry-run."""

    observe: Callable[[], Snapshot]
    execute: Callable[[PlannerOutput, UIElement | None], ActOutcome]
    run_terminal: Callable[[str], ActOutcome]
    confirm_terminal: Callable[[str], bool] = lambda cmd: False
    on_plan: Callable[[int, Snapshot, PlannerOutput], None] = lambda *a: None
    on_select: Callable[[Selection], None] = lambda *a: None
    on_record: Callable[[StepRecord], None] = lambda *a: None
    sleep: Callable[[float], None] = time.sleep


@dataclass
class AgentResult:
    success: bool
    reason: str
    history: list[StepRecord] = field(default_factory=list)


class Agent:
    def __init__(self, settings: Settings, planner: Planner, selector: Selector, hooks: Hooks, dry_run: bool = False):
        self.s = settings
        self.planner = planner
        self.selector = selector
        self.hooks = hooks
        self.dry_run = dry_run

    def run(self, goal: str) -> AgentResult:
        history: list[StepRecord] = []
        failures = 0
        snap: Snapshot | None = None
        for n in range(1, self.s.max_steps + 1):
            snap = snap or self.hooks.observe()
            rec, snap = self._step(n, goal, snap, history)
            if rec is None:
                return AgentResult(True, "planner reported the goal is done", history)
            history.append(rec)
            self.hooks.on_record(rec)
            if self.dry_run:
                return AgentResult(rec.note.startswith("dry-run"), "dry run: stopped after one step", history)
            failures = 0 if rec.succeeded else failures + 1
            if failures >= self.s.max_consecutive_failures:
                return AgentResult(False, f"{failures} consecutive failed steps", history)
        return AgentResult(False, f"reached max steps ({self.s.max_steps})", history)

    def _step(
        self, n: int, goal: str, snap: Snapshot, history: list[StepRecord]
    ) -> tuple[StepRecord | None, Snapshot | None]:
        """Run one iteration. Returns (record, snapshot to reuse next time) or (None, _) when done."""
        try:
            plan = self.planner.next_step(goal, snap.scope(), history)
        except (PlannerError, httpx.HTTPError) as e:
            return StepRecord(n, "planner error", note=str(e)[:300]), snap
        self.hooks.on_plan(n, snap, plan)
        if plan.done:
            return None, snap
        rec = StepRecord(n, plan.summary())

        if plan.action == "terminal":
            cmd = plan.command or ""
            if self.dry_run:
                rec.note = f"dry-run: would run {cmd!r}"
                return rec, snap
            if not self.hooks.confirm_terminal(cmd):
                rec.note = "user declined the terminal command"
                return rec, snap
            out = self.hooks.run_terminal(cmd)
            rec.executed, rec.verified, rec.note = True, out.ok, out.note
            self.hooks.sleep(self.s.settle_s)
            return rec, None  # terminal commands may change the frontmost app; re-observe

        element = None
        if plan.needs_jev():
            try:
                sel = self.selector.select(goal, snap, plan)
            except (JevError, httpx.HTTPError) as e:
                rec.note = f"selector error: {e}"[:300]
                return rec, snap
            self.hooks.on_select(sel)
            element = sel.element
            rec.element, rec.probability, rec.confidence = element.describe(), sel.probability, sel.confidence
            if sel.probability < self.s.threshold:
                rec.note = (
                    f"skipped: best match probability {sel.probability:.2f} below threshold {self.s.threshold:.2f} "
                    f"({sel.candidates} candidates); describe the target differently or use another action"
                )
                return rec, snap

        if self.dry_run:
            rec.note = "dry-run: action not executed"
            return rec, snap

        out = self.hooks.execute(plan, element)
        rec.executed, rec.note = out.ok, out.note
        if not out.ok:
            return rec, None
        self.hooks.sleep(self.s.settle_s)
        after = self.hooks.observe()
        try:
            p = self.selector.verify(goal, plan, element, snap, after)
            rec.verify_probability, rec.verified = p, p >= self.s.verify_threshold
        except (JevError, httpx.HTTPError) as e:
            rec.note += f"; verify error: {e}"[:200]
        return rec, after
