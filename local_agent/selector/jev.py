"""Jev (System-1) clients: pick the concrete UI element for a planner step and verify outcomes.

Two backends share one question/answer format (TypeSafe's /v1/systemone): `JevClient` talks to an OpenJev
server over HTTP; `JevK5Client` reads JevK5 GGUF through llama-server in-process. Candidates are narrowed by
role and a lexical pre-filter first. A `choice` question accepts at most 255 options, so larger candidate sets
run as a tournament: chunks of <=255 (one question per chunk), then the chunk winners in a final round.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from local_agent.config import JEV_MAX_CHOICES, Settings
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.prefilter import prefilter

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
    source: str = "jev"  # "planner" when the planner named the element by its screen-view number


def pick_by_id(snap: Snapshot, step: PlannerOutput) -> Selection | None:
    """The element the planner named by [number], if it exists and suits the action; None means ask Jev."""
    t = step.target
    if t is None or t.id is None:
        return None
    el = next((e for e in snap.elements if e.id == t.id), None)
    if el is None or (step.action == "type" and el.role not in TEXT_ROLES):
        return None
    return Selection(el, 1.0, 1.0, 1, 0, source="planner")


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

    def _choice_round(self, state: str, instructions: str, groups: list[list[UIElement]]) -> list[ChoiceAnswer]:
        questions, names = {}, {}
        for i, group in enumerate(groups):
            qid = f"pick_{i}"
            names[qid] = [_option_name(e) for e in group]
            questions[qid] = {
                "type": "choice",
                "instructions": instructions,
                "criteria": {_option_name(e): e.describe() for e in group},
            }
        answers = self.system_one(state, questions)
        return [_parse_choice(answers[qid], names[qid]) for qid in questions]

    def select(self, goal: str, snap: Snapshot, step: PlannerOutput) -> Selection:
        target = step.target.description if step.target else "the text field to type into"
        candidates = prefilter(target, filter_candidates(snap.elements, step), self.settings.prefilter_top_k)
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
                state, {"is_target": {"type": "noul", "instructions": f"Is this element {target}? {only.describe()}"}}
            )
            p = _parse_noul(answers["is_target"])
            return Selection(only, p, p, 1, 1)

        instructions = f"Which UI element is: {target}?"
        by_name = {_option_name(e): e for e in candidates}
        pool, rounds = candidates, 0
        while True:
            rounds += 1
            results = self._choice_round(state, instructions, chunk(pool, self.chunk_size))
            if len(results) == 1:
                final = results[0]
                break
            pool = [by_name[r.choice] for r in results]
        return Selection(by_name[final.choice], final.probability, final.confidence, len(candidates), rounds)

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


class JevK5Client(JevClient):
    """JevK5 GGUF on a local llama-server, read with jevk5's own stdlib client (no torch).

    JevK5 reads up to 16 options per forward pass and combines larger questions with its knockout readout,
    so each choice question costs ceil(n / 16) + 1 passes.
    """

    def __init__(self, settings: Settings, model=None):
        from jevk5 import JevK5GGUF

        self.settings = settings
        self.chunk_size = max(2, min(settings.jev_chunk_size, JEV_MAX_CHOICES))
        self.model = model or JevK5GGUF(
            url=settings.jevk5_url,
            temperature=settings.jevk5_temperature,
            knockout_temperature=settings.jevk5_knockout_temperature,
            timeout_s=settings.http_timeout_s,
        )

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
    if settings.jev_backend == "jevk5":
        return JevK5Client(settings)
    if settings.jev_backend == "openjev":
        return JevClient(settings)
    raise ValueError(f"unknown jev_backend {settings.jev_backend!r}; use 'jevk5' or 'openjev'")
