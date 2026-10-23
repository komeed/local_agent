"""A form on the screen as a checklist (pure Python over snapshot elements).

`find_form` picks the front-most dialog/sheet form or else the biggest one; `form_fields` lists its editable fields in
reading order with label, required, current value and whether the field is one the agent must never fill (passwords,
card numbers, SSNs and other government IDs). `match_values` pairs the goal's labelled quotes and a --data file with the
fields; nothing is invented: a required field without a value ends the run as `needs_input`."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from local_agent.observe.models import Snapshot, UIElement

FILL_RE = re.compile(
    r"\b(?:fill|complete|finish)\b.{0,40}?\b(?:form|document|application|sign-?\s?up|registration|survey|questionnaire)\b"
    r"|\bsign\s?up\b|\bregister\b", re.I)  # fmt: skip
FIELD_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXPopUpButton", "AXCheckBox", "AXSecureTextField"}
CONTAINER_ROLES = {"AXSheet", "AXDialog"}
SENSITIVE = re.compile(
    r"cc-|card\s*(?:number|no)|credit|debit|cvv|cvc|csc|security code|\bssn\b|social security|passport|national id|"
    r"tax id|driver.?s? licen[cs]e|\biban\b|routing|account number|pin code", re.I)  # fmt: skip
SUBMIT_NAME = re.compile(r"^(?:submit|sign up|register|create|send|save|continue|next|apply|join|finish|done|ok|get started)\b", re.I)
SUCCESS_TEXT = re.compile(r"thank you|thanks for|success|submitted|confirmation|account (?:has been )?created|welcome", re.I)
PLACEHOLDER_OPTION = re.compile(r"^\s*(?:choose|select|please|pick|--|—|-+\s*$)", re.I)
CONNECTORS = {"fill", "out", "in", "complete", "the", "form", "with", "using", "and", "my", "is", "as", "to", "a", "an",
              "sign", "up", "application", "document", "please", "enter", "use", "set", "put", "then", "of", "for"}  # fmt: skip
NEEDS_INPUT = "needs_input"
SENSITIVE_NOTE = "typing card numbers, SSNs, government IDs or passwords is not allowed; the user must do this"


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass
class FormField:
    element: UIElement
    label: str
    kind: str  # text | select | checkbox | password
    required: bool
    value: str
    sensitive: str  # "" or why the agent must not fill it

    @property
    def empty(self) -> bool:
        e = self.element
        v = (self.value or "").strip()
        if self.kind == "checkbox":
            return v.lower() not in ("1", "true", "on", "yes")
        if self.kind == "select":
            return not v or bool(PLACEHOLDER_OPTION.match(v))
        return not v or v == (e.placeholder or "").strip()


@dataclass
class Form:
    title: str
    fields: list[FormField]
    submit: UIElement | None
    key: str = ""

    def field(self, label: str) -> FormField | None:
        return next((f for f in self.fields if norm(f.label) == norm(label)), None)


def container_key(e: UIElement) -> str:
    segs = e.path.split(" > ")
    for roles in ({"AXForm"}, CONTAINER_ROLES):
        idx = [i for i, s in enumerate(segs) if s.split(":", 1)[0] in roles]
        if idx:
            return " > ".join(segs[: idx[-1] + 1])
    return e.path


def _label(e: UIElement) -> tuple[str, bool]:
    raw = e.title or e.label or ""
    if not raw:
        m = re.search(r'label "([^"]*)"', e.context)
        raw = m.group(1) if m else (e.placeholder or e.help or "")
    star = "*" in raw
    return re.sub(r"\s*[*:]+\s*$", "", raw.replace("*", "")).strip(), star


def _kind(e: UIElement) -> str:
    if e.role == "AXSecureTextField":
        return "password"
    if e.role == "AXPopUpButton":
        return "select"
    return "checkbox" if e.role == "AXCheckBox" else "text"


def sensitive_reason(e: UIElement) -> str:
    """Why the agent must not type into this field ("" when it may)."""
    if e.role == "AXSecureTextField":
        return "password field"
    text = " ".join([e.title, e.label, e.placeholder, e.help, e.hint, e.context])
    return "card, SSN or government ID field" if SENSITIVE.search(text) else ""


def _field(e: UIElement) -> FormField:
    label, star = _label(e)
    return FormField(e, label or e.role.removeprefix("AX"), _kind(e), e.required or star, e.value or "", sensitive_reason(e))


def _order(e: UIElement):
    return (round(e.frame.y / 8) if e.frame else 1e9, e.frame.x if e.frame else 0, e.id)


def find_form(snap: Snapshot) -> Form | None:
    """The front-most form: a dialog/sheet with ≥2 editable fields, else the container with the most; None if none has two."""
    groups: dict[str, list[UIElement]] = {}
    for e in snap.elements:
        if e.interactive and e.enabled and e.role in FIELD_ROLES:
            groups.setdefault(container_key(e), []).append(e)
    forms = {k: v for k, v in groups.items() if len(v) >= 2}
    if not forms:
        return None

    def rank(item):
        key, els = item
        in_dialog = any(s.split(":", 1)[0] in CONTAINER_ROLES for s in key.split(" > "))
        return (not in_dialog, -len(els), _order(min(els, key=_order)))

    key, els = min(forms.items(), key=rank)
    fields = [_field(e) for e in sorted(els, key=_order)]
    buttons = [e for e in snap.elements if e.role == "AXButton" and e.interactive and e.enabled and e.name()
               and (container_key(e) == key or e.path.startswith(key))]  # fmt: skip
    submit = (next((b for b in buttons if b.is_default), None)
              or next((b for b in buttons if SUBMIT_NAME.match(b.name())), None)
              or (sorted(buttons, key=_order)[-1] if buttons else None))
    last = key.split(" > ")[-1]
    title = last.split(":", 1)[1] if ":" in last and last.split(":", 1)[1] else (snap.page_title or snap.window_title)
    return Form(title or "form", fields, submit, key)


def wants_form(goal: str) -> bool:
    return bool(FILL_RE.search(goal))


# --- values ------------------------------------------------------------------------------------------------------------


def labelled_quotes(goal: str) -> list[tuple[str, str]]:
    """(label words, value) for each double-quoted string that follows a label: `name "Ada", email "a@b.c"`."""
    out, pos = [], 0
    for m in re.finditer(r'[“"]([^”"]+)[”"]', goal):
        words = [w for w in re.findall(r"[A-Za-z][\w-]*", goal[pos : m.start()]) if w.lower() not in CONNECTORS]
        pos = m.end()
        if words:
            out.append((" ".join(words[-2:]), m.group(1)))
    return out


def parse_data(text: str) -> dict[str, str]:
    """A --data file: a JSON object, or `key: value` lines."""
    text = text.strip()
    if text.startswith("{"):
        return {str(k): str(v) for k, v in json.loads(text).items()}
    out = {}
    for line in text.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def match_values(fields: list[FormField], goal: str, data: dict[str, str] | None, pick=None) -> dict[str, str]:
    """label -> value for the fields the goal's labelled quotes and `data` name. Exact normalised label first, then a
    containing label, then `pick(key, labels)` (a Jev choice) when given. A field gets one value; none is invented."""
    pairs = labelled_quotes(goal) + list((data or {}).items())
    values: dict[str, str] = {}
    free = [f for f in fields if not f.sensitive]
    for key, value in pairs:
        k = norm(key)
        match = next((f for f in free if norm(f.label) == k and f.label not in values), None)
        if match is None and len(k) >= 3:
            partial = [f for f in free if f.label not in values and (k in norm(f.label) or (len(norm(f.label)) >= 3 and norm(f.label) in k))]
            match = partial[0] if len(partial) == 1 else None
        if match is None and pick is not None:
            labels = [f.label for f in free if f.label not in values]
            chosen = pick(f"{key}: {value}", labels) if labels else None
            match = next((f for f in free if f.label == chosen), None)
        if match is not None:
            values[match.label] = value
    return values


def missing_required(form: Form, values: dict[str, str]) -> list[str]:
    """Labels of required fields that are empty and have no value (sensitive ones can never have one)."""
    out = []
    for f in form.fields:
        if not f.required or not f.empty:
            continue
        if f.sensitive:
            out.append(f"{f.label} (the user must enter it)")
        elif f.label not in values:
            out.append(f.label)
    return out


def success_shown(snap: Snapshot) -> bool:
    return any(not e.interactive and SUCCESS_TEXT.search(e.value) for e in snap.elements)


@dataclass
class FormPlan:
    form: Form
    values: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
