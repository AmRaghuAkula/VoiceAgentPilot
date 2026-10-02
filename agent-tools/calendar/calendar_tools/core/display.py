"""The `display` string of a slot (spec 5.2): a formatted value for reading
aloud, not a sentence. English only in v1 (spec 4.1 requires an `en-*`
locale). Names come from fixed tables, not the process locale, so output never
depends on the host's settings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def format_slot(start_utc: datetime, tz: ZoneInfo, locale: str) -> str:
    """`"Monday, October 5 at 10:00 AM"` in the binding's zone."""
    if start_utc.tzinfo is None or start_utc.utcoffset() != timedelta(0):
        raise ValueError("start must be a tz-aware UTC datetime")
    if not isinstance(locale, str) or not (locale == "en" or locale.startswith("en-")):
        raise ValueError("only English locales are supported")
    wall = start_utc.astimezone(tz)
    hour = wall.hour % 12 or 12
    meridiem = "AM" if wall.hour < 12 else "PM"
    return f"{_WEEKDAYS[wall.weekday()]}, {_MONTHS[wall.month - 1]} {wall.day} at {hour}:{wall.minute:02d} {meridiem}"
