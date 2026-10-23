"""A System 1 made only of sentence embeddings: probabilities straight from cosine similarity.

Each choice option (an element's describe() text) and the question ("Which UI element is: <target>?") are embedded by
an Ollama embedding model; the option probabilities are a softmax over cosine(question, option) / temperature, plus a
small exact-word-overlap term (`cosine_lexical_weight`), because embeddings blur exactly the tokens that tell rows
apart (invoice-0425 vs invoice-0424). Embeddings are cached by text, so a screen's options are embedded once.

What it can't do is reason: it measures how alike two texts are, not whether a statement holds. So it answers element
picks (a similarity question), while `verify` ("did this action take effect?") is a UI-diff heuristic instead of a
model call, and negations or goal-dependent choices are out of reach. Answers use the same shapes as JevK5's.
"""

from __future__ import annotations

import math

import httpx

from local_agent.config import JEV_MAX_CHOICES, Settings
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import JevClient, JevError, Selection
from local_agent.selector.prefilter import _best, words

MAX_CACHE = 20_000
# Embedding models that were trained with task prefixes and score noticeably worse without them.
PREFIXES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "qwen3-embedding": ("Instruct: Find the UI element the user means\nQuery: ", ""),
}


def _prefixes(model: str) -> tuple[str, str]:
    return PREFIXES.get(model.split(":")[0], ("", ""))


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def softmax(scores: list[float], temperature: float) -> list[float]:
    top = max(scores)
    w = [math.exp((s - top) / temperature) for s in scores]
    total = sum(w)
    return [x / total for x in w]


def overlap(query: str, text: str) -> float:
    """Fraction of the query's words that appear in text (with the pre-filter's prefix/fuzzy matching)."""
    q = set(words(query))
    if not q:
        return 0.0
    ws = words(text)
    return sum(_best(w, ws) for w in q) / len(q)


class CosineClient(JevClient):
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.chunk_size = JEV_MAX_CHOICES  # one similarity pass handles any number of options
        self.client = client or httpx.Client(base_url=settings.ollama_url, timeout=settings.http_timeout_s)
        self.cache: dict[str, list[float]] = {}
        # A JevK5Client (llama-server up) that judges goal / safety / verify; cosine then only picks elements.
        self.judge: JevClient | None = None

    def checks(self) -> dict:
        j = self.judge is not None
        return {"goal": j, "safety": j, "verify": "jev" if j else "heuristic"}

    def health(self) -> bool:
        try:
            names = {m["name"] for m in self.client.get("/api/tags").json().get("models", [])}
        except (httpx.HTTPError, ValueError):
            return False
        m = self.settings.embed_model
        return m in names or f"{m}:latest" in names

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = list(dict.fromkeys(t for t in texts if t not in self.cache))
        if missing:
            r = self.client.post("/api/embed", json={"model": self.settings.embed_model, "input": missing})
            if r.status_code >= 400:
                raise JevError(f"Ollama embed HTTP {r.status_code}: {r.text[:300]}")
            if len(self.cache) + len(missing) > MAX_CACHE:
                self.cache.clear()
            for t, v in zip(missing, r.json()["embeddings"]):
                self.cache[t] = _unit(v)
        return [self.cache[t] for t in texts]

    def similarities(self, query: str, options: list[str]) -> list[float]:
        q_prefix, d_prefix = _prefixes(self.settings.embed_model)
        q, *docs = self.embed([q_prefix + query, *(d_prefix + o for o in options)])
        w = self.settings.cosine_lexical_weight
        return [sum(a * b for a, b in zip(q, d)) + w * overlap(query, o) for d, o in zip(docs, options)]

    def system_one(self, state: str, questions: dict[str, dict]) -> dict[str, dict]:
        out = {}
        for qid, q in questions.items():
            if q["type"] != "choice":
                raise JevError("the cosine backend answers choice questions only")
            names = list(q["criteria"])
            probs = softmax(self.similarities(q["instructions"], [q["criteria"][n] for n in names]),
                            self.settings.cosine_temperature)  # fmt: skip
            best = max(range(len(names)), key=probs.__getitem__)
            entropy = -sum(p * math.log(p) for p in probs if p > 0)
            confidence = 1 - entropy / math.log(len(names)) if len(names) > 1 else 1.0
            out[qid] = {"choice": names[best], "probabilities": dict(zip(names, probs)), "confidence": confidence}
        return out

    def choose(self, goal, snap, step, candidates, describe=UIElement.describe) -> Selection:
        if len(candidates) != 1:
            return super().choose(goal, snap, step, candidates, describe)
        # A lone candidate already matched the target's words in the pre-filter; how alike it reads decides the rest.
        target = step.target.description if step.target else "the text field to type into"
        sim = self.similarities(f"Which UI element is: {target}?", [describe(candidates[0])])[0]
        p = 1 / (1 + math.exp(-(sim - 0.5) / 0.05))
        return Selection(candidates[0], p, p, 1, 1)

    def command_safety(self, goal, step, snap, history) -> float | None:
        if self.judge:
            return self.judge.command_safety(goal, step, snap, history)
        return None  # safety needs judgment; without it every non-turbo command asks the user

    def pick_option(self, state: str, instructions: str, options: dict[str, str]):
        return None  # choosing the next action needs judgment; jev mode fails the step instead of guessing

    def step_kind(self, state: str):
        return None  # the next kind of step needs judgment; the planner chooses it itself

    def goal_done(self, goal: str, snap: Snapshot) -> float | None:
        if self.judge:
            return self.judge.goal_done(goal, snap)
        return None  # "is this done?" needs judgment, not similarity; the planner and the loop guard decide

    def verify(self, goal: str, step: PlannerOutput, element: UIElement | None, before: Snapshot,
               after: Snapshot) -> float:  # fmt: skip
        """Heuristic: typed text showing up counts as success; any other UI change as likely success; none as
        failure. With a judge, Jev decides."""
        if self.judge:
            return self.judge.verify(goal, step, element, before, after)
        if step.action == "type" and step.text and any(step.text[:30] in e.value for e in after.elements):
            return 0.95
        changed = {e.describe(80) for e in before.elements} ^ {e.describe(80) for e in after.elements}
        if before.window_title != after.window_title or before.app != after.app:
            return 0.9
        return 0.8 if changed else 0.1
