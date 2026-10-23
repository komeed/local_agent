"""Facts a goal states outright, checked against the screen in code instead of asking a model.

`type "hello world"` is a text fact (some text field shows it); `open a new document in TextEdit` is an app fact
(TextEdit is in front). When a goal yields facts and all hold, the run is done; goals without facts go to Jev."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote_plus, urlparse

from local_agent.act.browser import KNOWN_BROWSERS
from local_agent.apps import installed_apps, resolve_app
from local_agent.forms import find_form, success_shown
from local_agent.observe.models import Snapshot
from local_agent.selector.jev import TEXT_ROLES

QUOTE = r"[\"“‘']([^\"”’']+?)[\"”’']"
TEXT_RE = re.compile(rf"\b(?:type|write|enter)\b[^\"“‘']*?{QUOTE}", re.I)
# Words an app name may follow: "open Mail", "in textedit", "launch Safari".
# The lookahead keeps matches from consuming each other ("open a new document in textedit").
APP_AFTER_RE = re.compile(
    r"\b(?:open|in|launch)\s+(?:the\s+|a\s+|an\s+)?(?=([A-Za-z0-9][\w.+-]*(?:\s+[A-Za-z0-9][\w.+-]*){0,2}))", re.I
)
# "save it as "x.txt"", "save the file as x.txt", "save as x": the front window is then titled with the file name.
SAVE_RE = re.compile(
    rf"\bsave\b(?:\s+(?:it|this|that|the\s+\w+|\w+))?\s+as\s+(?:{QUOTE}|([\w.\-]+\w))", re.I
)
CAPITALISED_RE = re.compile(r"\b([A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*){0,2})")
# Apps named like common words ("mail", "photos", "music", "maps") count only right after open/in/launch or when
# capitalised: those are the only two places this module looks for app names.


@dataclass(frozen=True)
class Fact:
    kind: str  # "outline" (a planned step, judged by Jev) | "text" | "app" | "saved" | "new" (the noun) | "site" (name or domain) | "search" (the quoted query)
    value: str
    pos: int = field(default=0, compare=False)  # where the goal mentions it, to order the checklist
    alias: str = field(default="", compare=False)  # site facts: a host our own navigation was redirected to
    data: str = field(default="", compare=False)  # field facts: the value to enter; submit: the button's name
    meta: str = field(default="", compare=False)  # field facts: text | select | checkbox

    def label(self) -> str:
        return self.value if self.kind == "app" else f'"{self.value}"'


NEW_DOC_RE = re.compile(r"\b(?:new|blank)\s+(?:document|file)\b", re.I)
# "open/create/start/make (a) new <noun>", and "new <noun> in <App>".
NEW_VERB_RE = re.compile(r"\b(?:open|create|start|make|begin)\s+(?:a\s+|an\s+|the\s+)?new\s+([a-z][\w-]*)", re.I)
NEW_IN_RE = re.compile(r"\bnew\s+([a-z][\w-]*)\s+in\b", re.I)
NOT_NOUNS = {"a", "an", "the", "and", "or", "in", "on", "of", "to", "for", "with", "at", "from", "by", "one"}
DOC_NOUNS = {"document", "file"}  # their "new" shows on screen (an untitled / empty document); others are remembered


# "open youtube in Arc", "go to github.com", "visit reddit": a site by name or domain. A bare name counts after
# go to / visit, or after open when a browser is named; a domain (dots + a TLD that isn't a file extension) always.
SITE_RE = re.compile(
    r"\b(open|go\s+to|goto|visit|navigate\s+to|browse\s+to)\s+(?:the\s+)?(?:(?:web\s*)?site\s+)?(?:https?://)?"
    r"((?:[a-z0-9][\w-]*\.)+[a-z]{2,}|[a-z][a-z0-9-]*(?:\s+(?!(?:in|on|and|then|to|for|using|with|from|at|via|so|but)\b)"
    r"[a-z][a-z0-9-]*){0,2})(?=[\s,;!?/]|\.(?:\s|$)|$)", re.I)
NOT_SITES = NOT_NOUNS | {"new", "blank", "my", "your", "it", "up", "app", "application", "file", "document", "folder",
                         "tab", "window", "settings", "preferences", "this", "that", "page", "browser", "next", "previous", "back",
                         "top", "bottom", "line", "end", "start", "beginning", "first", "last", "menu", "dock", "desktop"}  # fmt: skip
FILE_EXTENSIONS = {"txt", "pdf", "doc", "docx", "md", "csv", "png", "jpg", "jpeg", "gif", "zip", "json", "xlsx",
                   "pptx", "rtf", "py", "js", "html", "htm", "app", "dmg", "log", "xml", "yaml", "yml", "sh", "tar"}  # fmt: skip
SEARCH_RE = re.compile(rf"\bsearch(?:ing)?(?:\s+for)?\s+{QUOTE}", re.I)


def open_command(fact: Fact, facts: list[Fact] | None = None) -> str:
    """`open -a "<Browser>" "<url>"` with the goal's browser, else plain `open "<url>"` (the default browser)."""
    browser = next((f.value for f in facts or [] if f.kind == "app" and is_browser(f.value)), None)
    url = site_url(fact.value)
    return f'open -a "{browser}" "{url}"' if browser else f'open "{url}"'


