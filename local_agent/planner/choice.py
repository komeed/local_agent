"""Multiple-choice planner for small models: instead of writing a whole PlannerOutput, Qwen answers two short
questions in one conversation:

1. What kind of step is next? (one word from an ordered checklist, constrained by the JSON schema)
2. The details for that kind: keys, which element (picked by its name from the screen view), the text, the command.

The second question is appended to the first conversation, so Ollama reuses the cached prompt (goal, history,
screen view) and only reads the new question. Answers are short and mostly enums, which a 3B can get right where a
free-form JSON step is too much. Assembled into the same PlannerOutput as QwenPlanner, so the loop doesn't change.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from local_agent.history import StepRecord, loop_warning
from local_agent.planner.qwen import PlannerError, QwenPlanner, progress_line
from local_agent.planner.schema import PlannerOutput, Target

KINDS = ("done", "terminal", "click", "shortcut", "type", "open", "scroll")
MENU_KINDS = ("done", "terminal", "click", "menu", "shortcut", "type", "open", "scroll")  # menu actions on
# Small models follow an ordered checklist much better than a menu of equal options (they otherwise answer "click").
KIND_QUESTION_MENU = """Which kind is the next step? Go down this list and answer with the FIRST one that fits:
1. done: the steps so far already did everything the goal asks (a step marked success worked), or the goal asks a
   question you can now answer
2. terminal: the goal is about files or folders, or needs an app or website that is not on screen yet (open it)
3. click: a dialog, sheet or error message is on screen that needs a button clicked
4. menu: the app's menus offer it (menu_commands): new, save, save as, copy, paste, undo, find, bold, close window
5. shortcut: a plain key does it: close a menu or dialog (escape), confirm (return), tab, arrows
6. type: text needs to be entered
7. open: a file or item in a list must be opened
8. click: anything else on screen
9. scroll: what you need is not on screen
Reply {"kind": "..."}."""
KIND_QUESTION = """Which kind is the next step? Go down this list and answer with the FIRST one that fits:
1. done: the steps so far already did everything the goal asks (a step marked success worked), or the goal asks a
   question you can now answer
