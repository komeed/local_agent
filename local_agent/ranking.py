"""Superlatives ("the latest dream video", "the oldest", "the first result") decided in code from the dates and positions
on screen, instead of asking a model to compare "2 days ago" with "3 weeks ago".

Dates come from an element's own text or its context (the card or row around it): relative ("2 days ago", "yesterday",
"Streamed 3 weeks ago", "just now") and absolute (2026-10-21, "Oct 21, 2026", "21 Oct"). Only candidates that also match the
object's other words ("dream") are ranked."""

from __future__ import annotations

import re
from datetime import datetime

from local_agent.observe.models import UIElement

UNITS = {"sec": 1, "second": 1, "min": 60, "minute": 60, "hr": 3600, "hour": 3600, "day": 86400, "week": 604800,
         "month": 2_592_000, "year": 31_536_000}  # fmt: skip
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = "(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?"
REL = re.compile(r"\b(\d+|an?|one)\s*(sec|second|min|minute|hr|hour|day|week|month|year)s?\s+ago\b", re.I)
WORD_REL = re.compile(r"\b(yesterday|today|just now)\b", re.I)
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MDY = re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I)
DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON}(?:,?\s+(\d{{4}}))?\b", re.I)
YOUNG = {"latest", "newest", "recent", "newer"}
OLD = {"oldest", "earliest", "older"}
FIRST = {"first", "top"}
LAST = {"last", "bottom"}
GENERIC = {"video", "videos", "post", "posts", "article", "item", "link", "button", "result", "one", "song", "track",
           "file", "photo", "image", "page", "entry", "episode", "thing", "stream"}  # fmt: skip


def superlative(text: str) -> str | None:
    """"young" | "old" | "first" | "last" for the superlative the text asks for, else None."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    for kind, group in (("young", YOUNG), ("old", OLD), ("first", FIRST), ("last", LAST)):
        if words & group:
            return kind
    return None


def date_fragment(text: str) -> str:
    """The first date-looking piece of `text` ("2 days ago", "Oct 21, 2026"), else ""."""
    for rx in (REL, WORD_REL, ISO, MDY, DMY):
        if m := rx.search(text):
            return m.group(0)
    return ""


def parse_age(text: str, now: datetime | None = None) -> float | None:
    """How many seconds before `now` the text says something happened; None when it carries no date."""
    now = now or datetime.now()
    if m := REL.search(text):
        n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
        return n * UNITS[m.group(2).lower()]
    if m := WORD_REL.search(text):
        return {"yesterday": 86400, "today": 0, "just now": 0}[m.group(1).lower()]
    try:
        if m := ISO.search(text):
            when = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m := MDY.search(text):
            when = _absolute(MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3), now)
        elif m := DMY.search(text):
            when = _absolute(MONTHS[m.group(2).lower()], int(m.group(1)), m.group(3), now)
        else:
            return None
    except ValueError:
        return None
    return max(0.0, (now - when).total_seconds())


def _absolute(month: int, day: int, year: str | None, now: datetime) -> datetime:
    when = datetime(int(year) if year else now.year, month, day)
    return datetime(when.year - 1, month, day) if not year and when > now else when


def required_words(obj: str) -> set[str]:
    from local_agent.goal_facts import content_words

    return content_words(obj) - GENERIC


def _text(e: UIElement) -> str:
    return " ".join([e.name(), e.value, e.context, e.surroundings()])


def _position(e: UIElement):
    return (round(e.frame.y / 8) if e.frame else 1e9, e.frame.x if e.frame else 0, e.id)


def pick_ranked(obj: str, candidates: list[UIElement], now: datetime | None = None) -> tuple[UIElement | None, str]:
    """(the element, why) for an object that carries a superlative, ranked among the candidates whose text or context has
    the object's other words. (None, "") when it can't be decided in code: no superlative, no match, or no dates to compare."""
    kind = superlative(obj)
    if kind is None:
        return None, ""
    need = required_words(obj)
    matching = [e for e in candidates if e.interactive and all(w in _text(e).lower() for w in need)]
    if not matching:
        return None, ""
    if kind in ("young", "old"):
        aged = [(age, _position(e), e) for e in matching if (age := parse_age(_text(e), now)) is not None]
        if not aged:
            return None, ""
        aged.sort(key=lambda t: (t[0] if kind == "young" else -t[0], t[1]))  # page order breaks ties
        best = aged[0][2]
        return best, f"{'youngest' if kind == 'young' else 'oldest'} of {len(aged)} dated matches ({date_fragment(_text(best))})"
    ordered = sorted(matching, key=_position)
    pick = ordered[0] if kind == "first" else ordered[-1]
    return pick, f"{kind} of {len(ordered)} matches by position"