# "click (on) the latest dream video", "open the pricing page", "play the first song": a thing on screen to click.
CLICK_RE = re.compile(
    r"\b(click(?:\s+on)?|press|tap|play|open)\s+(?:the\s+|a\s+|an\s+)?([^,.;]+?)"
    r"(?=\s+(?:and|then|in|on|using|with)\b|[,.;]|$)", re.I)
NOT_CLICK_OBJECTS = {"new", "blank", "document", "file", "folder", "app", "application", "tab", "window", "it", "this", "that",
                     "up", "my", "your", "browser", "settings", "preferences", "some", "any", "something", "anything", "all", "every"}  # fmt: skip
SUPERLATIVE_WORDS = {"latest", "newest", "most", "recent", "oldest", "earliest", "first", "last", "top", "best", "newer", "older"}
STOP_WORDS = {"the", "a", "an", "on", "in", "of", "to", "and", "or", "for", "with", "at", "from", "by", "this", "that", "button",
              "link", "item", "one", "result", "thing"}  # fmt: skip


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS and w not in SUPERLATIVE_WORDS and len(w) > 1}


def site_url(value: str) -> str:
    return f"https://{value if '.' in value else value + '.com'}"


def is_browser(app: str) -> bool:
    return any(name.lower() in app.lower() for name in KNOWN_BROWSERS.values())


# "on youtube in Arc", "at github.com using Safari": a site named by where to be, with the browser right after.
SITE_ON_RE = re.compile(r"\b(?:on|at)\s+(?:the\s+)?((?:[a-z0-9][\w-]*\.)+[a-z]{2,}|[a-z][a-z0-9-]*)\s+(?:in|with|using)\s+([a-z][\w ]*)", re.I)


def _site_facts(goal: str, apps: list[str], found: list[str]) -> list[Fact]:
    out = []
    has_browser = any(is_browser(a) for a in found)
    for m in SITE_ON_RE.finditer(re.sub(QUOTE, " ", goal)):
        noun, after = m.group(1).lower(), m.group(2).lower()
        if has_browser and noun not in NOT_SITES and not resolve_app(noun, apps) and any(
                is_browser(a) and after.startswith(a.lower().split()[0]) for a in found):
            if "." not in noun or noun.rsplit(".", 1)[1] not in FILE_EXTENSIONS:
                out.append(Fact("site", noun, m.start(1)))
    for m in SITE_RE.finditer(re.sub(QUOTE, " ", goal)):
        verb, noun = m.group(1).lower(), re.sub(r"\s+", " ", m.group(2).lower().rstrip("."))
        if noun.split()[0] in NOT_SITES or resolve_app(noun, apps):
            continue
        if "." in noun:
            if noun.rsplit(".", 1)[1] in FILE_EXTENSIONS:
                continue
        elif not (has_browser or not verb.startswith("open")):
            continue  # a bare "open <name>" with no browser named is an app we don't have
        if Fact("site", noun) not in out:
            out.append(Fact("site", noun, m.start(2)))
    return out


