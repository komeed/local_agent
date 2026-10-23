"""Jev (System-1) clients: pick the concrete UI element for a planner step and verify outcomes.

Two backends share one question/answer format (TypeSafe's /v1/systemone): `JevClient` talks to an OpenJev
server over HTTP; `JevK5Client` reads JevK5 GGUF through llama-server in-process. Candidates are narrowed by
role and a lexical pre-filter first. A `choice` question accepts at most 255 options, so larger candidate sets
run as a tournament: chunks of <=255 (one question per chunk), then the chunk winners in a final round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import httpx

from local_agent.config import JEV_MAX_CHOICES, Settings
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.prefilter import missing_name, prefilter, score

# Roles treated as interchangeable when filtering by the planner's target role.
ROLE_GROUPS = [
    {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXSecureTextField"},
    {"AXButton", "AXMenuButton", "AXPopUpButton", "AXDisclosureTriangle"},
    {"AXMenuItem", "AXMenuBarItem"},
    {"AXCell", "AXRow"},
    {"AXCheckBox", "AXRadioButton", "AXTab"},
]
TEXT_ROLES = ROLE_GROUPS[0]


class JevError(RuntimeError):
    pass


@dataclass
class ChoiceAnswer:
    choice: str
    probability: float
    confidence: float
    probabilities: dict[str, float]


@dataclass
class Selection:
    element: UIElement
    probability: float
    confidence: float
    candidates: int
    rounds: int
    source: str = "jev"  # "planner": named by its screen-view number; "exact": its name equals the description
    margin: float = 1.0  # final probability minus the runner-up's; small when Jev couldn't tell two options apart
    # The planner's [number] that the ambiguity guard overruled (planner_pick), when Jev picked instead.
    overrode_id: int | None = None


def pick_by_id(snap: Snapshot, step: PlannerOutput) -> Selection | None:
    """The element the planner named by [number], if it exists and suits the action; None means ask Jev."""
    t = step.target
    if t is None or t.id is None:
        return None
    el = next((e for e in snap.elements if e.id == t.id), None)
    if el is None or (step.action == "type" and el.role not in TEXT_ROLES):
        return None
    return Selection(el, 1.0, 1.0, 1, 0, source="planner")


def same_named(snap: Snapshot, el: UIElement) -> list[UIElement]:
    """Other enabled controls of el's role group with exactly el's name: the rows' "Delete" buttons, or unnamed fields
    told apart only by their labels. They read the same in the planner's screen view."""
    name = el.name()
    return [e for e in snap.elements
            if e.id != el.id and e.interactive and e.enabled and e.name() == name and _role_matches(el.role, e.role)]  # fmt: skip


@dataclass
class PlannerPick:
    selection: Selection | None  # the planner's element, or None: ask Jev
    overruled: int | None = None  # the planner's id, when the guard sent the pick to Jev
    note: str = ""


def planner_pick(snap: Snapshot, step: PlannerOutput) -> PlannerPick:
    """pick_by_id plus two guards. First, the number must match the description: an element sharing no word with it
    (text, surroundings or role) is dropped and Jev picks from the description. Second, an ambiguity guard. A small planner reading many identical lines ("[57] Delete" once per row)
    often gives the wrong number while its description names the right row. When a same-named control matches the
    description better than the numbered one, the number is dropped and Jev picks from the description. If the
    description doesn't tell them apart, the number is kept: Jev would have nothing better to go on."""
    sel = pick_by_id(snap, step)
    if sel is None:
        return PlannerPick(None)
    el, description = sel.element, step.target.description.strip()
    if description and score(description, el)[1] == 0:
        # Nothing in the element (text, surroundings, role) resembles the description: the number is a misread.
        note = (f"[{el.id}] ({el.describe(60)}) does not match the description {description!r}; "
                "Jev chose from the description instead. Use the number of the control you mean, or none")  # fmt: skip
        exact = exact_match(description, filter_candidates(snap.elements, step))
        if exact is not None:
            note += f"; picked [{exact.id}] {exact.name()!r}, whose name matches exactly"
            return PlannerPick(Selection(exact, 1.0, 1.0, 1, 0, source="exact"), overruled=el.id, note=note)
        return PlannerPick(None, overruled=el.id, note=note)
    twins = same_named(snap, el)
    if not twins or not description:
        return PlannerPick(sel)
    mine = score(description, el)[1]
    better = [t for t in twins if score(description, t)[1] > mine + 1e-6]
    if not better:
        return PlannerPick(sel)
    note = (f"[{el.id}] is one of {len(twins) + 1} {el.name()!r} controls and the description matches "
            f"{'another' if len(better) == 1 else f'{len(better)} others'} better; Jev chose instead")  # fmt: skip
    return PlannerPick(None, overruled=el.id, note=note)


