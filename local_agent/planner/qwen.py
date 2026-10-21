"""Qwen 2.5 Coder planner via Ollama. Sees only the goal, a compact window scope and recent history,
never the full element JSON."""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.planner.schema import PlannerOutput

SYSTEM_PROMPT = """You are the planning brain of a macOS desktop automation agent.
Each turn you choose exactly ONE next step toward the user's goal. You do not see the full UI; a fast selector model
will find the concrete element from your target description, so describe the target precisely (visible text, purpose,
location such as toolbar/sidebar/dialog).

Actions:
- click / double_click / right_click: needs target {role, description}.
- type: needs text. Set target to the text field to type into, or null to type into the already focused element.
  To submit after typing, use a separate shortcut step with keys "return".
- shortcut: needs keys, e.g. "cmd+n", "cmd+shift+t", "return", "escape", "tab".
- scroll: needs direction; target optional (the scrollable area).
- terminal: run a shell command; you MUST fill "command". Prefer it for launching apps ("open -a TextEdit"),
  opening URLs ("open https://example.com") and file operations. Its output is reported back to you.

Rules:
- Use roles from the element counts in the scope when possible (AXButton, AXTextField, AXTextArea, AXMenuItem,
  AXMenuBarItem, AXLink, AXCell, AXRow, AXCheckBox, AXPopUpButton, ...). Use "any" if unsure.
- Look at the history: if a step failed, do not repeat it unchanged; try another target, a shortcut or terminal.
- Set done=true (action null) only when the history shows the goal is fully accomplished.
- Respond with JSON only, matching the schema."""


def build_messages(goal: str, scope: dict, history: list[StepRecord], max_history: int = 6) -> list[dict]:
    payload = {
        "goal": goal,
        "current_scope": scope,
        "steps_taken": len(history),
        "recent_history": [h.for_planner() for h in history[-max_history:]],
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)},
    ]


class PlannerError(RuntimeError):
    pass


class QwenPlanner:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.ollama_url, timeout=settings.http_timeout_s)
        self.schema = PlannerOutput.model_json_schema()

    def _chat(self, messages: list[dict]) -> str:
        r = self.client.post(
            "/api/chat",
            json={
                "model": self.settings.planner_model,
                "messages": messages,
                "format": self.schema,
                "stream": False,
                "options": {"temperature": 0},
            },
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def next_step(self, goal: str, scope: dict, history: list[StepRecord]) -> PlannerOutput:
        messages = build_messages(goal, scope, history)
        raw = self._chat(messages)
        try:
            return PlannerOutput.model_validate_json(raw)
        except ValidationError as e:
            # One repair attempt: show the model its own output and the validation error.
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That JSON was invalid: {e.errors()[0]['msg']}. Return corrected JSON."},
            ]
            raw = self._chat(messages)
            try:
                return PlannerOutput.model_validate_json(raw)
            except ValidationError as e2:
                raise PlannerError(f"Planner returned invalid output twice: {e2}") from e2