2. terminal: the goal is about files or folders, or needs an app or website that is not on screen yet (open it)
3. click: a dialog, sheet or error message is on screen, or the goal says to use a menu or button
4. shortcut: a keyboard shortcut does it: select all, new document, save, find, bold, italic, undo, close a menu
5. type: text needs to be entered
6. open: a file or item in a list must be opened
7. click: anything else on screen
8. scroll: what you need is not on screen
Reply {"kind": "..."}."""
# Worked examples (deliberately not the eval's goals). In the system prompt, so Ollama caches them across steps.
EXAMPLES_MENU = """Examples of choosing the kind:
- Goal "Delete old.txt from my Documents folder", no steps yet -> terminal
- Goal "Open Safari", no steps yet -> terminal
- Goal "Check my calendar", the screen shows Terminal (where the agent was started) -> terminal
- Goal "Copy the selected text", no steps yet -> menu
- Goal "Undo my last change", no steps yet -> menu
- Goal "Close this menu", a menu is open -> shortcut
- Goal "Write Dear Bob", steps: 1. type text='Dear Bob' -> success -> done
- Goal "What's the date on screen?", the date is shown on SCREEN -> done
- Goal "Print the page", a dialog with an "OK" button and an error message is on screen -> click
- Goal "Use the Edit menu to paste", no steps yet -> menu
- Goal "Search for cats", the search field is focused -> type
- Goal "Open the photo beach.jpg", it is listed in a Finder window -> open"""

EXAMPLES = """Examples of choosing the kind:
- Goal "Delete old.txt from my Documents folder", no steps yet -> terminal
- Goal "Open Safari", no steps yet -> terminal
- Goal "Check my calendar", the screen shows Terminal (where the agent was started) -> terminal
- Goal "Copy the selected text", no steps yet -> shortcut
- Goal "Undo my last change", no steps yet -> shortcut
- Goal "Close this menu", a menu is open -> shortcut
- Goal "Write Dear Bob", steps: 1. type text='Dear Bob' -> success -> done
- Goal "What's the date on screen?", the date is shown on SCREEN -> done
- Goal "Print the page", a dialog with an "OK" button and an error message is on screen -> click
- Goal "Use the Edit menu to paste", no steps yet -> click
- Goal "Search for cats", the search field is focused -> type
- Goal "Open the photo beach.jpg", it is listed in a Finder window -> open"""

SYSTEM = """You control a Mac for the user, one small step at a time. You get the user's goal, the steps taken so far
and SCREEN, a text view of what is on screen now: each control has a [number]; quoted lines are visible text.
Answer each question with JSON only. Never repeat a step that failed; try another way. A step marked success worked.
If a dialog or error message is shown, deal with it first."""

SHORTCUTS = ('select all "cmd+a", new "cmd+n", save "cmd+s", find "cmd+f", bold "cmd+b", italic "cmd+i", '
             'undo "cmd+z", copy "cmd+c", paste "cmd+v", close window "cmd+w", back "cmd+[", '
             'close a menu or dialog "escape", confirm "return"')  # fmt: skip
NOT_LISTED = "not listed"
TEXT_KINDS = ("text field", "text area", "search field", "combo box", "password field")
_ID = re.compile(r"\[(\d+)\] ([a-z ]+?)(?= \"| =| \(|$)", re.M)
_LINE = re.compile(r"^(?:\s*|Focused: )\[(\d+)\] (.+)$", re.M)
OVERVIEW_CHARS = 700  # the first question only sees the top of the view (header, focused, dialog, open menu)


MENU_CHOICES = 60


def menu_paths(lines: list[str], goal: str, limit: int = MENU_CHOICES) -> list[str]:
    """Menu paths from the scope's "File > Save As… [cmd+shift+s]" lines, trimmed to `limit` by closeness to the goal."""
    from local_agent.observe.models import UIElement
    from local_agent.selector.prefilter import rank

    paths = list(dict.fromkeys(re.sub(r" \[[^\[\]]*\]$", "", l) for l in lines))
    paths = [p for p in paths if ">" in p] or paths  # bare menu names (large-menu summaries) are not commands
    if len(paths) <= limit:
        return paths
    pseudo = [UIElement(id=i, role="AXMenuItem", title=p) for i, p in enumerate(paths)]
    return [paths[e.id] for e in rank(goal, pseudo, limit).kept][:limit]


def screen_ids(screen: str | None) -> tuple[list[int], list[int]]:
    """(all numbered element ids, ids of text inputs) in the screen view, in order shown."""
    ids, text_ids = [], []
    for m in _ID.finditer(screen or ""):
        i = int(m.group(1))
        if i not in ids:
            ids.append(i)
            if m.group(2) in TEXT_KINDS:
                text_ids.append(i)
    return ids, text_ids


def _label(rest: str) -> str:
    """'menu "Edit" ▸' -> 'Edit (menu ▸)': name first, no quotes, so the model can write what it means."""
    m = re.match(r'([a-z ]+?) "([^"]*)"(.*)$', rest)
    label = f"{m.group(2)} ({m.group(1)}{m.group(3)})" if m else rest
    return label.replace('"', "").strip()


def screen_choices(screen: str | None, limit: int = 70) -> dict[str, int]:
    """Option label (the view line, name first, without its number) -> element id. A small model picks a meaningful
    label far better than a bare number; labels repeated on screen get the number appended."""
    out: dict[str, int] = {}
    for m in _LINE.finditer(screen or ""):
        if m.group(2).endswith("disabled)"):
            continue
        label = _label(m.group(2))[:limit]
        if label in out:
            label = f"{label} #{m.group(1)}"
        out.setdefault(label, int(m.group(1)))
    return out


def overview(screen: str, chars: int = OVERVIEW_CHARS) -> str:
    if len(screen) <= chars:
        return screen
    cut = screen.rfind("\n", 0, chars)
    return screen[: cut if cut > 0 else chars] + "\n…"


