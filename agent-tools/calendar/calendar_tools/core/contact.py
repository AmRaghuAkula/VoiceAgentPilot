"""Contact normalization (spec 5.3 contact rows, 7.4 step 1, 9.3; plan UC04a, P15).

`normalize` turns the request's `contact` object into a `Contact` or an
`InvalidFields` naming the bad fields (`contact.name`, `contact.phone`,
`contact.email`, or `contact` itself). It enforces only what step 1 needs: each
present field is well formed, and an identity (a phone or an email) exists,
since no contact tag or fingerprint can be computed without one. The binding's
`required_contact_fields` depend on current config and stay a step-4 check.

Nothing here logs, and neither `Contact` nor `InvalidFields` can print a value.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import phonenumbers

from calendar_tools.core.bindings import Binding

__all__ = ["MAX_NAME_CHARS", "Contact", "InvalidFields", "normalize"]

MAX_NAME_CHARS = 80
MAX_NAME_INPUT = 1024  # raw characters scanned at most
MAX_PHONE_INPUT = 40
MAX_EMAIL_CHARS = 254

# Characters stripped from a name: every Unicode "other" category (controls,
# format characters such as bidi overrides, unassigned, private use,
# surrogates) and the line/paragraph separators.
# Line breaks, tabs and separators become a space first, so a name split over
# two lines keeps its word break; runs of whitespace then collapse to one space.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"})
_AS_SPACE = frozenset("\t\n\v\f\r\x1c\x1d\x1e\x1f\x85\u2028\u2029")
_WHITESPACE_RUN = re.compile(r"\s+")
# Digits, spaces and the usual phone punctuation only: letters are refused, so a
# vanity number is never silently converted into digits.
_PHONE_CHARS = re.compile(r"[0-9+()./\- ]+")
# Basic syntax only (spec 5.3): one "@", no whitespace or controls, a dotted domain
# with no empty labels.
_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")


@dataclass(frozen=True)
class Contact:
    name: str | None = field(repr=False)
    phone: str | None = field(repr=False)  # E.164
    email: str | None = field(repr=False)  # as given, trimmed

    @property
    def identity(self) -> str | None:
        """What the contact tag is computed over: the phone, else the lowercased email."""
        if self.phone is not None:
            return self.phone
        if self.email is not None:
            return self.email.lower()
        return None

    def __repr__(self) -> str:
        present = [n for n in ("name", "phone", "email") if getattr(self, n) is not None]
        return f"Contact(<redacted: {', '.join(present) or 'empty'}>)"

    __str__ = __repr__


@dataclass(frozen=True)
class InvalidFields:
    fields: tuple[str, ...]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > MAX_NAME_INPUT:
        # Bounded before the per-character scan (UC04a cso r2); a name this
        # long could never be 80 characters after cleaning anyway.
        return None
    spaced = "".join(" " if ch in _AS_SPACE else ch for ch in value)
    kept = "".join(ch for ch in spaced if unicodedata.category(ch) not in _STRIP_CATEGORIES)
    stripped = _WHITESPACE_RUN.sub(" ", kept).strip()
    if not stripped or len(stripped) > MAX_NAME_CHARS:
        return None
    if not any(_is_visible_letter_or_digit(ch) for ch in stripped):
        # A visibly blank name (fillers, braille blank, punctuation only) would
        # put an empty-looking name into the host's event; refuse it.
        return None
    return stripped


# Letters that render as blank space (Hangul fillers) and the braille blank.
_BLANK_LOOKING = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800)))


def _is_visible_letter_or_digit(ch: str) -> bool:
    return unicodedata.category(ch)[0] in ("L", "N") and ch not in _BLANK_LOOKING


def _clean_phone(value: Any, region: str) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > MAX_PHONE_INPUT or not _PHONE_CHARS.fullmatch(text):
        return None
    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    # P15: is_possible_number, not is_valid_number, whose metadata may reject the
    # fictional 555-01xx lines used in tests.
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _clean_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > MAX_EMAIL_CHARS or not text.isprintable() or not _EMAIL.fullmatch(text):
        return None
    return text


def normalize(binding: Binding, raw: Any) -> Contact | InvalidFields:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return InvalidFields(("contact",))

    bad: list[str] = []

    name: str | None = None
    if raw.get("name") is not None:
        name = _clean_name(raw.get("name"))
        if name is None:
            bad.append("contact.name")

    phone: str | None = None
    raw_phone = raw.get("phone")
    phone_present = not _blank(raw_phone)
    if phone_present:
        phone = _clean_phone(raw_phone, binding.default_phone_region)
        if phone is None:
            bad.append("contact.phone")

    email: str | None = None
    raw_email = raw.get("email")
    email_present = not _blank(raw_email)
    if email_present:
        email = _clean_email(raw_email)
        if email is None:
            bad.append("contact.email")

    if not phone_present and not email_present:
        # No identity at all: no contact tag or fingerprint can exist (plan UC04a).
        bad.extend(["contact.phone", "contact.email"])

    if bad:
        return InvalidFields(tuple(bad))
    return Contact(name=name, phone=phone, email=email)
