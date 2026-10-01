"""Message normalization (K11) and the positive template validator (K12, D-064).

The service authors no content (K1): it only normalizes, checks and forwards the text.
The validator accepts only ``<label>: <value>`` lines whose labels come from the
``SMS_ALLOWED_LABELS`` setting and whose values use a small character allowlist, so no
link, web address, email address, handle or IP address can be written (D-064).
The normalization and validator characters are written as ``chr()`` code points (no
invisible or bidi character appears in this source); the GSM-7 tables are printable literals.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

# --- reasons (spec section 4; the only detail a rejection ever carries) --------------------

BAD_REQUEST = "bad_request"
EMPTY = "empty"
TOO_MANY_LINES = "too_many_lines"
TOO_LONG = "too_long"
BAD_LINE = "bad_line"
UNKNOWN_LABEL = "unknown_label"
DUPLICATE_LABEL = "duplicate_label"
BAD_CHARACTER = "bad_character"
REASONS = (
    BAD_REQUEST,
    EMPTY,
    TOO_MANY_LINES,
    TOO_LONG,
    BAD_LINE,
    UNKNOWN_LABEL,
    DUPLICATE_LABEL,
    BAD_CHARACTER,
)

MAX_LABEL_CHARS = 32


class MessageRejected(Exception):
    """The message can't be sent: ``invalid_request`` with a fixed ``reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = "invalid_request"
        self.reason = reason


class TemplateConfigInvalid(Exception):
    """``SMS_PREFIX`` or ``SMS_ALLOWED_LABELS`` breaks K12 (a configuration error: ``unavailable``)."""


# --- K11 normalization --------------------------------------------------------------------

# Bidi controls and zero-width characters (every Cf character is stripped as well).
_INVISIBLE = frozenset(
    chr(c)
    for c in (
        0x061C, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF,
    )
)  # fmt: skip

