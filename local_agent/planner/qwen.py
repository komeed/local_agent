"""Qwen 2.5 Coder planner via Ollama. Sees only the goal, a compact window scope and recent history,
never the full element JSON."""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from local_agent.config import Settings
from local_agent.history import StepRecord, loop_warning
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
  opening URLs ("open https://example.com", or in a given browser "open -a Arc https://mail.google.com") and file
  operations. Its output is reported back to you. Safe commands run right away; risky ones (deleting, sudo,
  changing settings) wait for the user's approval, so prefer read-only commands and move files to the Trash
  ("mv file ~/.Trash/") instead of rm.

Opening apps: you pick the app. If the goal needs an app that is not the one on screen (see "note" in the scope),
the first step is a terminal step that opens it with its plain name, e.g. open -a "Arc", open -a "Google Chrome",
open -a "Notes"; it is brought to the front for the next step.

Common shortcuts: select all "cmd+a", copy "cmd+c", paste "cmd+v", undo "cmd+z", new document/window "cmd+n",
save "cmd+s", close window "cmd+w", find "cmd+f", bold "cmd+b". Selecting text is a shortcut, never a click.

Menus: menu items (AXMenuItem) only exist after their menu is opened. To use a menu, first click its AXMenuBarItem
(e.g. "Format"), then click the AXMenuItem; for a submenu (e.g. Format > Font > Bold) click the parent AXMenuItem
("Font") and then the item ("Bold"). While a menu is open, "open_menu_items" in the scope lists its visible items:
only target an AXMenuItem that is listed there; anything else is inside a submenu, so click its listed parent first.
Press "escape" to close a menu.

Rules:
- Use roles from the element counts in the scope when possible (AXButton, AXTextField, AXTextArea, AXMenuItem,
  AXMenuBarItem, AXLink, AXCell, AXRow, AXCheckBox, AXPopUpButton, ...). Use "any" if unsure.
- Look at the history: if a step failed, do not repeat it unchanged; try another target, a shortcut or terminal.
- Set done=true (action null) only when the history shows the goal is fully accomplished.
- Respond with JSON only, matching the schema."""


_OLD_SHORTCUT = '- shortcut: needs keys, e.g. "cmd+n", "cmd+shift+t", "return", "escape", "tab".'
_NEW_SHORTCUT = '''- menu: needs command, a path from "menu_commands" in the JSON, e.g. "File > Save As…". Use it for anything the
  app's menus offer (new, save, copy, bold, find...) instead of clicking menus or pressing shortcuts yourself.
- shortcut: needs keys, only for keys that are not menu commands: "return", "escape", "tab", arrows.'''
# With menu actions on, menu commands replace the shortcut list and the paragraph on opening menus by clicking.
MENU_SYSTEM_PROMPT = (
    SYSTEM_PROMPT.replace(_OLD_SHORTCUT, _NEW_SHORTCUT)
    .replace(SYSTEM_PROMPT[SYSTEM_PROMPT.index("Common shortcuts:"):SYSTEM_PROMPT.index("Rules:")],
             'Selecting text is a shortcut ("cmd+a"), never a click. Press "escape" to close a menu or dialog.\n\n')
)  # fmt: skip

SCREEN_PROMPT = """
Screen view: below the JSON you get SCREEN, a text view of what is on screen right now. Each control has a [number];
lines without a number are visible text (labels, messages). Read it before deciding: use controls you can see
(toolbar buttons, dialog buttons, fields, open menu items) instead of guessing. When the element you want is listed,
set target.id to its number (role/description optional). When it is not listed (e.g. "… more not shown"), leave id
null and describe it; it will be found. Numbers change every step, so always use the numbers from the current SCREEN.
If a dialog or error message is shown, deal with it first."""

SCREEN_PROMPT_NAMES = """
Screen view: below the JSON you get SCREEN, a text view of what is on screen right now: controls with their kind and
name, and quoted lines of visible text (labels, messages). Read it before deciding: use controls you can see
(toolbar buttons, dialog buttons, fields, open menu items) instead of guessing, and describe the target using its
name from SCREEN. If a dialog or error message is shown, deal with it first."""


PROGRESS_PROMPT = """
Progress: when the JSON has "progress", it is computed from the screen by code and is right. Work only on "next";
never redo items in "done"."""


def progress_line(progress: dict) -> str:
    """One line for the planners that don't get JSON: done items, the next one, the rest."""
    parts = [f"done: {'; '.join(progress['done']) or 'nothing yet'}"]
    if progress.get("next"):
        parts.append(f"next: {progress['next']}")
    if progress.get("remaining"):
        parts.append(f"remaining: {'; '.join(progress['remaining'])}")
    return "PROGRESS: " + "; ".join(parts)


def repeated_failures(history: list[StepRecord]) -> int:
    """How many trailing steps failed with the same plan as the last one."""
    n = 0
    for h in reversed(history):
        if h.succeeded or h.plan != history[-1].plan:
            break
        n += 1
    return n


