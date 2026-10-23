"""No planner model: code lists the candidate next actions, JevK5 picks one (planner_mode="jev").

Every step the options are built from the checklist (`progress`), the screen and the goal:

- `done`, when the checklist has nothing left (or there is no checklist and a step already worked);
- the checklist's next line turned into actions (type "X" into "Field", press return, cmd+n / cmd+s for new / save);
- `click "<name>"` for the enabled controls that best match the next item and the goal (prefilter.rank);
- `type "<q>" into "<field>"` for each of the goal's quoted strings and the text fields;
- press return / escape / tab, scroll when the view says there is more, menu commands when menu actions are on;
- "none of these": the goal needs something the options don't offer (a shell command); the step then fails.

There is no free-form terminal and no model fallback: with a backend that can't judge (cosine) or on a Jev error the
step fails with a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from local_agent.goal_facts import ACTION_VERBS, FILLER_WORDS, QUOTE, content_words, parse_facts, quoted_strings
from local_agent.history import StepRecord
from local_agent.observe.models import SINGLE_LINE_ROLES, Snapshot, UIElement
from local_agent.planner.kind import DONE_MIN_P, kind_state
from local_agent.planner.qwen import PlannerError
from local_agent.planner.schema import PlannerOutput
from local_agent.ranking import date_fragment
from local_agent.selector.prefilter import _parts, rank, score

MAX_CLICKS = 12
MAX_OPTIONS = 15  # JevK5 reads 16 options per pass: a choice of at most this many is one pass
MAX_TYPE_FIELDS = 4
MAX_TYPE_STRINGS = 3
MAX_MENU = 8
STATE_CHARS = 1500
HISTORY_LINES = 3
NONE_KEY = "none"
NO_SHELL = "this goal needs a shell command; not available in jev mode"
QUESTION = ("Which action is the best next step toward the user's goal? Work on the PROGRESS 'next' item when there is "
            "one, deal with an open dialog or alert first, and never repeat a step that failed.")  # fmt: skip
TEXT_FIELD_ROLES = SINGLE_LINE_ROLES | {"AXTextArea"}
KEY_OPTIONS = ("return", "escape", "tab")
NEXT_WEIGHT = 0.5  # the checklist's next line counts half as much as the goal's own words when ranking clicks
GENERIC_WORDS = {"my", "your", "our", "tab", "tabs", "window", "windows", "page", "pages", "button", "link", "site"}


class Unsure(PlannerError):
    """Jev's best option is below `jev_min_pick_p` and isn't the checklist's own next line: the step does nothing."""


@dataclass
class Option:
    key: str
    label: str  # what Jev compares
    plan: PlannerOutput | None  # None: "none of these" or done handled by the caller
    kind: str = ""  # done | click | type | key | scroll | menu | none
    from_checklist: bool = False  # built from the checklist's own next line


def _plan(**kw) -> PlannerOutput:
    return PlannerOutput(done=False, reasoning="chosen from the listed actions", **kw)


def _name(e: UIElement) -> str:
    return (e.name() or "").strip()


def _quote(text: str, n: int = 40) -> str:
    text = text.replace('"', "'")
    return text if len(text) <= n else text[: n - 1] + "…"


def _plain(name: str) -> str:
    return re.sub(r"[\s*:]+$", "", name.replace("*", " ")).strip().lower()


def _named(snap: Snapshot, name: str, roles) -> UIElement | None:
    return next((e for e in snap.elements if e.interactive and e.role in roles and _plain(_name(e)) == _plain(name)), None)


def _roles(snap: Snapshot) -> set[str]:
    return {e.role for e in snap.elements}


def _target(e: UIElement | None, role: str, name: str) -> dict:
    return {"id": e.id, "role": e.role, "description": name} if e else {"role": role, "description": name}


def _labelled(e: UIElement, n: int = 40) -> str:
    """The element's name for an option label: cut to n characters, but a date ("2 days ago") is never cut off."""
    name = _name(e).replace('"', "'")
    if len(name) <= n:
        return name
    frag = date_fragment(f"{name} {e.context}")
    base = name[: n - 1].rstrip() + "…"
    return f"{base} · {frag}" if frag and frag not in base else base


