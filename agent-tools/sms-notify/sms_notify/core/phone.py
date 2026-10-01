"""E.164 checks, country checks and masking (D-006/D-021: ``***1234`` in every log)."""

from __future__ import annotations

import re

_E164 = re.compile(r"\+[1-9]\d{7,14}")
_NANP = re.compile(r"\+1([2-9]\d{2})[2-9]\d{6}")

# Canadian geographic NANP area codes (CNAC). Recipients are in Canada only (Q-086).
CANADIAN_AREA_CODES = frozenset(
    {
        # Alberta
        "368", "403", "587", "780", "825",
        # British Columbia
        "236", "250", "257", "604", "672", "778",
        # Manitoba
        "204", "431", "584",
        # New Brunswick
        "428", "506",
        # Newfoundland and Labrador
        "709", "879",
        # Nova Scotia and Prince Edward Island
        "782", "902",
        # Ontario
        "226", "249", "289", "343", "365", "382", "416", "437", "519", "548",
        "613", "647", "683", "705", "742", "753", "807", "905", "942",
        # Quebec
        "263", "354", "367", "418", "438", "450", "468", "514", "579", "581", "819", "873",
        # Saskatchewan
        "306", "474", "639",
        # Yukon, Northwest Territories and Nunavut
        "867",
    }
)  # fmt: skip

SUPPORTED_COUNTRIES = frozenset({"CA"})


def is_e164(number: object) -> bool:
    return isinstance(number, str) and _E164.fullmatch(number) is not None


def country_of(number: str) -> str | None:
    """The ISO country for a supported country, or None."""
    match = _NANP.fullmatch(number)
    if match and match.group(1) in CANADIAN_AREA_CODES:
        return "CA"
    return None


def mask(number: object) -> str:
    """``***`` plus the last four digits; never the full number."""
    digits = re.sub(r"\D", "", number) if isinstance(number, str) else ""
    return "***" + digits[-4:] if len(digits) >= 4 else "***"
