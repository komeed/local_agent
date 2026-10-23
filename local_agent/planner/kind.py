"""Jev chooses the kind of step, the planner fills in the details (planner_mode="kind").

The 3B planner's main failure is the kind of step (it answers "click" for terminal and shortcut tasks and almost
never "done"), and that's a short multiple-choice judgment, which is what Jev is for. So each step:

1. Jev answers a `choice` over STEP_KINDS (selector/jev.py) from the goal, the steps so far, the scope and the top
   of the screen view.
2. Auto view: the planner gets the screen view only for kinds that need it (click, type, done); for terminal,
   shortcut and scroll it sees just the goal, the steps and the app/window, so its prompt stays small.
3. The planner answers ChoicePlanner's detail question for that kind (keys, command, which element, text).

A low-probability "done" falls back to Jev's next choice: a wrong done ends the run. Backends that can't judge
kinds (cosine) or Jev errors fall back to ChoicePlanner asking the planner itself.
"""

from __future__ import annotations

from local_agent.history import StepRecord, loop_warning
from local_agent.planner.choice import ChoicePlanner, history_lines, overview
from local_agent.planner.qwen import progress_line
from local_agent.planner.schema import PlannerOutput

VIEW_KINDS = {"click", "type", "done"}  # kinds whose details need the screen view
DONE_MIN_P = 0.6  # below this, take Jev's runner-up instead of ending the run
KIND_VIEW_CHARS = 1500  # how much of the screen view Jev reads for the kind question

SYSTEM = """You control a Mac for the user, one small step at a time. You get the user's goal, the steps taken so far
and, when it matters, SCREEN, a text view of what is on screen now: each control has a [number]; quoted lines are
visible text. The kind of the next step is already chosen; answer each question with JSON only. Never repeat a step
that failed; try another way. A step marked success worked.
When the goal says to use a menu, run its command with the menu kind, not a toolbar button."""

SYSTEM_OLD = """You control a Mac for the user, one small step at a time. You get the user's goal, the steps taken so far
and, when it matters, SCREEN, a text view of what is on screen now: each control has a [number]; quoted lines are
visible text. The kind of the next step is already chosen; answer each question with JSON only. Never repeat a step
that failed; try another way. A step marked success worked.
Menus: menu items only exist after their menu is opened. To use a menu, first click its menu bar item (e.g. Format),
then the item; for a submenu (Format > Font > Bold) click the parent item (Font) first. When the goal says to use a
menu, click the menu, not a toolbar button."""


def kind_state(goal: str, scope: dict, history: list[StepRecord], screen: str | None) -> str:
    out = f"User goal: {goal}\nSteps so far:\n{history_lines(history)}"
    if warning := loop_warning(history):
        out += f"\nWarning: {warning}"
    out += f"\nApp on screen: {scope.get('app')} — window {scope.get('window')!r}"
    if scope.get("progress"):
        out += f"\n{progress_line(scope['progress'])}"
    if scope.get("note"):
        out += f"\nNote: {scope['note']}"
    if scope.get("open_menu_items"):
        out += f"\nOpen menu items: {', '.join(scope['open_menu_items'])}"
    if scope.get("focused_element"):
        out += f"\nFocused: {scope['focused_element']}"
    if screen:
        out += f"\nTop of the screen:\n{overview(screen, KIND_VIEW_CHARS)}"
    return out


def pick_kind(probabilities: dict[str, float]) -> tuple[str, float]:
    ranked = sorted(probabilities.items(), key=lambda kv: -float(kv[1]))
    kind, p = ranked[0]
    if kind == "done" and float(p) < DONE_MIN_P and len(ranked) > 1:
        kind, p = ranked[1]
    return kind, float(p)


class KindPlanner(ChoicePlanner):
    def __init__(self, settings, selector, client=None):
        super().__init__(settings, client=client)
        self.selector = selector
        self.last_kind: tuple[str, float] | None = None  # (kind, Jev's probability); None when the planner chose

    def next_step(
        self, goal: str, scope: dict, history: list[StepRecord], screen: str | None = None
    ) -> PlannerOutput:
        from local_agent.selector.jev import JevError

        self.last_kind = None
        try:
            answer = self.selector.step_kind(kind_state(goal, scope, history, screen))
        except JevError:
            answer = None
        if answer is None:
            return super().next_step(goal, scope, history, screen)
        kind, p = pick_kind(answer.probabilities or {answer.choice: answer.probability})
        self.last_kind = (kind, p)
        self.max_prompt_tokens = 0
        view = screen if kind in VIEW_KINDS else None
        messages = self._messages(goal, scope, history, view, SYSTEM if self.menus else SYSTEM_OLD)
        return self._build(messages, kind, view, why=f"Jev chose {kind} (p={p:.2f})")