def text_fields(snap: Snapshot) -> list[UIElement]:
    fields = [e for e in snap.elements if e.interactive and e.enabled and e.role in TEXT_FIELD_ROLES]
    fields.sort(key=lambda e: (not e.focused, e.frame.y if e.frame else 1e9))
    return fields


def checklist_actions(next_line: str, snap: Snapshot) -> list[tuple[str, PlannerOutput, str]]:
    """(label, plan, kind) for what the checklist's next line says in words code understands."""
    out: list[tuple[str, PlannerOutput, str]] = []
    m = re.search(r'type "([^"]+)" into (?:the )?"([^"]+)"(?: field)?', next_line)
    if m:
        text, name = m.groups()
        target = _target(_named(snap, name, TEXT_FIELD_ROLES), "any", name)
        out.append((f'type "{_quote(text)}" into "{name}"', _plan(action="type", text=text, target=target), "type"))
    if m := re.match(r'choose "([^"]+)" in "([^"]+)"', next_line):
        value, name = m.groups()
        target = _target(_named(snap, name, ("AXPopUpButton", "AXComboBox")), "any", name)
        out.append((f'choose "{_quote(value)}" in "{name}"', _plan(action="type", text=value, target=target), "type"))
    if m := re.match(r'click "([^"]+)", then choose "([^"]+)"', next_line):  # a setting's control, then its value
        out.append((f'click "{m.group(1)}"', _plan(action="click", target=_target(
            _named(snap, m.group(1), _roles(snap)), "any", m.group(1))), "click"))
    if m := re.fullmatch(r'choose "([^"]+)"', next_line):  # the open menu's item for the setting's value
        out.append((f'click "{m.group(1)}"', _plan(action="click", target=_target(
            _named(snap, m.group(1), _roles(snap)), "any", m.group(1))), "click"))
    if m := re.match(r'tick "([^"]+)"', next_line):
        out.append((f'click "{m.group(1)}"', _plan(action="click", target=_target(
            _named(snap, m.group(1), ("AXCheckBox",)), "AXCheckBox", m.group(1))), "click"))
    if m := re.match(r'submit the form: click "([^"]+)"', next_line):
        out.append((f'click "{m.group(1)}"', _plan(action="click", target=_target(
            _named(snap, m.group(1), ("AXButton",)), "AXButton", m.group(1))), "click"))
    if "press return" in next_line or "then press return" in next_line:
        out.append(("press return", _plan(action="shortcut", keys="return"), "key"))
    if re.match(r"make a new", next_line):
        out.append(("press cmd+n (new)", _plan(action="shortcut", keys="cmd+n"), "key"))
    if next_line.startswith("save the document as"):
        out.append(("press cmd+s (save)", _plan(action="shortcut", keys="cmd+s"), "key"))
    return out


def distinctive_words(goal: str) -> list[str]:
    """The goal's content words minus its verbs, filler, generic nouns ("tab", "my"), quoted text (that is typed, not
    clicked) and the names of the apps it mentions: what a control would be called to be the one the goal means."""
    app_words = {w for f in parse_facts(goal) if f.kind == "app" for w in re.findall(r"[a-z0-9]+", f.value.lower())}
    lower = goal.lower()
    words = sorted(content_words(re.sub(QUOTE, " ", goal)), key=lower.find)  # in goal order
    skip = FILLER_WORDS | GENERIC_WORDS | app_words
    return [w for w in words if w not in skip and not any(w.startswith(v) for v in ACTION_VERBS)]


def rank_clicks(goal: str, next_line: str, pool: list[UIElement]) -> list[UIElement]:
    return ranked_clicks(goal, next_line, pool)[0]


