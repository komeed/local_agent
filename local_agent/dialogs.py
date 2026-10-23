"""Reading dialogs and sheets off the snapshot: an overwrite warning, and a name-the-file sheet with its text field and
default button. All of it comes from the screen's structure, nothing app-specific."""

from __future__ import annotations

import re
from dataclasses import dataclass

from local_agent.observe.models import SINGLE_LINE_ROLES, Snapshot, UIElement

DIALOG_ROLES = {"AXSheet", "AXDialog", "AXSystemDialog"}
OVERWRITE_TEXT = re.compile(r"already exists|replace|overwrite", re.I)
OVERWRITE_BUTTONS = {"replace", "overwrite"}
FALLBACK_DEFAULTS = {"save", "ok"}


def in_dialog(e: UIElement) -> bool:
    return any(seg.split(":", 1)[0] in DIALOG_ROLES for seg in e.path.split(" > "))


def dialog_elements(snap: Snapshot) -> list[UIElement]:
    return [e for e in snap.elements if in_dialog(e) or e.role in DIALOG_ROLES]


def _texts(e: UIElement) -> str:
    return e.value or e.title or e.label


def overwrite_alert(snap: Snapshot) -> bool:
    """A sheet/dialog whose text says something exists / will be replaced, with a Replace/Overwrite button."""
    els = dialog_elements(snap)
    warns = any(OVERWRITE_TEXT.search(_texts(e)) for e in els if not e.interactive)
    return warns and any(e.role == "AXButton" and e.name().strip().lower() in OVERWRITE_BUTTONS for e in els)


def is_overwrite_button(e: UIElement | None) -> bool:
    return e is not None and e.role == "AXButton" and e.name().strip().lower() in OVERWRITE_BUTTONS


@dataclass
class NameSheet:
    field: UIElement
    label: str  # what the field is called on screen
    button: str  # the dialog's default button


def _field_label(e: UIElement) -> str:
    quoted = re.findall(r'"([^"]*)"', e.context)
    return (e.title or e.label or e.placeholder or (quoted[0] if quoted else "") or "text").strip().rstrip(":").strip()


def name_sheet(snap: Snapshot) -> NameSheet | None:
    """A dialog/sheet with a focused (or the only) single-line text field, and its default button: AXDefaultButton, else
    a button named Save or OK."""
    els = dialog_elements(snap)
    fields = [e for e in els if e.role in SINGLE_LINE_ROLES and e.interactive]
    field = next((e for e in fields if e.focused), fields[0] if len(fields) == 1 else None)
    if field is None:
        return None
    buttons = [e for e in els if e.role == "AXButton" and e.interactive]
    button = next((b for b in buttons if b.is_default), None) or next(
        (b for b in buttons if b.name().strip().lower() in FALLBACK_DEFAULTS), None)
    return NameSheet(field, _field_label(field), button.name() if button else "")


def alert_file(snap: Snapshot) -> str:
    """The file name an overwrite alert quotes (“x.txt” already exists...), else ""."""
    for e in dialog_elements(snap):
        if not e.interactive and OVERWRITE_TEXT.search(_texts(e)):
            if m := re.search(r"[“\"]([^”\"]+)[”\"]", _texts(e)):
                return m.group(1)
    return ""


def alert_buttons(snap: Snapshot) -> tuple[str, str]:
    """(the Replace/Overwrite button's name, the alert's cancel button's name or "")."""
    buttons = [e for e in dialog_elements(snap) if e.role == "AXButton" and e.interactive]
    replace = next((b.name() for b in buttons if is_overwrite_button(b)), "Replace")
    cancel = next((b.name() for b in buttons if b.name().strip().lower() == "cancel"), "")
    return replace, cancel


# --- leave / discard dialogs: "Changes you made may not be saved", Don't Save in a close sheet --------------------------------
LEAVE_TEXT = re.compile(r"(?:may|might|will|would)\s+not\s+be\s+saved|won.t be saved|unsaved|leave (?:this )?(?:site|page)|discard|lose (?:your )?changes", re.I)
LEAVE_BUTTONS = {"leave", "leave page", "leave site", "discard", "ok", "reload", "reload site", "don't save"}
DONT_SAVE = "don't save"  # discards data in any dialog, whatever its text says
STAY_BUTTONS = {"cancel", "stay", "stay on page", "stay on this page"}


def _bname(e: UIElement) -> str:
    return e.name().strip().lower().replace("’", "'")


def leave_text(snap: Snapshot) -> str:
    """The text of a dialog saying that leaving loses changes ("" when there is none)."""
    return next((_texts(e) for e in dialog_elements(snap) if not e.interactive and LEAVE_TEXT.search(_texts(e))), "")


def leave_dialog(snap: Snapshot) -> bool:
    """A dialog whose text says leaving loses changes, with a button that does it (Leave, Discard, OK, Reload...)."""
    buttons = [e for e in dialog_elements(snap) if e.role == "AXButton" and e.interactive]
    return bool(leave_text(snap)) and any(_bname(b) in LEAVE_BUTTONS for b in buttons)


def is_discard_button(snap: Snapshot, e: UIElement | None) -> bool:
    """Pressing `e` throws away unsaved work: Don't Save in any dialog, or Leave / Discard / OK / Reload in a leave dialog."""
    if e is None or e.role != "AXButton" or not in_dialog(e):
        return False
    name = _bname(e)
    return name == DONT_SAVE or (name in LEAVE_BUTTONS and leave_dialog(snap))


def leave_buttons(snap: Snapshot) -> tuple[UIElement | None, UIElement | None]:
    """(the button that leaves and loses the changes, the one that stays)."""
    buttons = [e for e in dialog_elements(snap) if e.role == "AXButton" and e.interactive]
    leave = next((b for b in buttons if _bname(b) in LEAVE_BUTTONS and _bname(b) != "ok"), None) or next(
        (b for b in buttons if _bname(b) == "ok"), None)
    stay = next((b for b in buttons if _bname(b) in STAY_BUTTONS), None)
    return leave, stay


def leave_line(snap: Snapshot) -> str:
    """The checklist line for a leave dialog the planner has to answer."""
    _, stay = leave_buttons(snap)
    text = leave_text(snap).replace('"', "'")[:80]
    return f'answer the dialog: "{text}": "{stay.name() if stay else "Cancel"}" keeps your changes'