def _click_facts(goal: str, apps: list[str], facts: list[Fact]) -> list[Fact]:
    """`click` facts: the object of click / press / tap / play / open when it is not an app, a site, a file or a "new"
    thing the other facts already cover. They hold after a verified click that was chosen for that object (the loop)."""
    taken = set()
    for f in facts:
        taken |= set(re.findall(r"[a-z0-9]+", f.value.lower()))
    out = []
    for m in CLICK_RE.finditer(re.sub(QUOTE, " ", goal)):
        obj = re.sub(r"\s+", " ", m.group(2).strip())
        if m.group(1).lower() == "open" and len(content_words(obj)) < 2:
            continue  # "open gmail" names an app or site, not a thing to click
        words = re.findall(r"[a-z0-9]+", obj.lower())
        if not words or words[0] in NOT_CLICK_OBJECTS or resolve_app(obj, apps) or not content_words(obj):
            continue
        if set(words) <= taken or obj.lower().split()[-1].rsplit(".", 1)[-1] in FILE_EXTENSIONS and "." in obj:
            continue
        if re.match(r"(?:new|blank)\b", obj, re.I) or any(w in ("document", "file") for w in words[:1]):
            continue
        if Fact("click", obj) not in out:
            out.append(Fact("click", obj, m.start(2)))
    return out


def _search_facts(goal: str) -> list[Fact]:
    return [Fact("search", m.group(1).strip(), m.start()) for m in SEARCH_RE.finditer(goal) if m.group(1).strip()]


def _at(goal: str, value: str) -> int:
    return max(goal.lower().find(value.lower()), 0)


def parse_facts(goal: str, apps: list[str] | None = None, progress: bool = False) -> list[Fact]:
    """Facts: the app first (it has to be in front), then in the order the goal mentions them. `progress=True` adds the ones only worth showing as a checklist."""
    facts = _parse_facts(goal, installed_apps() if apps is None else apps)
    if progress:
        facts += new_facts(goal)
    return sorted(facts, key=lambda f: (f.kind != "app", f.pos))  # the app comes first, then goal order


def new_facts(goal: str) -> list[Fact]:
    """`new` facts: the noun of "open a new conversation", "start a new chat", "new tab in Safari", "new document"."""
    found: dict[str, int] = {}
    for rx in (NEW_VERB_RE, NEW_IN_RE):
        for m in rx.finditer(goal):
            noun = m.group(1).lower()
            if noun not in NOT_NOUNS:
                found.setdefault(noun, m.start())
    if (m := NEW_DOC_RE.search(goal)) and not {"document", "file"} & set(found):
        found[m.group(0).split()[-1].lower()] = m.start()
    return [Fact("new", noun, pos) for noun, pos in found.items()]


def _parse_facts(goal: str, apps: list[str]) -> list[Fact]:
    facts: list[Fact] = []
    texts = [m.group(1).strip() for m in TEXT_RE.finditer(goal)]
    quoted = {t.lower() for t in texts}
    for t in texts:
        if t and Fact("text", t) not in facts:
            facts.append(Fact("text", t, _at(goal, t)))
    for m in SAVE_RE.finditer(goal):
        name = (m.group(1) or m.group(2)).strip()
        if Fact("saved", name) not in facts:
            facts.append(Fact("saved", name, _at(goal, name)))
    found: list[str] = []

    def add(candidate: str) -> bool:
        words = candidate.split()
        for n in range(len(words), 0, -1):  # longest prefix that names an installed app
            phrase = " ".join(words[:n])
            if phrase.lower() in quoted:
                return False
            app = resolve_app(phrase, apps)
            if app:
                if app not in found:
                    found.append(app)
                return True
        return False

    outside = re.sub(QUOTE, " ", goal)  # quoted text is content, not an app name
    for m in APP_AFTER_RE.finditer(outside):
        add(m.group(1))
    for m in CAPITALISED_RE.finditer(outside):
        first = m.group(1).split()[0]
        if m.start() == 0 and first.lower() in {"open", "launch", "type", "write", "enter", "in"}:
            continue  # sentence-initial verb
        run = m.group(1).split()
        for i in range(len(run)):  # "Check Photos": the app may start later in the capitalised run
            if add(" ".join(run[i:])):
                break
    facts += [Fact("app", a, _at(goal, a)) for a in found]
    sites = _site_facts(goal, apps, found)
    searches = _search_facts(goal)
    facts += sites + searches
    facts += _click_facts(goal, apps, facts)
    return facts