def ranked_clicks(goal: str, next_line: str, pool: list[UIElement]) -> tuple[list[UIElement], list[UIElement]]:
    """The controls offered as click options: ranked by the goal's distinctive words first and by the checklist's next
    line second (browser chrome and page controls compete alike), at most MAX_CLICKS plus ties; every control whose own
    text has a distinctive goal word is kept even beyond that cut. Returns (kept in screen order, kept best first)."""
    words = " ".join(distinctive_words(goal))
    scores = [(score(words, e)[1], score(next_line, e)[1]) if words else (0.0, score(next_line, e)[1]) for e in pool]
    if not any(g for g, _ in scores):  # the goal's words name no control: the old ranking over both texts
        kept = rank(f"{next_line} {goal}".strip(), pool, MAX_CLICKS, MAX_CLICKS * 3).kept
        return kept, kept
    combined = [round(g + NEXT_WEIGHT * n, 6) for g, n in scores]
    order = sorted(range(len(pool)), key=lambda i: (-combined[i], i))
    if len(pool) <= MAX_CLICKS:
        return pool, [pool[i] for i in order]
    cutoff = combined[order[MAX_CLICKS - 1]]
    keep = [i for i in order if combined[i] > cutoff or (combined[i] == cutoff and cutoff > 0)][: MAX_CLICKS * 3]
    keep = order[:MAX_CLICKS] if cutoff <= 0 else keep
    named = {i for i in range(len(pool)) if _parts(words, pool[i])[0] > 0}
    kept = set(keep) | named
    return [pool[i] for i in sorted(kept)], [pool[i] for i in order if i in kept]


def build_options(goal: str, scope: dict, history: list[StepRecord], snap: Snapshot | None, screen: str | None,
                  menus: bool = False, hint_p: float = 0.5, max_options: int = MAX_OPTIONS) -> list[Option]:
    """The candidate next actions, in a stable order, each with a unique key; at most `max_options` (the cap drops the
    least relevant first: tab, scroll, menu commands, extra type targets, then the lowest-ranked clicks)."""
    options: list[tuple[str, PlannerOutput | None, str]] = []
    prio: dict[str, float] = {}  # label -> how much the option matters when the list must be cut (lower: keep)
    progress = scope.get("progress")
    next_line = (progress or {}).get("next") or ""
    hinted = bool(history) and (history[-1].goal_probability or 0) >= hint_p  # Jev's goal check says it may be done
    if (progress and not next_line) or (not progress and any(h.succeeded for h in history)) or hinted:
        options.append(("done: the goal is already accomplished", PlannerOutput(done=True, reasoning="chosen from the listed actions"), "done"))
    own: set[str] = set()  # labels of the options the checklist's next line spells out
    if next_line:
        listed = checklist_actions(next_line, snap) if snap is not None else []
        own = {label for label, _, _ in listed}
        options += listed
    if snap is not None:
        pool = [e for e in snap.elements if e.interactive and e.enabled and _name(e)]
        top, best_first = ranked_clicks(goal, next_line, pool)
        click_rank = {id(e): i for i, e in enumerate(best_first)}
        seen: set[str] = set()
        counts: dict[str, int] = {}
        for e in top:
            counts[_name(e).lower()] = counts.get(_name(e).lower(), 0) + 1
        for e in top:
            label = f'click "{_labelled(e)}"'
            if counts[_name(e).lower()] > 1:  # two controls share a name: say where each is
                where = e.surroundings().strip() or e.role.removeprefix("AX")
                label += f" ({_quote(where, 40)})"
            if label in seen:
                continue
            seen.add(label)
            options.append((label, _plan(action="click", target={"id": e.id, "role": e.role, "description": _name(e)}), "click"))
            prio[label] = 1 + click_rank.get(id(e), 0) / 50
        quotes = quoted_strings(goal)[:MAX_TYPE_STRINGS]
        for q in quotes:
            for f in text_fields(snap)[:MAX_TYPE_FIELDS]:
                fname = _name(f) or f.role.removeprefix("AX").lower()
                label = f'type "{_quote(q)}" into "{_quote(fname)}"' + (" (focused)" if f.focused else "")
                options.append((label, _plan(action="type", text=q, target={"id": f.id, "role": f.role, "description": fname}), "type"))
                prio[label] = 1.1 if f.focused else 5
    for k in KEY_OPTIONS:
        options.append((f"press {k}", _plan(action="shortcut", keys=k), "key"))
        prio[f"press {k}"] = {"return": 1.0, "escape": 1.1}.get(k, 9)
    more = screen and re.search(r"\d+ more (?:not shown|controls below)", screen)
    if more:
        options.append(("scroll down", _plan(action="scroll", direction="down"), "scroll"))
        options.append(("scroll up", _plan(action="scroll", direction="up"), "scroll"))
        prio["scroll down"], prio["scroll up"] = 1.05, 1.05
    if menus and scope.get("menu_commands"):
        for c in scope["menu_commands"][:MAX_MENU]:
            path = c if isinstance(c, str) else c.get("path", "")
            if path:
                options.append((f'menu "{path}"', _plan(action="menu", command=path), "menu"))
                prio[f'menu "{path}"'] = 6
    options.append(("none of these: the goal needs something else, such as a shell command", None, "none"))
    kept, labels = [], set()
    for label, plan, kind in options:
        if label not in labels:
            labels.add(label)
            kept.append((label, plan, kind))
    if len(kept) > max_options:  # keep the most relevant, in their original order
        order = sorted(range(len(kept)), key=lambda i: (0 if kept[i][2] in ("done", "none") or kept[i][0] in own else prio.get(kept[i][0], 5), i))
        keep = set(order[:max_options])
        kept = [o for i, o in enumerate(kept) if i in keep]
    return [Option(f"o{i}", label, plan, kind, label in own) for i, (label, plan, kind) in enumerate(kept)]


