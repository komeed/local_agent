"""Cheap lexical ranking that narrows Jev's candidate list to the elements whose text matches the target."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from local_agent.observe.models import UIElement

STOPWORDS = {
    "the", "a", "an", "in", "on", "of", "to", "for", "at", "with", "and", "or", "that", "this", "is", "it",
    "its", "into", "from", "by", "near", "next", "top", "bottom", "left", "right", "window", "element",
}  # fmt: skip
# Role/subrole words count for less than visible text: every button matches "button".
ROLE_WEIGHT = 0.3
# The row/label/container an element sits in identifies it, but less than its own text does: "Delete" in another row
# should still beat "Edit" in the named row.
CONTEXT_WEIGHT = 0.5


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


def _parts(target: str, e: UIElement) -> tuple[float, float, float]:
    """(own text score, surroundings score, total). Each target word counts once, at its best weighted match: own text
    1.0, surroundings (row, form label, named containers) CONTEXT_WEIGHT, role ROLE_WEIGHT."""
    query = set(words(target))
    if not query:
        return 0.0, 0.0, 0.0
    text_words = words(" ".join([e.title, e.label, e.placeholder, e.value[:60], e.help]))
    ctx_words = words(e.surroundings())
    role_words = words(f"{e.role} {e.subrole}")
    text = ctx = total = 0.0
    for q in query:
        t, c, r = _best(q, text_words), _best(q, ctx_words), _best(q, role_words)
        text += t
        ctx += c
        total += max(t, c * CONTEXT_WEIGHT, r * ROLE_WEIGHT)
    return text, ctx, total


def score(target: str, e: UIElement) -> tuple[float, float]:
    """(own text score, total score) of an element against the target description."""
    text, _, total = _parts(target, e)
    return text, total


@dataclass
class PrefilterResult:
    kept: list[UIElement]
    cutoff: float = 0.0  # total score of the top_k-th candidate
    tie_size: int = 0  # candidates scoring exactly the cutoff
    truncated: bool = False  # max_k cut through that tie


def rank(target: str, candidates: list[UIElement], top_k: int, max_k: int = 0) -> PrefilterResult:
    """The `top_k` candidates whose text best matches the target, plus everything tied with the last of them (up to
    `max_k`), in their original order. Never cutting through a tie leaves the disambiguation to Jev instead of to
    document order. Returns all candidates when nothing's visible text or surroundings match (e.g. icon-only
    buttons), so Jev still sees the right one."""
    max_k = max(max_k, top_k)
    if top_k <= 0 or len(candidates) <= top_k:
        return PrefilterResult(candidates)
    parts = [_parts(target, e) for e in candidates]
    if not any(t or c for t, c, _ in parts):
        return PrefilterResult(candidates)
    totals = [round(p[2], 6) for p in parts]
    order = sorted(range(len(candidates)), key=lambda i: (-totals[i], i))
    last = order[top_k - 1]
    cutoff = totals[last]
    tied = [i for i in order if totals[i] == cutoff]
    if not (parts[last][0] or parts[last][1]):
        # The cutoff is filler (role-only or no match): widening it would just add noise.
        keep, truncated = order[:top_k], False
    else:
        widened = [i for i in order if totals[i] >= cutoff]
        keep, truncated = widened[:max_k], len(widened) > max_k
    return PrefilterResult([candidates[i] for i in sorted(keep)], cutoff, len(tied), truncated)


def prefilter(target: str, candidates: list[UIElement], top_k: int, max_k: int = 0) -> list[UIElement]:
    return rank(target, candidates, top_k, max_k).kept


QUOTED = re.compile(r"[\"“]([^\"”]+)[\"”]")
CAP_RUN = re.compile(r"\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*)+")


def specific_name(description: str, candidates: list[UIElement]) -> str:
    """The name the description insists on: quoted text, or a run of two or more capitalised words ("New Document"),
    minus words that are only role words of the candidates ("Text Area"). "" for descriptions without one ("the trash
    icon in the invoice-0441 row"). A lone capitalised word is ignored: at the start of a sentence it proves nothing."""
    names = QUOTED.findall(description) + CAP_RUN.findall(description)
    role_words = {w for e in candidates for w in words(f"{e.role} {e.subrole}")}
    kept = [n for n in names if any(_best(w, list(role_words)) < 0.8 for w in words(n))]
    return kept[0] if kept else ""


def missing_name(description: str, candidates: list[UIElement]) -> str:
    """The specific name in the description when no candidate's own text or surroundings matches any word of it
    (so the best "match" would be an unrelated element picked by Jev); "" when there's no such name or it exists."""
    name = specific_name(description, candidates)
    if not name or not candidates:
        return ""
    for e in candidates:
        text, ctx, _ = _parts(name, e)
        if text or ctx:
            return ""
    return name