ROLE_WORDS = {"the", "a", "an", "button", "menu", "item", "field", "icon", "tab", "checkbox", "link", "option"}


def _norm(text: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in ROLE_WORDS)


def exact_match(description: str, pool: list[UIElement]) -> UIElement | None:
    """The one candidate whose own name (title, label or value; not surroundings) equals the description once
    quotes, case and role words are dropped. Several equal names ("Delete" in every row) or none: None."""
    want = _norm(description)
    if not want:
        return None
    named = [e for e in pool if _norm(e.name()) == want]
    return named[0] if len(named) == 1 else None


def _role_matches(wanted: str, role: str) -> bool:
    if role == wanted:
        return True
    return any(wanted in g and role in g for g in ROLE_GROUPS)


def filter_candidates(elements: list[UIElement], step: PlannerOutput) -> list[UIElement]:
    enabled = [e for e in elements if e.enabled and e.interactive]
    wanted = (step.target.role if step.target else "any").strip()
    if step.action == "type":
        pool = [e for e in enabled if e.role in TEXT_ROLES]
    elif wanted and wanted.lower() != "any":
        pool = [e for e in enabled if _role_matches(wanted, e.role)]
    else:
        pool = enabled
    return pool or enabled


def chunk(items: list, size: int) -> list[list]:
    """Split into the fewest groups of <= size, balanced so no group is left with a single option."""
    n_groups = max(1, -(-len(items) // size))
    base, extra = divmod(len(items), n_groups)
    out, start = [], 0
    for i in range(n_groups):
        end = start + base + (1 if i < extra else 0)
        out.append(items[start:end])
        start = end
    return out


def _option_name(e: UIElement) -> str:
    return f"e{e.id}"


def parse_answers(data: dict) -> dict[str, dict]:
    """Normalize OpenJev answers into {question_id: answer_dict}. Accepts dict-keyed or list-of-answers shapes."""
    answers = data.get("answers", data)
    if isinstance(answers, list):
        out = {}
        for a in answers:
            qid = a.get("id") or a.get("question_id") or a.get("name")
            if qid is None:
                raise JevError(f"Answer without id: {a}")
            out[qid] = a
        return out
    if isinstance(answers, dict):
        # Possibly grouped by type: {"choices": {...}, "nouls": {...}}
        if set(answers) & {"choices", "nouls", "scores"}:
            out = {}
            for group in answers.values():
                out.update(group)
            return out
        return answers
    raise JevError(f"Unexpected OpenJev response: {data!r:.300}")


def _parse_choice(a: dict, option_names: list[str]) -> ChoiceAnswer:
    probs = a.get("probabilities") or {}
    if isinstance(probs, list):
        probs = dict(zip(option_names, probs))
    choice = a.get("choice") or a.get("value")
    if choice is None and probs:
        choice = max(probs, key=probs.get)
    if choice not in option_names:
        raise JevError(f"Jev chose unknown option {choice!r}")
    p = float(probs.get(choice, a.get("top_probability", 0.0)))
    conf = float(a.get("confidence", p))
    return ChoiceAnswer(choice=choice, probability=p, confidence=conf, probabilities=probs)


def _parse_noul(a: dict) -> float:
    v = a.get("noul", a.get("probability", a.get("value")))
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if v is None:
        raise JevError(f"noul answer without probability: {a}")
    return float(v)


class JevClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        headers = {"Authorization": f"Bearer {settings.jev_api_key}"} if settings.jev_api_key else {}
        self.client = client or httpx.Client(
            base_url=settings.jev_url, timeout=settings.http_timeout_s, headers=headers
        )
        self.chunk_size = max(2, min(settings.jev_chunk_size, JEV_MAX_CHOICES))

    def health(self) -> bool:
        try:
            return self.client.get("/v1/models").status_code == 200
        except httpx.HTTPError:
            return False

    def system_one(self, state: str, questions: dict[str, dict]) -> dict[str, dict]:
        r = self.client.post(
            "/v1/systemone",
            json={"model": self.settings.jev_model, "state": state, "questions": questions},
        )
        if r.status_code >= 400:
            raise JevError(f"OpenJev HTTP {r.status_code}: {r.text[:300]}")
        return parse_answers(r.json())

    # --- element selection -------------------------------------------------------------------------------

    def _choice_round(
        self, state: str, instructions: str, groups: list[list[UIElement]], describe: Callable[[UIElement], str]
    ) -> list[ChoiceAnswer]:
        questions, names = {}, {}
        for i, group in enumerate(groups):
            qid = f"pick_{i}"
            names[qid] = [_option_name(e) for e in group]
            questions[qid] = {
                "type": "choice",
                "instructions": instructions,
                "criteria": {_option_name(e): describe(e) for e in group},
            }
        answers = self.system_one(state, questions)
        return [_parse_choice(answers[qid], names[qid]) for qid in questions]

    def select(self, goal: str, snap: Snapshot, step: PlannerOutput) -> Selection:
        target = step.target.description if step.target else "the text field to type into"
        pool = filter_candidates(snap.elements, step)
        if step.target and (name := missing_name(target, pool)):
            raise JevError(f'no element named "{name}" on screen')
        if step.target and step.action != "type" and (exact := exact_match(target, pool)) is not None:
            return Selection(exact, 1.0, 1.0, len(pool), 0, source="exact")  # no model call
        candidates = prefilter(target, pool, self.settings.prefilter_top_k, self.settings.prefilter_max_k)
        return self.choose(goal, snap, step, candidates)

    def choose(
        self,
        goal: str,
        snap: Snapshot,
        step: PlannerOutput,
        candidates: list[UIElement],
        describe: Callable[[UIElement], str] = UIElement.describe,
    ) -> Selection:
        """Ask Jev which of `candidates` the step targets (a tournament when there are more than chunk_size)."""
        target = step.target.description if step.target else "the text field to type into"
        if not candidates:
            raise JevError("No candidate elements in the current window")
        state = (
            f"User goal: {goal}\n"
            f"App: {snap.app}  Window: {snap.window_title}\n"
            f"Next step: {step.summary()}\n"
            f"Element to find: {target}"
        )
        if len(candidates) == 1:
            # A one-option choice is meaningless; ask whether the lone candidate is the target instead.
            only = candidates[0]
            answers = self.system_one(
                state, {"is_target": {"type": "noul", "instructions": f"Is this element {target}? {describe(only)}"}}
            )
            p = _parse_noul(answers["is_target"])
            return Selection(only, p, p, 1, 1)

        instructions = f"Which UI element is: {target}?"
        by_name = {_option_name(e): e for e in candidates}
        pool, rounds = candidates, 0
        while True:
            rounds += 1
            results = self._choice_round(state, instructions, chunk(pool, self.chunk_size), describe)
            if len(results) == 1:
                final = results[0]
                break
            pool = [by_name[r.choice] for r in results]
        others = [float(p) for name, p in final.probabilities.items() if name != final.choice]
        margin = final.probability - max(others, default=0.0)
        return Selection(by_name[final.choice], final.probability, final.confidence, len(candidates), rounds,
                         margin=margin)  # fmt: skip

    def checks(self) -> dict:
        """Which judgments this selector makes (the `start` event's `checks`)."""
        return {"goal": True, "safety": True, "verify": "jev"}

    # --- goal completion -------------------------------------------------------------------------------------

    def goal_done(self, goal: str, snap: Snapshot) -> float | None:
        """Probability that the screen shows `goal` fully accomplished; None when the backend can't judge it.

        Only the goal and the screen go in, not the steps taken: with "opened gmail.com" in the state, a yes/no model
        tends to answer from the history instead of from what's on screen."""
        answers = self.system_one(
            goal_state(goal, snap, self.settings.goal_view_tokens),
            {"goal_done": {"type": "noul", "instructions": GOAL_QUESTION,
                           "criteria": {"true": "the screen shows the goal done", "false": "not done yet"}}},
        )  # fmt: skip
        return _parse_noul(answers["goal_done"])

    # --- step kind ------------------------------------------------------------------------------------------

    def step_kind(self, state: str) -> ChoiceAnswer | None:
        """Which kind of step comes next (STEP_KINDS), for planner_mode="kind": the small planner then only fills
        in the details. `state` is planner/kind.py::kind_state. None when the backend can't judge."""
        kinds = step_kinds(self.settings)
        answers = self.system_one(state, {"kind": {"type": "choice", "instructions": KIND_QUESTION, "criteria": kinds}})
        return _parse_choice(answers["kind"], list(kinds))

    def pick_option(self, state: str, instructions: str, options: dict[str, str]) -> ChoiceAnswer | None:
        """A `choice` over `options` (key -> description) for planner_mode="jev". None when the backend can't judge."""
        answers = self.system_one(state, {"pick": {"type": "choice", "instructions": instructions, "criteria": options}})
        return _parse_choice(answers["pick"], list(options))

    # --- terminal command safety ---------------------------------------------------------------------------

    def command_safety(self, goal: str, step: PlannerOutput, snap: Snapshot, history: list) -> float | None:
        """Probability that `step.command` is safe to run without asking the user (local_agent/safety.py); None
        when the backend can't judge. Unlike goal_done, the steps so far are context here: they show what the
        command is for."""
        from local_agent.safety import SAFETY_QUESTION, safety_state

        state = safety_state(goal, step.command or "", step.reasoning, snap.app, snap.window_title,
                             [h.plan for h in history[-5:]])  # fmt: skip
        answers = self.system_one(
            state,
            {"safe": {"type": "noul", "instructions": SAFETY_QUESTION,
                      "criteria": {"true": "safe to run without asking", "false": "ask the user first"}}},
        )  # fmt: skip
        return _parse_noul(answers["safe"])

    # --- verification ------------------------------------------------------------------------------------

    def verify(self, goal: str, step: PlannerOutput, element: UIElement | None, before: Snapshot, after: Snapshot) -> float:
        """Probability that the UI changed the way this step intended."""
        # The overall goal is left out on purpose: with it, Jev tends to answer "is the goal done?" (usually no)
        # instead of "did this one step take effect?". Wording tuned on live TextEdit cases.
        before_names = {e.describe(80) for e in before.elements}
        after_names = {e.describe(80) for e in after.elements}
        appeared = sorted(after_names - before_names)[:25]
        disappeared = sorted(before_names - after_names)[:25]
        state = (
            f"Action performed: {step.summary()}\n"
            f"Target element: {element.describe() if element else 'n/a'}\n"
            f"UI elements before: {len(before.elements)}, after: {len(after.elements)}\n"
            f"Appeared or changed: {appeared or 'NOTHING'}\n"
            f"Disappeared or changed: {disappeared or 'NOTHING'}"
        )
        answers = self.system_one(
            state,
            {
                "changed": {
                    "type": "noul",
                    "instructions": "Is there evidence in the UI changes that the action worked (for example a menu "
                    "opened, text was selected, text was entered, a window or dialog appeared)? If nothing changed, "
                    "answer false.",
                    "criteria": {
                        "true": "the changes show the action took effect",
                        "false": "no relevant change",
                    },
                }
            },
        )
        return _parse_noul(answers["changed"])


GOAL_QUESTION = (
    "Does the current screen show that the user's goal is fully accomplished? Answer true only if the screen shows "
    "the result the goal asks for (for example the requested page or document is open, the setting is changed, the "
    "text is entered). Answer false if only part of the goal is done, it is still loading or in progress, or "
    "something else is shown."
)


KIND_QUESTION = "Which kind of step should the agent take next toward the user's goal?"
# Option descriptions are what Jev compares; they name the typical tasks of each kind, since that's what the goal says.
STEP_KINDS = {
    "done": "Finish: the steps so far already did everything the goal asks, or their output or the screen already "
    "answers the goal's question",
    "terminal": "Run a shell command: open an app or website that is not on screen yet, or list, create, move or "
    "read files and folders",
    "menu": "Run a command from the app's menus: new document, save, save as, copy, paste, undo, find, bold, "
    "close window",
    "shortcut": "Press a plain key: close an open menu or dialog with escape, confirm with return, tab, arrows",
    "click": "Click something on screen: a button, a checkbox, a pop-up, a field, or a file in a list; also a "
    "dialog, sheet or error message that must be dealt with first",
    "type": "Type text into a text field that is focused or on screen",
    "scroll": "Scroll, because what the goal needs is further down or up and not visible on screen",
}

STEP_KINDS_OLD = {
    "done": "Finish: the steps so far already did everything the goal asks, or their output or the screen already "
    "answers the goal's question",
    "terminal": "Run a shell command: open an app or website that is not on screen yet, or list, create, move or "
    "read files and folders",
    "shortcut": "Press a keyboard shortcut: select all, new document, save, find, copy, paste, undo, bold, close an "
    "open menu with escape, confirm with return",
    "click": "Click something on screen: a button, a menu or menu item, a checkbox, a pop-up, a field, or a file "
    "in a list; also a dialog, sheet or error message that must be dealt with first",
    "type": "Type text into a text field that is focused or on screen",
    "scroll": "Scroll, because what the goal needs is further down or up and not visible on screen",
}


def step_kinds(settings: Settings) -> dict:
    """The kinds Jev chooses among: with menu actions on, `menu` takes over the menu commands and shortcut list."""
    return STEP_KINDS if settings.menus_on() else STEP_KINDS_OLD



def goal_state(goal: str, snap: Snapshot, view_tokens: int) -> str:
    from local_agent.observe.screen_view import render

    return f"User goal: {goal}\nCurrent screen:\n{render(snap, view_tokens, numbered=False)}"


def _gguf_class():
    """jevk5's llama-server client with two changes: `cache_prompt` is sent as configured (jevk5 hard-codes False), so
    llama-server reuses the KV of the prompt prefix its slot already holds (no extra RAM: that is what `--cache-ram 0`
    leaves alone), and each call's `prompt_n` (tokens evaluated), `cache_n` (tokens reused) and server time are counted."""
    from jevk5 import JevK5GGUF

    class CachedGGUF(JevK5GGUF):
        cache_prompt = True

        def _logprobs(self, prompt: str):
            tokens = self._post("/tokenize", {"content": prompt, "add_special": False, "parse_special": True})["tokens"]
            out = self._post("/completion", {"prompt": tokens, "n_predict": 1, "n_probs": self.top_k, "temperature": 0,
                                             "cache_prompt": self.cache_prompt})  # fmt: skip
            top = out["completion_probabilities"][0]["top_logprobs"]
            timings = out.get("timings") or {}
            evaluated = timings.get("prompt_n", out.get("tokens_evaluated", 0))
            seconds = (timings.get("prompt_ms", 0.0) + timings.get("predicted_ms", 0.0)) / 1000
            stats = self.stats
            stats["calls"] += 1
            stats["prompt_n"] += evaluated
            stats["cache_n"] += timings.get("cache_n", 0)
            stats["server_s"] += seconds
            return {e["token"]: e["logprob"] for e in top}, out.get("tokens_evaluated", evaluated), seconds

    CachedGGUF.stats = {"calls": 0, "prompt_n": 0, "cache_n": 0, "server_s": 0.0}
    return CachedGGUF


class JevK5Client(JevClient):
    """JevK5 GGUF on a local llama-server, read with jevk5's own stdlib client (no torch).

    JevK5 reads up to 16 options per forward pass and combines larger questions with its knockout readout,
    so each choice question costs ceil(n / 16) + 1 passes.
    """

    def __init__(self, settings: Settings, model=None):
        self.settings = settings
        self.chunk_size = max(2, min(settings.jev_chunk_size, JEV_MAX_CHOICES))
        self.model = model or _gguf_class()(
            url=settings.jevk5_url,
            temperature=settings.jevk5_temperature,
            knockout_temperature=settings.jevk5_knockout_temperature,
            timeout_s=settings.http_timeout_s,
        )
        if hasattr(self.model, "cache_prompt"):
            self.model.cache_prompt = settings.jev_cache_prompt

    def take_stats(self) -> dict:
        """Jev calls, prompt tokens evaluated / reused and server seconds since the last call of this method."""
        stats = getattr(self.model, "stats", None)
        if stats is None:
            return {}
        out = dict(stats)
        stats.update(calls=0, prompt_n=0, cache_n=0, server_s=0.0)
        return out

    def health(self) -> bool:
        try:
            return httpx.get(f"{self.settings.jevk5_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def system_one(self, state: str, questions: dict[str, dict]) -> dict[str, dict]:
        try:
            return {qid: self.model.decide(state, q) for qid, q in questions.items()}
        except OSError as e:  # urllib errors from llama-server
            raise JevError(f"llama-server at {self.settings.jevk5_url}: {e}") from e


def make_selector(settings: Settings) -> JevClient:
    from local_agent.selector import catalog

    settings = catalog.resolve(settings)
    if settings.jev_backend == "jevk5":
        return JevK5Client(settings)
    if settings.jev_backend == "openjev":
        return JevClient(settings)
    if settings.jev_backend == "cosine":
        from local_agent.selector.cosine import CosineClient

        client = CosineClient(settings)
        # Cosine only measures similarity: if llama-server is up, JevK5 judges goal / safety / verify.
        try:
            judge = JevK5Client(settings)
            client.judge = judge if judge.health() else None
        except ImportError:
            pass
        return client
    raise ValueError(f"unknown jev_backend {settings.jev_backend!r}; use 'jevk5', 'openjev' or 'cosine'")