DOUBLE_QUOTED = re.compile(r"[\"“]([^\"”]+)[\"”]")


def quoted_strings(goal: str) -> list[str]:
    """Every double-quoted string in the goal, in order."""
    return [m.strip() for m in DOUBLE_QUOTED.findall(goal) if m.strip()]


MAX_READ = 1_000_000  # bytes of a saved file read to look for the typed text


EDITABLE_ROLES = {"AXTextArea", "AXTextField", "AXSearchField", "AXComboBox"}
LIST_PARTS = {"AXOutline", "AXTable", "AXList", "AXBrowser", "AXRow", "AXCell"}


def _editable_texts(snap: Snapshot) -> list:
    """Text fields and areas the user could type in: not the name cells of a file list or table row."""
    def listed(e) -> bool:
        return any(seg.split(":", 1)[0] in LIST_PARTS for seg in e.path.split(" > "))

    return [e for e in snap.elements if e.role in EDITABLE_ROLES and not listed(e)]


def resolve(facts: list[Fact], snap: Snapshot, since: float | None = None, known: dict | None = None) -> list[bool]:
    """Whether each fact holds. A fact the screen says nothing about (None: only a sheet is showing, no editable text)
    keeps the value it last had in this run (`known`, updated here), or False if it never had one. Only an explicit
    False flips something back to not done."""
    known = {} if known is None else known
    out = []
    for f in facts:
        v = _holds(f, snap, facts, since)
        if v is None:
            v = known.get(f, False)
        else:
            known[f] = v
        out.append(v)
    return out


def check_facts(facts: list[Fact], snap: Snapshot, since: float | None = None, known: dict | None = None) -> bool:
    return bool(facts) and all(resolve(facts, snap, since, known))


def _saved_problem(fact: Fact, snap: Snapshot, facts: list[Fact], since: float | None) -> str | None:
    """Why the goal's file isn't saved yet (None: it is). With the document's path the file itself is checked: its
    exact name, that it exists, that it was written during this run and that it holds the goal's text. Without a
    path (the app doesn't report one) only the window title can be compared."""
    name = fact.value
    if not snap.document_path:
        title, want = snap.window_title.strip().lower(), name.lower()
        return None if title in (want, os.path.splitext(want)[0]) else f"no window is titled {fact.label()}"
    base = os.path.basename(snap.document_path)
    if base.lower() != name.lower():
        return f'saved as "{base}"; the goal asks for {fact.label()}'
    try:
        st = os.stat(snap.document_path)
    except OSError:
        return f"{fact.label()} does not exist on disk"
    if since is not None and st.st_mtime < since:
        return f"{fact.label()} was not written during this run"
    wanted = [f.value.lower() for f in facts if f.kind == "text"]
    if wanted:
        with open(snap.document_path, "rb") as fh:
            content = fh.read(MAX_READ).decode("utf-8", errors="ignore").lower()
        for text in wanted:
            if text not in content:
                return f'{fact.label()} doesn\'t contain "{text}"'
    return None


def _on_site(value: str, url: str, alias: str = "") -> bool | None:
    """The page's host is the domain (or a subdomain of it), or a bare name is one of its labels (`youtube` is a label of
    www.youtube.com, not of notyoutube.com). Without a URL there is no evidence (a title naming the site is not enough)."""
    host = (urlparse(url).hostname or "").lower() if url else ""
    if not host:
        return None
    if alias and (host == alias or host.endswith("." + alias)):
        return True
    if "." in value:
        return host == value or host.endswith("." + value)
    labels = host.split(".")
    if " " in value:  # a name of several words ("uiuc portal"): each word, or all of them run together, is in the host
        words = value.split()
        return all(any(w in label for label in labels) for w in words) or "".join(words) in labels
    return value in labels


def _searched(query: str, url: str) -> bool | None:
    """True when the URL's query string carries the search; else no evidence (the loop remembers a submitted search
    on pages that keep the query out of the URL)."""
    if url and query.lower() in unquote_plus(urlparse(url).query).lower():
        return True
    return None