def pick(probabilities: dict[str, float], options: list[Option]) -> tuple[Option, float]:
    """Jev's top option; a low-probability `done` gives way to the runner-up, because a wrong done ends the run."""
    by_key = {o.key: o for o in options}
    ranked = sorted(((k, float(p)) for k, p in probabilities.items() if k in by_key), key=lambda kv: -kv[1])
    key, p = ranked[0]
    if by_key[key].kind == "done" and p < DONE_MIN_P and len(ranked) > 1:
        key, p = ranked[1]
    return by_key[key], p


@dataclass
class JevPlanner:
    settings: object
    selector: object
    needs_snapshot: bool = True  # Agent and the eval pass `snap=` (the options come from the elements)
    last_messages: list = field(default_factory=list)
    last_prompt_tokens: int | None = None
    last_kind: tuple[str, float] | None = None
    last_options: list = field(default_factory=list)

    def next_step(self, goal: str, scope: dict, history: list[StepRecord], screen: str | None = None,
                  snap: Snapshot | None = None) -> PlannerOutput:
        from local_agent.selector.jev import JevError

        menus = bool(getattr(self.settings, "menus_on", lambda: False)())
        options = self.last_options = build_options(goal, scope, history, snap, screen, menus,
                                                    getattr(self.settings, "goal_hint_threshold", 0.5),
                                                    getattr(self.settings, "jev_max_options", MAX_OPTIONS))
        state = kind_state(goal, scope, history[-HISTORY_LINES:], screen)
        listing = "\n".join(f"{o.key}: {o.label}" for o in options)
        self.last_messages = [{"role": "user", "content": f"{state}\n\nOptions:\n{listing}"}]
        self.last_prompt_tokens = (len(state) + len(listing)) // 3
        pick_option = getattr(self.selector, "pick_option", None)
        try:
            answer = pick_option(state, QUESTION, {o.key: o.label for o in options}) if pick_option else None
        except JevError as e:
            raise PlannerError(f"jev mode: Jev failed ({e})") from e
        if answer is None:
            raise PlannerError("jev mode: this Jev backend can't choose the next action (needs JevK5 or OpenJev); "
                               "there is no planner model to fall back on")  # fmt: skip
        option, p = pick(answer.probabilities or {answer.choice: answer.probability}, options)
        self.last_kind = (option.label, p)
        if option.kind == "none" or option.plan is None:
            raise PlannerError(NO_SHELL)
        floor = getattr(self.settings, "jev_min_pick_p", 0.0)
        if p < floor and option.kind != "done" and not option.from_checklist:
            ranked = sorted(answer.probabilities or {}, key=lambda k: -answer.probabilities[k])
            labels = {o.key: o.label for o in options}
            top = "; ".join(labels[k] for k in ranked[:3] if k in labels)
            raise Unsure(f"unsure (p={p:.2f}): no option clearly fits; {top}"[:300])
        out = option.plan.model_copy(update={"reasoning": f"Jev chose: {option.label} (p={p:.2f})"})
        return out
