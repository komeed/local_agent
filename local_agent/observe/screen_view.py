"""A compact (optionally numbered) text rendering of a Snapshot for the planner, so even a small model can read what is on
screen instead of recalling where things usually are.

    Focused: [0] text area = "Hello world" (focused, selected "Hello world")
    Window "Untitled":
      [3] checkbox "bold" off   [4] checkbox "italic" off   ...
    Menu bar: [27] Apple  [29] File  [31] Format ...

Sections are emitted in priority order (focused, dialogs, open menus, window, toolbars, lists, menu bar) until the
token budget runs out; long lists are collapsed to their first few items plus a count. Numbers are UIElement.id, so
the planner can answer with `target.id`.
"""

from __future__ import annotations

from collections import OrderedDict

from local_agent.observe.models import Snapshot, UIElement

KINDS = {
    "AXButton": "button", "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXPopUpButton": "popup",
    "AXMenuButton": "menu button", "AXComboBox": "combo box", "AXTextField": "text field", "AXTextArea": "text area",
    "AXSecureTextField": "password field", "AXSearchField": "search field", "AXSlider": "slider", "AXLink": "link",
    "AXMenuItem": "item", "AXMenuBarItem": "menu", "AXTab": "tab", "AXCell": "cell", "AXRow": "row",
    "AXColorWell": "color well", "AXDisclosureTriangle": "disclosure", "AXIncrementor": "stepper",
    "AXDateField": "date field", "AXSegmentedControl": "segmented", "AXStaticText": "text", "AXHeading": "heading",
}  # fmt: skip
LIST_CONTAINERS = ("AXTable", "AXOutline", "AXList", "AXBrowser", "AXCollection", "AXGrid")
LIST_SHOW = 12  # items shown per list before collapsing
CHARS_PER_TOKEN = 3.0  # conservative for Qwen tokenizers on this kind of text
# Prompt tokens that aren't the screen view: system prompt (+ schema when structured output is unavailable),
# goal/scope/history JSON and the reply.
PROMPT_RESERVE_TOKENS = 3500
MIN_VIEW_TOKENS = 400


def view_budget(num_ctx: int, max_tokens: int) -> int:
    """Screen-view token budget that fits next to the rest of the prompt in a `num_ctx` window."""
    return max(MIN_VIEW_TOKENS, min(max_tokens, num_ctx - PROMPT_RESERVE_TOKENS))


def _tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _quote(text: str, limit: int = 60) -> str:
    text = text.replace('"', "'")
    return f'"{text if len(text) <= limit else text[: limit - 1] + "…"}"'


def line(e: UIElement, numbered: bool = True) -> str:
    if not e.interactive:
        return _quote(e.value, 120)
    kind = KINDS.get(e.role, e.role.removeprefix("AX").lower())
    if e.subrole in ("AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton"):
        kind = e.subrole.removeprefix("AX").replace("Button", " button").lower()
    name = e.title or e.label or e.help
    parts = [f"[{e.id}]"] if numbered else []
    if kind:
        parts.append(kind)
    if name:
        parts.append(_quote(name, 50))
    if e.role in ("AXCheckBox", "AXRadioButton", "AXDisclosureTriangle") and e.value in ("0", "1"):
        parts.append("on" if e.value == "1" else "off")
    elif e.value and e.value != name and e.role not in ("AXColorWell",):
        parts.append(f"= {_quote(e.value)}")
    if e.placeholder and not e.value:
        parts.append(f"(placeholder {_quote(e.placeholder, 40)})")
    if e.submenu:
        parts.append("▸")
    flags = []
    if e.focused:
        flags.append("focused")
    if e.selected:
        flags.append(f"selected {_quote(e.selected, 40)}")
    if not e.enabled:
        flags.append("disabled")
    if flags:
        parts.append(f"({', '.join(flags)})")
    return " ".join(parts)


def _segments(e: UIElement) -> list[str]:
    return [s for s in e.path.split(" > ") if s]


def area(e: UIElement) -> tuple[int, str]:
    """(priority, section title) for an element; lower priority renders first."""
    segs = _segments(e)
    roles = [s.split(":", 1)[0] for s in segs]
    if "AXSheet" in roles or any(s in ("AXDialog", "AXSystemDialog") for s in roles):
        return 1, "Dialog"
    if e.role == "AXMenuBarItem":
        return 7, "Menu bar"
    if roles and roles[0] in ("AXMenuBar", "AXMenu"):
        names = [s.split(":", 1)[1] for s in segs if ":" in s and s.split(":", 1)[0] in ("AXMenuBarItem", "AXMenuItem")]
        return 2, "Open menu" + (f" {' > '.join(names)}" if names else "")
    for i, r in enumerate(roles):
        if r in LIST_CONTAINERS:
            title = segs[i].split(":", 1)[1] if ":" in segs[i] else ""
            return 5, f"List {title}".strip()
    if "AXToolbar" in roles:
        return 4, "Toolbar"
    window = next((s.split(":", 1)[1] for s in segs if s.startswith("AXWindow:")), "")
    return 3, f'Window "{window}"' if window else "Window"


def _sort_key(e: UIElement) -> tuple[float, float, int]:
    return (round(e.frame.y / 8) if e.frame else 1e9, e.frame.x if e.frame else 0, e.id)


def render(snap: Snapshot, budget_tokens: int, numbered: bool = True) -> str:
    sections: dict[str, list[UIElement]] = OrderedDict()
    priority: dict[str, int] = {}
    focused = next((e for e in snap.elements if e.focused and e.interactive), None)
    for e in snap.elements:
        if e is focused or (e.role == "AXMenuItem" and not e.name()):  # menu separators
            continue
        prio, title = area(e)
        sections.setdefault(title, []).append(e)
        priority[title] = prio

    out: list[str] = [f'App: {snap.app} — window "{snap.window_title}"']
    if focused is not None:
        out.append(f"Focused: {line(focused, numbered)}")
    used = sum(_tokens(x) for x in out)
    omitted = 0
    for title in sorted(sections, key=lambda t: (priority[t], list(sections).index(t))):
        items = sorted(sections[title], key=_sort_key)
        lines, hidden = [], 0
        if title.startswith("List"):
            seen = set()
            named = []
            for e in items:
                key = line(e, False)
                if e.name() and key not in seen:
                    seen.add(key)
                    named.append(e)
            hidden = len(items) - min(len(named), LIST_SHOW)
            items = named[:LIST_SHOW]
        header = f"{title}:"
        cost = _tokens(header)
        for e in items:
            text = "  " + line(e, numbered)
            t = _tokens(text)
            if used + cost + t > budget_tokens:
                hidden += len(items) - len(lines)
                break
            lines.append(text)
            cost += t
        if not lines:
            omitted += hidden or len(items)
            continue
        out.append(header)
        out.extend(lines)
        if hidden:
            note = f"  … {hidden} more not shown (describe the one you need; it will be found)"
            out.append(note)
            cost += _tokens(note)
        used += cost
    if omitted:
        out.append(f"(+{omitted} more elements not shown)")
    return "\n".join(out)