def build_messages(
    goal: str,
    scope: dict,
    history: list[StepRecord],
    screen: str | None = None,
    numbered: bool = True,
    max_history: int = 6,
    menus: bool = False,
) -> list[dict]:
    scope = dict(scope)
    progress = scope.pop("progress", None)
    menu_commands = scope.pop("menu_commands", None)
    if not menus:
        menu_commands = None
    payload = {
        "goal": goal,
        "current_scope": scope,
        "steps_taken": len(history),
        "recent_history": [h.for_planner() for h in history[-max_history:]],
    }
    if progress:
        payload["progress"] = progress
    if menu_commands:
        payload["menu_commands"] = menu_commands
    warning = loop_warning(history)
    if warning:
        payload["warning"] = warning
    user = json.dumps(payload, ensure_ascii=False, indent=1)
    if screen:
        user += f"\n\nSCREEN:\n{screen}"
    return [
        {"role": "system", "content": (MENU_SYSTEM_PROMPT if menus else SYSTEM_PROMPT) + ((SCREEN_PROMPT if numbered else SCREEN_PROMPT_NAMES) if screen else "")
         + (PROGRESS_PROMPT if progress else "")},
        {"role": "user", "content": user},
    ]


class PlannerError(RuntimeError):
    pass


def extract_json(raw: str) -> str:
    """The outermost {...} in a reply; unconstrained models wrap JSON in prose or ``` fences."""
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start : end + 1] if start != -1 and end > start else raw


def _without_menu(schema: dict) -> dict:
    """The planner schema with "menu" removed from the action enum (wherever the schema keeps it)."""
    def strip(node):
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list) and "menu" in node["enum"] and "click" in node["enum"]:
                node = {**node, "enum": [a for a in node["enum"] if a != "menu"]}
            return {k: strip(v) for k, v in node.items()}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(schema)


class QwenPlanner:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.ollama_url, timeout=settings.http_timeout_s)
        self.schema = PlannerOutput.model_json_schema()
        if not settings.menus_on():  # the model can't pick an action its prompt doesn't offer
            self.schema = _without_menu(self.schema)
            self.schema["properties"]["command"]["description"] = (
                "Shell command to run in the terminal. Required when action is 'terminal'.")
        # Some Ollama engines (e.g. Qwen3.5 NVFP4 builds) reject `format`; then the schema goes in the prompt.
        self.structured = True
        self.schema_in_prompt = True  # when unstructured, add the schema as a system message (ChoicePlanner doesn't)
        self.last_prompt_tokens = 0  # Ollama's prompt_eval_count for the most recent call
        # Messages of the most recent call as sent (incl. any schema note), plus the raw reply as the last message.
        self.last_messages: list[dict] = []
        self._sent: list[dict] = []

    def _post(self, messages: list[dict], schema: dict) -> httpx.Response:
        body = {
            "model": self.settings.planner_model,
            "messages": messages,
            "stream": False,
            "think": self.settings.planner_think,
            "options": {"temperature": 0, "num_ctx": self.settings.planner_num_ctx},
        }
        if self.structured:
            body["format"] = schema
        elif self.schema_in_prompt:
            schema_note = {
                "role": "system",
                "content": "Reply with one JSON object only (no prose, no code fences) matching this JSON schema:\n"
                + json.dumps(schema),
            }
            body["messages"] = [messages[0], schema_note, *messages[1:]]
        self._sent = list(body["messages"])
        return self.client.post("/api/chat", json=body)

    def _chat(self, messages: list[dict], schema: dict | None = None) -> str:
        schema = schema or self.schema
        r = self._post(messages, schema)
        if self.structured and r.status_code == 501 and "structured output" in r.text:
            self.structured = False
            r = self._post(messages, schema)
        r.raise_for_status()
        data = r.json()
        self.last_prompt_tokens = data.get("prompt_eval_count", 0)
        self.last_messages = [*self._sent, {"role": "assistant", "content": data["message"]["content"]}]
        return extract_json(data["message"]["content"])

    def next_step(
        self, goal: str, scope: dict, history: list[StepRecord], screen: str | None = None
    ) -> PlannerOutput:
        messages = build_messages(goal, scope, history, screen, numbered=self.settings.planner_view == "ids",
                                  menus=self.settings.menus_on())
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
                msg = e2.errors()[0]["msg"]
                raise PlannerError(f"Planner returned invalid output twice ({msg}); last output: {raw[:400]}") from e2


def make_planner(settings: Settings, selector=None):
    if settings.planner_mode == "jev":
        from local_agent.planner.jev_only import JevPlanner

        if selector is None:
            raise ValueError("planner_mode 'jev' needs the Jev selector")
        return JevPlanner(settings, selector)
    if settings.planner_mode == "kind":
        from local_agent.planner.kind import KindPlanner

        if selector is None:
            raise ValueError("planner_mode 'kind' needs the Jev selector")
        return KindPlanner(settings, selector)
    if settings.planner_mode == "choice":
        from local_agent.planner.choice import ChoicePlanner

        return ChoicePlanner(settings)
    return QwenPlanner(settings)