def search_field(snap: Snapshot):
    """A search box on screen: a search field, or a text field named or labelled with 'search'."""
    for e in snap.elements:
        if e.interactive and (e.role == "AXSearchField" or (
                e.role in ("AXTextField", "AXComboBox", "AXTextArea") and "search" in (e.name() + e.placeholder).lower())):
            return e
    return None


def checkable_facts(facts: list[Fact], snap: Snapshot) -> list[Fact]:
    """The facts that can be checked on `snap`: site and search need the page's URL, so without one a planner `done`
    is not rejected for them (it would be rejected forever)."""
    return [f for f in facts if snap.url or f.kind not in ("site", "search")]


def _holds(fact: Fact, snap: Snapshot, facts: list[Fact] | None = None, since: float | None = None) -> bool | None:
    """True / False, or None when the screen has no evidence either way (no editable text, no document window)."""
    if fact.kind == "app":
        return snap.app.lower() == fact.value.lower()
    if fact.kind == "saved":
        if not snap.window_title and not snap.document_path:
            return None
        return _saved_problem(fact, snap, facts or [fact], since) is None
    if fact.kind == "click":
        return None  # the loop remembers a verified click chosen for this object
    if fact.kind == "outline":
        return None  # judged by the loop (Jev over the screen, or a matching verified step), then remembered
    if fact.kind == "field":
        form = find_form(snap)
        match = form.field(fact.value) if form else None
        return None if match is None else not match.empty
    if fact.kind == "submit":
        if success_shown(snap):
            return True
        return find_form(snap) is None  # the form is gone (or it was never on this screen: no evidence, see resolve)
    if fact.kind == "site":
        return _on_site(fact.value, snap.url, fact.alias)
    if fact.kind == "search":
        return _searched(fact.value, snap.url)
    if fact.kind == "new":
        if fact.value not in DOC_NOUNS:
            return None  # nothing on screen says a new conversation is open; the loop remembers its New click
        areas = [e for e in snap.elements if e.role == "AXTextArea"]
        return snap.window_title.strip().lower().startswith("untitled") or (len(areas) == 1 and not areas[0].value)
    texts = _editable_texts(snap)
    if not texts:
        return None
    return any(fact.value.lower() in e.value.lower() for e in texts)


def _says(fact: Fact) -> str:
    """The fact as a state (for result reasons)."""
    if fact.kind == "app":
        return f"{fact.value} in front"
    if fact.kind == "new":
        return "a new empty document open" if fact.value in DOC_NOUNS else f"a new {fact.value} open"
    if fact.kind == "click":
        return f"{fact.label()} clicked"
    if fact.kind == "outline":
        return fact.value
    if fact.kind == "field":
        return f"{fact.label()} filled in"
    if fact.kind == "submit":
        return "the form submitted"
    if fact.kind == "site":
        return f"{fact.label()} site open"
    if fact.kind == "search":
        return f"a search for {fact.label()} done"
    return f"window titled {fact.label()}" if fact.kind == "saved" else f"{fact.label()} in text area"


def _action(fact: Fact, facts: list[Fact] | None = None, snap: Snapshot | None = None) -> str:
    """The fact as an instruction (for the planner's checklist). Site and search lines read the other facts and the screen."""
    if fact.kind == "click":
        return f"click {fact.value}"
    if fact.kind == "outline":
        return fact.value
    if fact.kind == "field":
        if fact.meta == "select":
            return f'choose "{fact.data}" in "{fact.value}"'
        if fact.meta == "checkbox":
            return f'tick "{fact.value}"'
        return f'type "{fact.data}" into "{fact.value}"'
    if fact.kind == "submit":
        return f'submit the form: click "{fact.data or fact.value}"'
    if fact.kind == "site":
        domain = site_url(fact.value).removeprefix("https://")
        bar = next((e for e in (snap.elements if snap is not None else []) if e.interactive
                    and e.role in ("AXTextField", "AXComboBox", "AXSearchField")
                    and re.search(r"address|url|location|search or enter", e.name() + " " + e.placeholder, re.I)), None)
        field = f'the "{bar.name()}" field' if bar is not None and bar.name() else "the address bar"
        if "." not in fact.value:  # a name, not an address: the browser's own search finds the site
            return (f'go to {fact.value}: search for "{fact.value}" in {field}, press return and open the official '
                    "site from the results")
        return f'go to {fact.value}: click {field}, type "{domain}" and press return'
    if fact.kind == "search":
        field = search_field(snap) if snap is not None else None
        if field is not None:
            return f'type {fact.label()} into the "{field.name() or field.placeholder or "search"}" field, then press return'
        return f"search for {fact.label()}"
    return {
        "app": f"open {fact.value}",
        "new": "make a new empty document" if fact.value in DOC_NOUNS else f"make a new {fact.value}",
        "text": f"type {fact.label()} into the document",
        "saved": f"save the document as {fact.label()}",
    }[fact.kind]


