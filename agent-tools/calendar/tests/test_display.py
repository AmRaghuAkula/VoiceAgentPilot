"""`display.format_slot` (spec 5.2; plan UC03): English only, no leading zero on
the hour, minutes always two digits, rendered in the binding's zone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from calendar_tools.core.display import format_slot

TORONTO = ZoneInfo("America/Toronto")
LONDON = ZoneInfo("Europe/London")


@pytest.mark.parametrize(
    ("when", "tz", "expected"),
    [
        (datetime(2026, 10, 5, 14, 0, tzinfo=UTC), TORONTO, "Monday, October 5 at 10:00 AM"),
        (datetime(2026, 10, 6, 18, 30, tzinfo=UTC), TORONTO, "Tuesday, October 6 at 2:30 PM"),
        (datetime(2026, 10, 10, 4, 5, tzinfo=UTC), TORONTO, "Saturday, October 10 at 12:05 AM"),
        (datetime(2026, 10, 10, 16, 0, tzinfo=UTC), TORONTO, "Saturday, October 10 at 12:00 PM"),
        (datetime(2026, 12, 31, 23, 55, tzinfo=UTC), TORONTO, "Thursday, December 31 at 6:55 PM"),
        (datetime(2027, 1, 1, 9, 0, tzinfo=UTC), LONDON, "Friday, January 1 at 9:00 AM"),
        # The zone decides the date, not UTC.
        (datetime(2026, 10, 7, 2, 0, tzinfo=UTC), TORONTO, "Tuesday, October 6 at 10:00 PM"),
        # Fall-back: the first 1:30 AM is EDT.
        (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), TORONTO, "Sunday, November 1 at 1:30 AM"),
    ],
)
def test_format_slot(when, tz, expected) -> None:
    assert format_slot(when, tz, "en-CA") == expected


@pytest.mark.parametrize("locale", ["en", "en-US", "en-GB"])
def test_any_english_locale(locale) -> None:
    assert format_slot(datetime(2026, 10, 5, 14, 0, tzinfo=UTC), TORONTO, locale) == "Monday, October 5 at 10:00 AM"


@pytest.mark.parametrize("locale", ["fr-CA", "de", ""])
def test_non_english_locale_is_refused(locale) -> None:
    with pytest.raises(ValueError):
        format_slot(datetime(2026, 10, 5, 14, 0, tzinfo=UTC), TORONTO, locale)


def test_naive_start_is_refused() -> None:
    with pytest.raises(ValueError):
        format_slot(datetime(2026, 10, 5, 14, 0), TORONTO, "en-CA")