def history_lines(history: list[StepRecord], max_history: int = 6) -> str:
    if not history:
        return "none yet"
    out = []
    for h in history[-max_history:]:
        line = f"{h.step}. {h.plan}"
        if h.element:
            line += f" on {h.element[:80]}"
        line += f" -> {'success' if h.succeeded else 'FAILED'}"
        if h.note:
            line += f" ({h.note[:160]})"
        out.append(line)
    return "\n".join(out)


def _obj(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props)}


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class ChoicePlanner(QwenPlanner):
    max_prompt_tokens = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.menus = self.settings.menus_on()  # the menu kind and its prompts; off: the pre-menu prompts
        self._menu_lines: list[str] = []
        self._goal = ""
        self.schema_in_prompt = False  # each question spells out its reply format; keeps the cached prefix stable

    def _ask(self, messages: list[dict], question: str, schema: dict) -> dict:
        messages.append({"role": "user", "content": question})
        raw = self._chat(messages, schema)
        prompt_tokens = self.last_prompt_tokens
        try:
            ans = json.loads(raw)
            if not isinstance(ans, dict) or not all(k in ans for k in schema["required"]):
                raise ValueError(f"needs {schema['required']}")
        except ValueError as e:
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Invalid ({e}). Reply with JSON like "
                          f"{json.dumps({k: '...' for k in schema['required']})}."}]  # fmt: skip
            raw = self._chat(messages, schema)
            try:
                ans = json.loads(raw)
            except ValueError as e2:
                raise PlannerError(f"Planner gave invalid JSON twice: {raw[:200]}") from e2
            if not isinstance(ans, dict):
                raise PlannerError(f"Planner gave invalid JSON twice: {raw[:200]}")
        messages.append({"role": "assistant", "content": json.dumps(ans)})
        self.max_prompt_tokens = max(self.max_prompt_tokens, prompt_tokens)
        return ans

    def next_step(
        self, goal: str, scope: dict, history: list[StepRecord], screen: str | None = None
    ) -> PlannerOutput:
        self.max_prompt_tokens = 0
        examples, question, kinds = (EXAMPLES_MENU, KIND_QUESTION_MENU, MENU_KINDS) if self.menus else (
            EXAMPLES, KIND_QUESTION, KINDS)  # fmt: skip
        messages = self._messages(goal, scope, history, screen, f"{SYSTEM}\n\n{examples}")
        kind = self._ask(messages, question, _obj(kind={"enum": list(kinds)}))["kind"]
        kind = str(kind).strip().lower()
        if kind not in kinds:
            raise PlannerError(f"unknown step kind {kind!r}")
        return self._build(messages, kind, screen)

    def _messages(
        self, goal: str, scope: dict, history: list[StepRecord], screen: str | None, system: str
    ) -> list[dict]:
        self._menu_lines, self._goal = scope.get("menu_commands") or [], goal
        context = f"GOAL: {goal}\n\nSTEPS SO FAR:\n{history_lines(history)}"
        if warning := loop_warning(history):
            context += f"\nWARNING: {warning}"
        if scope.get("progress"):
            context += f"\n{progress_line(scope['progress'])}"
        if scope.get("note"):
            context += f"\nNOTE: {scope['note']}"
        if scope.get("open_menu_items"):
            context += f"\n\nOPEN MENU ITEMS: {', '.join(scope['open_menu_items'])}"
        context += f"\n\nTOP OF SCREEN:\n{overview(screen)}" if screen else f"\n\nAPP: {scope.get('app')} — window {scope.get('window')!r}"
        # The context is its own message so the follow-up questions share its cached prefix.
        return [{"role": "system", "content": system}, {"role": "user", "content": context},
                {"role": "assistant", "content": "Ready."}]  # fmt: skip

    def _menu_details(self, messages: list[dict], why: str) -> PlannerOutput:
        """Pick the command's path from an enum of the enabled menu commands (at most MENU_CHOICES, the ones closest
        to the goal when there are more)."""
        paths = menu_paths(self._menu_lines, self._goal)
        if paths:
            a = self._ask(messages, "Which menu command? Answer with its path. Reply {\"command\": \"...\"}.",
                          _obj(command={"enum": paths}))  # fmt: skip
        else:
            a = self._ask(messages, 'Which menu command, as "Menu > Item"? Reply {"command": "..."}.',
                          _obj(command={"type": "string"}))  # fmt: skip
        return PlannerOutput(done=False, reasoning=why, action="menu", command=str(a["command"]))

    def _build(self, messages: list[dict], kind: str, screen: str | None, why: str = "") -> PlannerOutput:
        try:
            plan = self._details(messages, kind, screen, screen_ids(screen)[1], why or f"chose {kind}")
        except ValidationError as e:
            raise PlannerError(f"could not build a {kind} step: {e.errors()[0]['msg']}") from e
        self.last_prompt_tokens = self.max_prompt_tokens
        return plan

    def _details(
        self, messages: list[dict], kind: str, screen: str | None, text_ids: list[int], why: str
    ) -> PlannerOutput:
        full = f"SCREEN:\n{screen}\n\n" if screen else ""
        if kind == "done":
            a = self._ask(messages, full + "In one sentence, what is the result? Include any answer the user asked for. "
                          'Reply {"summary": "..."}.', _obj(summary={"type": "string"}))  # fmt: skip
            return PlannerOutput(done=True, reasoning=str(a["summary"]))
        if kind == "shortcut":
            keys_q = ('Which keys? Only plain keys: "escape", "return", "tab", an arrow. Reply {"keys": "..."}.'
                      if self.menus else f'Which keys? Common: {SHORTCUTS}. Reply {{"keys": "..."}}.')
            a = self._ask(messages, keys_q,
                          _obj(keys={"type": "string"}))
            return PlannerOutput(done=False, reasoning=why, action="shortcut", keys=str(a["keys"]).strip().lower())
        if kind == "menu":
            return self._menu_details(messages, why)
        if kind == "terminal":
            a = self._ask(messages, 'Which shell command? For files and folders use ls, mkdir, mv, cp or cat '
                          'directly (~ is the home folder); use "open -a <App>" only to open an app. '
                          'Reply {"command": "..."}.', _obj(command={"type": "string"}))  # fmt: skip
            return PlannerOutput(done=False, reasoning=why, action="terminal", command=str(a["command"]))
        if kind == "scroll":
            a = self._ask(messages, 'Which direction? Reply {"direction": "..."}.',
                          _obj(direction={"enum": ["up", "down", "left", "right"]}))
            return PlannerOutput(done=False, reasoning=why, action="scroll", direction=a["direction"])
        if kind == "type":
            if text_ids:
                a = self._ask(messages, full + "What text, and into which field? Give the field's [number] from SCREEN, "
                              'or 0 for the focused element. Reply {"text": "...", "field": 0}.',
                              _obj(text={"type": "string"}, field={"enum": [0, *text_ids]}))  # fmt: skip
            else:
                a = self._ask(messages, 'What text? Reply {"text": "..."}.', _obj(text={"type": "string"}))
            field = _int(a.get("field"))
            target = Target(id=field) if field in text_ids else None
            return PlannerOutput(done=False, reasoning=why, action="type", text=str(a["text"]), target=target)
        action = "double_click" if kind == "open" else "click"
        choices = screen_choices(screen)
        if choices:
            a = self._ask(messages, full + f"Which one do you {'open' if kind == 'open' else 'click'}? Answer with its "
                          f'name, or "{NOT_LISTED}". Reply {{"element": "..."}}.',
                          _obj(element={"enum": [*choices, NOT_LISTED]}))  # fmt: skip
            pick = choices.get(str(a["element"]).strip())
            if pick is not None:
                return PlannerOutput(done=False, reasoning=why, action=action, target=Target(id=pick))
        a = self._ask(messages, "Describe it: its visible name and where it is. Reply {\"description\": \"...\"}.",
                      _obj(description={"type": "string"}))
        return PlannerOutput(done=False, reasoning=why, action=action, target=Target(description=str(a["description"])))