def describe_facts(facts: list[Fact]) -> str:
    return ", ".join(_says(f) for f in sorted(facts, key=lambda f: f.kind != "app"))  # apps first


FILLER_WORDS = {"open", "launch", "start", "the", "a", "an", "app", "application", "please", "up", "and", "then",
                "in", "into", "it", "this", "that", "also", "to", "with", "go", "goto", "visit", "navigate", "browse",
                "site", "website", "page", "on", "for"}  # fmt: skip
# A goal that still names one of these after its facts are removed asks for more than the facts check ("send it",
# "rename the file"). Matched as word prefixes, so "saved" and "sending" count too.
ACTION_VERBS = (
    "save", "send", "rename", "close", "print", "export", "delete", "remove", "move", "copy", "paste", "submit",
    "upload", "download", "search", "find", "sign", "reply", "forward", "attach", "share", "set", "change", "enable",
    "disable", "install", "uninstall", "open", "launch", "create", "add", "edit", "format", "select", "click",
    "press", "scroll", "play", "pause", "write", "type", "enter", "fill", "check", "uncheck", "toggle", "turn",
    "run", "start", "stop", "quit", "order", "buy", "pay", "book", "schedule", "post", "publish", "archive", "mark",
    "drag", "drop", "duplicate", "merge", "sort", "filter", "tag", "label", "pin", "mute", "call", "login", "log",
)  # fmt: skip


def decisive_facts(goal: str, apps: list[str] | None = None) -> list[Fact]:
    """parse_facts, but only when the facts cover the whole goal; [] otherwise, so Jev's goal check decides.

    Remove what the facts account for (quoted text, the save phrase, app names, and open/launch/type next to them)
    and look at what is left: any other action verb ("send it", "rename") means more is asked than the facts check,
    and with no text or saved fact an app being in front proves little, so any further content word ("ChatGPT") also
    makes it indecisive."""
    facts = parse_facts(goal, apps)
    if not facts:
        return []
    new = new_facts(goal)
    rest = SEARCH_RE.sub(" ", SAVE_RE.sub(" ", goal))
    for f in facts:
        if f.kind == "click":
            rest = re.sub(rf"\b(?:click(?:\s+on)?|press|tap|play|open)\s+(?:the\s+|a\s+|an\s+)?{re.escape(f.value)}", " ", rest, flags=re.I)
    for rx in (NEW_VERB_RE, NEW_IN_RE, NEW_DOC_RE):  # "open a new conversation": covered by the `new` fact
        rest = rx.sub(" ", rest)
    rest = re.sub(QUOTE, " ", rest).lower()
    for f in facts:
        if f.kind in ("app", "site"):
            rest = rest.replace(f.value.lower(), " ")
    words = re.findall(r"[a-z0-9]+", rest)
    has_app = any(f.kind == "app" for f in facts)
    has_text = any(f.kind == "text" for f in facts)
    satisfied = ({"open", "launch", "start"} if has_app else set()) | ({"type", "write", "enter"} if has_text else set())
    satisfied |= {"create", "make", "begin"} if new else set()
    satisfied |= {"click", "press", "tap", "play", "open"} if any(f.kind == "click" for f in facts) else set()
    has_site = any(f.kind == "site" for f in facts)
    satisfied |= {"open", "go", "goto", "visit", "navigate", "browse"} if has_site else set()
    satisfied |= {"search", "searching"} if any(f.kind == "search" for f in facts) else set()
    for w in words:
        if w not in satisfied and any(w.startswith(v) for v in ACTION_VERBS):
            return []
    if not any(f.kind in ("text", "saved") for f in facts):
        if [w for w in words if w not in FILLER_WORDS]:
            return []
        return facts + new  # an app plus "new <noun>": the app in front and the New click that opened it
    return facts


