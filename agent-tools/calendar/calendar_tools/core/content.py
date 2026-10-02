"""Event content (spec 7.3, 5.3 `notes`, 9.3; plan UC04b).

The title and description come from the binding's templates, which may use
only the documented placeholders (checked at load, UC02a). Values are inserted
as plain text (`str.format_map` never interprets a value as a template), with
control characters, format characters and line breaks removed from every
inserted value. The title is one line, capped at 200 characters. The
`{notes_block}` is empty unless the binding accepts notes and notes are
present; notes are capped at 500 characters and written only here, never
logged. Nothing in this module knows what any binding's notes contain: their
wording is the caller's and the agent's, not the service's.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from calendar_tools.core.bindings import AppointmentType, Binding
from calendar_tools.core.contact import Contact

__all__ = ["MAX_NOTES_CHARS", "MAX_TITLE_CHARS", "NOTES_PREFIX", "clean_text", "render_event"]

MAX_TITLE_CHARS = 200
MAX_NOTES_CHARS = 500
NOTES_PREFIX = "Notes: "

# The same character classes the contact name drops (UC04a): controls, format
# characters (bidi overrides), unassigned, private use, surrogates and the
# line/paragraph separators. Line breaks and tabs become a space first.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"})
_AS_SPACE = frozenset("\t\n\v\f\r\x1c\x1d\x1e\x1f\x85  ")
_SPACE_RUN = re.compile(r"[ ]{2,}")


def clean_text(value: Any) -> str:
    """One line of plain text: line breaks and tabs become spaces, other
    control and format characters are removed, space runs collapse, and the
    ends are trimmed. A non-string is empty."""
    if not isinstance(value, str):
        return ""
    spaced = "".join(" " if ch in _AS_SPACE else ch for ch in value)
    kept = "".join(ch for ch in spaced if unicodedata.category(ch) not in _STRIP_CATEGORIES)
    return _SPACE_RUN.sub(" ", kept).strip()


def _notes_block(binding: Binding, notes: Any) -> str:
    if not binding.accept_notes:
        return ""
    text = clean_text(notes)[:MAX_NOTES_CHARS].rstrip()
    return f"{NOTES_PREFIX}{text}\n" if text else ""


def render_event(
    binding: Binding,
    contact: Contact,
    appointment_type: AppointmentType,
    duration: int,
    booking_ref: str,
    notes: Any,
) -> tuple[str, str]:
    """`(title, description)` for a new event (spec 7.3)."""
    values = {
        "contact_name": clean_text(contact.name),
        "contact_phone": clean_text(contact.phone),  # E.164, unmasked: the host calls back
        "contact_email": clean_text(contact.email),
        "appointment_type_label": clean_text(appointment_type.label),
        "duration_minutes": str(int(duration)),
        "booking_ref": clean_text(booking_ref),
        "notes_block": _notes_block(binding, notes),
    }
    title = clean_text(binding.event_title_template.format_map(values))[:MAX_TITLE_CHARS]
    description = binding.event_description_template.format_map(values)
    return title, description
