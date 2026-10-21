"""Cheap lexical ranking that narrows Jev's candidate list to the elements whose text matches the target."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from local_agent.observe.models import UIElement

STOPWORDS = {
    "the", "a", "an", "in", "on", "of", "to", "for", "at", "with", "and", "or", "that", "this", "is", "it",
    "its", "into", "from", "by", "near", "next", "top", "bottom", "left", "right", "window", "element",
}  # fmt: skip
# Role/subrole words count for less than visible text: every button matches "button".
ROLE_WEIGHT = 0.3


def words(text: str) -> list[str]:
    # Split camelCase / AXRoleNames, then keep alphanumeric tokens.
    text = re.sub(r"^AX", "", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


def _match(q: str, w: str) -> float:
    if q == w:
        return 1.0
    if len(q) >= 3 and len(w) >= 3 and (w.startswith(q) or q.startswith(w)):
        return 0.8
    if len(q) >= 4 and SequenceMatcher(None, q, w).ratio() >= 0.85:
        return 0.7
    return 0.0


def _best(q: str, ws: list[str]) -> float:
    return max((_match(q, w) for w in ws), default=0.0)


def score(target: str, e: UIElement) -> tuple[float, float]:
    """(text score, role score) of an element against the target description."""
    query = set(words(target))
    if not query:
        return 0.0, 0.0
    text_words = words(" ".join([e.title, e.label, e.placeholder, e.value[:60], e.help]))
    role_words = words(f"{e.role} {e.subrole}")
    text = sum(_best(q, text_words) for q in query)
    role = sum(_best(q, role_words) for q in query) * ROLE_WEIGHT
    return text, role


def prefilter(target: str, candidates: list[UIElement], top_k: int) -> list[UIElement]:
    """Top `top_k` candidates by text match, in their original order. Returns all candidates when
    nothing's visible text matches (e.g. icon-only buttons), so Jev still sees the right one."""
    if top_k <= 0 or len(candidates) <= top_k:
        return candidates
    scored = [(score(target, e), i) for i, e in enumerate(candidates)]
    if max(s[0] for s, _ in scored) == 0:
        return candidates
    ranked = sorted(scored, key=lambda t: (-(t[0][0] + t[0][1]), t[1]))[:top_k]
    return [candidates[i] for _, i in sorted(ranked, key=lambda t: t[1])]