def failing_facts(facts: list[Fact], snap: Snapshot, since: float | None = None, known: dict | None = None) -> str:
    """Why `facts` don't hold on `snap`, for the note on a rejected `done`."""
    why = {"new": "no new {} is open", "app": "{} isn't in front", "text": "{} isn't in any text field",
           "site": "{} isn't the open page", "search": "the search for {} hasn't been done",
           "click": "{} hasn't been clicked", "outline": "{} isn't done", "field": "{} isn't filled in", "submit": "the form hasn't been submitted"}  # fmt: skip
    out = []
    for f, ok in zip(facts, resolve(facts, snap, since, known)):
        if ok:
            continue
        if f.kind == "saved":
            out.append(_saved_problem(f, snap, facts, since) or f"no window is titled {f.label()}")
        else:
            out.append(why[f.kind].format(f.value if f.kind in ("app", "new") else f.label()))
    return ", ".join(out)


def progress(facts: list[Fact], snap: Snapshot, since: float | None = None,
             known: dict | None = None) -> list[tuple[Fact, bool]]:  # fmt: skip
    """Each fact with whether it holds (see `resolve`), in goal order."""
    return list(zip(facts, resolve(facts, snap, since, known)))


STARTING_STATES = {"new"}  # progress-only facts: a state to start from, not one that must stay true


def _progress_steps(facts, snap, seen, since, known):
    seen = set() if seen is None else seen
    steps = progress(facts, snap, since, known)
    done = []
    for i, (f, ok) in enumerate(steps):
        if f.kind in STARTING_STATES:
            ok = ok or f in seen or any(later for _, later in steps[i + 1:])
            if ok:
                seen.add(f)
        done.append(ok)
    return steps, done


def pending_fact(facts: list[Fact], snap: Snapshot, seen: set | None = None, since: float | None = None,
                 known: dict | None = None) -> Fact | None:
    """The first checklist fact that isn't done yet."""
    steps, done = _progress_steps(facts, snap, seen, since, known)
    return next((f for (f, _), ok in zip(steps, done) if not ok), None)


def rest_of_goal(goal: str, facts: list[Fact]) -> str:
    """The goal minus the phrases its facts account for (app, site, search, new-thing, save and quoted text)."""
    rest = SEARCH_RE.sub(" ", SAVE_RE.sub(" ", goal))
    for rx in (NEW_VERB_RE, NEW_IN_RE, NEW_DOC_RE):
        rest = rx.sub(" ", rest)
    for f in facts:
        if f.kind in ("app", "site"):
            rest = re.sub(rf"\b(?:open|launch|go\s+to|visit|in|on)?\s*(?:the\s+)?{re.escape(f.value)}\b", " ", rest,
                          flags=re.I)
        elif f.kind == "text":
            rest = re.sub(rf"\b(?:type|write|enter)\b[^\"“‘']*?{QUOTE}", " ", rest, flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.;")
    while (trimmed := re.sub(r"^(?:and|then|in|on|the)\s+|\s+(?:and|then|in|on|the)$", "", rest, flags=re.I).strip(" ,.;")) != rest:
        rest = trimmed
    return rest if re.search(r"[A-Za-z]{2}", rest) else goal


def progress_scope(facts: list[Fact], snap: Snapshot, seen: set | None = None, since: float | None = None,
                   known: dict | None = None, rest: str | None = None) -> dict:  # fmt: skip
    """The checklist as the planners show it, worded as actions: what is done, the first thing not done, and what
    comes after.

    A starting-state fact counts as done when it holds now, held earlier in the run (`seen`, updated here), or any
    fact after it holds: the work has moved past it. Real facts are done only while they hold.

    `rest` is what the facts don't cover, when they can't end the run alone: it is the last item, so `next` is never
    empty while the run can't end by facts."""
    steps, done = _progress_steps(facts, snap, seen, since, known)
    todo = [_action(f, facts, snap) for (f, _), ok in zip(steps, done) if not ok]
    if rest:
        todo.append(f"the rest of the goal: {rest}")
    return {"done": [_action(f, facts, snap) for (f, _), ok in zip(steps, done) if ok],
            "next": todo[0] if todo else None, "remaining": todo[1:]}  # fmt: skip
