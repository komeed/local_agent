"""Choosing the official site from a search results page, in code over the page's links (Phase 3 navigation).

A link is a candidate when it points to another site than the search engine's own, and neither it nor its surroundings
say Sponsored / Ad / Promoted. Candidates whose host contains a word of the name come first, in page order; otherwise
the caller asks Jev."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from local_agent.observe.models import Snapshot, UIElement

SPONSORED = re.compile(r"\b(?:sponsored|ads?|promoted)\b", re.I)
MAX_FOR_JEV = 10


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower() if url else ""


def base_host(host: str) -> str:
    """The last two labels: google.com for maps.google.com (good enough to tell a site from its engine)."""
    return ".".join(host.split(".")[-2:])


def is_sponsored(e: UIElement) -> bool:
    return bool(SPONSORED.search(" ".join([e.name(), e.context, e.path.replace(">", " ").replace(":", " ")])))


def organic_links(snap: Snapshot) -> list[UIElement]:
    """Page links to http(s) hosts other than the engine's, without sponsored ones, one per URL, in page order."""
    engine = base_host(host_of(snap.url))
    seen: set[str] = set()
    out = []
    for e in snap.elements:
        if e.role != "AXLink" or not e.interactive or not re.match(r"https?://", e.href or ""):
            continue
        host = host_of(e.href)
        if not host or (engine and base_host(host) == engine) or is_sponsored(e) or e.href in seen:
            continue
        seen.add(e.href)
        out.append(e)
    return sorted(out, key=lambda e: (round(e.frame.y / 8) if e.frame else 1e9, e.frame.x if e.frame else 0))


def name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 1]


def host_score(host: str, words: list[str]) -> int:
    labels = host.split(".")
    return sum(1 for w in words if any(w in label for label in labels))


def pick_by_host(links: list[UIElement], name: str) -> UIElement | None:
    """The link whose host contains most of the name's words (page order breaks ties); None if no host contains any."""
    words = name_words(name)
    best, best_score = None, 0
    for e in links:
        score = host_score(host_of(e.href), words)
        if score > best_score:
            best, best_score = e, score
    return best


def matches_name(e: UIElement, name: str) -> bool:
    return host_score(host_of(e.href), name_words(name)) > 0
