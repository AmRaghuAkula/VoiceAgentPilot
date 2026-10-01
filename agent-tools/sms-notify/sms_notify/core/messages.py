"""Message normalization and validation (spec K11, section 5 step 2).

The service authors no content (K1): it only normalizes, checks and forwards the text.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass


class MessageRejected(Exception):
    """The message can't be sent. ``code`` is ``invalid_request`` or ``content_rejected``."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


class PrefixInvalid(Exception):
    """``SMS_PREFIX`` is not a single line (a configuration error: ``unavailable``)."""


# Bidi controls and zero-width characters (the latter could hide a link from the check).
_INVISIBLE = {
    "؜",
    "​",
    "‌",
    "‍",
    "‎",
    "‏",
    "‪",
    "‫",
    "‬",
    "‭",
    "‮",
    "⁠",
    "⁦",
    "⁧",
    "⁨",
    "⁩",
    "﻿",
}

_ASCII_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "″": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "…": "...",
    }
)

# Ideographic and full-width full stops render as a dot; map them so the link check sees one.
_DOT_MAP = str.maketrans({"。": ".", "．": ".", "｡": "."})

_WHITESPACE_RUN = re.compile(r"\s+")

_LINK_TLDS = ("com", "net", "org", "ca", "io", "ly", "co", "me", "info", "biz", "app", "link", "xyz", "us")
_TLD_LINK = re.compile(
    r"(?<![a-z0-9])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:" + "|".join(_LINK_TLDS) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def _is_control(ch: str) -> bool:
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def normalize(text: str) -> str:
    """NFKC; CRLF/CR to LF; strip controls (except LF), bidi and zero-width characters;
    collapse whitespace within each line; drop blank lines; map punctuation to ASCII."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n")
    text = text.replace("\t", " ")
    text = text.translate(_DOT_MAP)
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


def contains_link(text: str) -> bool:
    lowered = text.lower()
    if "://" in lowered or "www." in lowered:
        return True
    return _TLD_LINK.search(lowered) is not None


def build_body(raw: str, *, prefix: str, max_chars: int, max_lines: int) -> str:
    """Return the body as sent (``prefix + normalized text``) or raise.

    Order (spec section 5 step 2): normalize, validate the prefix, check the limits, reject links.
    """
    text = normalize(raw)
    if not text:
        raise MessageRejected("content_rejected", "empty after normalization")
    if "\n" in prefix or "\r" in prefix:
        raise PrefixInvalid("SMS_PREFIX must be a single line")
    body = prefix + text
    if len(body) > max_chars:
        raise MessageRejected("invalid_request", "too long")
    if body.count("\n") + 1 > max_lines:
        raise MessageRejected("invalid_request", "too many lines")
    if contains_link(body):
        raise MessageRejected("content_rejected", "contains a link")
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