_ASCII_MAP = str.maketrans(
    {
        **{chr(c): "'" for c in (0x2018, 0x2019, 0x201A, 0x201B, 0x2032, 0x60)},
        **{chr(c): '"' for c in (0x201C, 0x201D, 0x201E, 0x201F, 0x2033)},
        **{chr(c): "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)},
        chr(0x2026): "...",
    }
)

# Ideographic and full-width full stops render as a dot; map them so the period rule sees one.
_DOT_MAP = str.maketrans({chr(0x3002): ".", chr(0xFF0E): ".", chr(0xFF61): "."})
_LINE_SEPARATORS = str.maketrans({chr(0x2028): "\n", chr(0x2029): "\n"})

_WHITESPACE_RUN = re.compile(r"\s+")


def _is_control(ch: str) -> bool:
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def normalize(text: str) -> str:
    """NFKC; CRLF/CR and U+2028/U+2029 to LF; full-stop variants to '.'; strip controls
    (except LF), bidi and every Cf character; collapse whitespace within each line;
    drop blank lines; map punctuation to ASCII."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_LINE_SEPARATORS).translate(_DOT_MAP)
    text = text.replace("\t", " ")
    text = "".join(
        ch
        for ch in text
        if ch == "\n"
        or not (_is_control(ch) or ch in _INVISIBLE or unicodedata.category(ch) == "Cf")
    )
    lines = []
    for line in text.split("\n"):
        collapsed = _WHITESPACE_RUN.sub(" ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines).translate(_ASCII_MAP)


# --- K12 template validator ---------------------------------------------------------------

_VALUE_PUNCTUATION = frozenset("$#,+-()'&% ")
_LATIN1_EXCLUDED = (chr(0xD7), chr(0xF7))  # multiplication and division signs
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DIGIT_RUN = re.compile(r"[0-9.]+")


def _fold(text: str) -> str:
    """Case-insensitive comparison on ASCII letters only (K12 rule 3)."""
    return text.translate(_ASCII_FOLD)


def _is_ascii_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _is_value_char(ch: str) -> bool:
    """K12 rule 4, without the period (rule 5 handles it)."""
    if "A" <= ch <= "Z" or "a" <= ch <= "z" or _is_ascii_digit(ch):
        return True
    if chr(0xC0) <= ch <= chr(0xFF) and ch not in _LATIN1_EXCLUDED:
        return True
    return ch in _VALUE_PUNCTUATION


def _value_ok(value: str) -> bool:
    """K12 rules 4 to 6: the charset, the period rule and the ``www`` rule."""
    if "www" in _fold(value):
        return False
    for i, ch in enumerate(value):
        if ch == ".":
            before = value[i - 1] if i > 0 else ""
            after = value[i + 1] if i + 1 < len(value) else ""
            if not (_is_ascii_digit(before) and _is_ascii_digit(after)):
                return False
        elif not _is_value_char(ch):
            return False
    # Any run of digits and periods may hold at most one period (no IP addresses).
    return all(run.count(".") <= 1 for run in _DIGIT_RUN.findall(value))


def validate_prefix(prefix: str) -> None:
    """K12 rule 7: a single line obeying rules 4 to 6 (empty is allowed)."""
    if "\n" in prefix or "\r" in prefix or (prefix and not _value_ok(prefix)):
        raise TemplateConfigInvalid("SMS_PREFIX breaks the template rules")


def parse_labels(raw: str, max_lines: int) -> dict[str, str]:
    """K12 rule 1: the allowed labels, keyed by their case-folded form. Fails closed."""
    if not raw or not raw.strip():
        raise TemplateConfigInvalid("SMS_ALLOWED_LABELS is empty")
    entries = [entry.strip() for entry in raw.split(",")]
    if len(entries) > max_lines:
        raise TemplateConfigInvalid("SMS_ALLOWED_LABELS has too many entries")
    labels: dict[str, str] = {}
    for entry in entries:
        if (
            not 1 <= len(entry) <= MAX_LABEL_CHARS
            or "." in entry
            or "  " in entry
            or not all(_is_value_char(ch) and ch != "," for ch in entry)
        ):
            raise TemplateConfigInvalid("SMS_ALLOWED_LABELS has an invalid entry")
        key = _fold(entry)
        if key in labels:
            raise TemplateConfigInvalid("SMS_ALLOWED_LABELS has a duplicate entry")
        labels[key] = entry
    return labels


def _check_lines(text: str, labels: dict[str, str]) -> None:
    """K12 rules 2 to 6, line by line from the top; the first failure wins."""
    seen: set[str] = set()
    for line in text.split("\n"):
        label, colon, rest = line.partition(":")
        if not colon or not label or len(rest) < 2 or rest[0] != " " or rest[1] == " ":
            raise MessageRejected(BAD_LINE)
        key = _fold(label)
        if key not in labels:
            raise MessageRejected(UNKNOWN_LABEL)
        if key in seen:
            raise MessageRejected(DUPLICATE_LABEL)
        seen.add(key)
        if not _value_ok(rest[1:]):
            raise MessageRejected(BAD_CHARACTER)


def build_body(raw: str, *, prefix: str, labels_raw: str, max_chars: int, max_lines: int) -> str:
    """Return the body as sent (``prefix + normalized text``) or raise.

    Order (spec K12 "Order of checks"): normalize; the config checks on the prefix and
    the labels (``TemplateConfigInvalid``); ``empty``; ``too_many_lines``; ``too_long``;
    then each line: ``bad_line``, ``unknown_label``, ``duplicate_label``, ``bad_character``.
    """
    text = normalize(raw)
    validate_prefix(prefix)
    labels = parse_labels(labels_raw, max_lines)
    if not text:
        raise MessageRejected(EMPTY)
    if text.count("\n") + 1 > max_lines:
        raise MessageRejected(TOO_MANY_LINES)
    body = prefix + text
    if len(body) > max_chars:
        raise MessageRejected(TOO_LONG)
    _check_lines(text, labels)
    return body


# --- segment counting --------------------------------------------------------------------

_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENSION = set("^{}\\[~]|€\f")


@dataclass(frozen=True)
class SegmentInfo:
    encoding: str  # "GSM-7" or "UCS-2"
    units: int  # septets (GSM-7) or UTF-16 code units (UCS-2)
    segments: int


def segment_info(body: str) -> SegmentInfo:
    if all(ch in _GSM7_BASIC or ch in _GSM7_EXTENSION for ch in body):
        units = sum(2 if ch in _GSM7_EXTENSION else 1 for ch in body)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        units = len(body.encode("utf-16-le")) // 2
        single, multi, encoding = 70, 67, "UCS-2"
    segments = 1 if units <= single else math.ceil(units / multi)
    return SegmentInfo(encoding=encoding, units=units, segments=segments)
