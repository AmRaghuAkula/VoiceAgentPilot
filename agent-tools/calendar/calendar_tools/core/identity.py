"""Identity HMACs and claim cells (spec 7.4 "Definitions", "HMAC encoding",
"Claim cells"; plan UC04a).

Every value here is an HMAC under the fingerprint key (`k_fp`), a time, or a
cell name built from them: nothing personal and nothing reversible. Each HMAC
input is `canonical_encode`d with a leading domain label, so no two kinds of
value can ever share an input (`booking_ref` uses the spec's own `"ref"`).

Pure and synchronous, except `CalendarIdentityCache`, which makes the one
cached adapter call that `calendar_key` needs (spec 7.4 step 1).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from calendar_tools.core.contact import Contact
from calendar_tools.core.deadline import PROVIDER_TIMEOUT, Deadline, DeadlineExceeded
from calendar_tools.core.encoding import canonical_encode, encode_time
from calendar_tools.core.keys import KEY_BYTES
from calendar_tools.core.ports import CalendarProvider, CalendarRef, ProviderConfigError, ProviderTimeout

__all__ = [
    "CELL_MINUTES",
    "CalendarIdentityCache",
    "CellKey",
    "booking_ref",
    "calendar_key",
    "cell_range",
    "ceil5",
    "contact_tag",
    "fingerprint",
    "floor5",
]

CELL_MINUTES = 5
_CELL = timedelta(minutes=CELL_MINUTES)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_REF_CHARS = 6
_MAX_CANONICAL_ID = 1024

_DOMAIN_CALENDAR = "calendar_key|v1"
_DOMAIN_CONTACT = "contact_tag|v1"
_DOMAIN_FINGERPRINT = "fingerprint|v1"
_DOMAIN_REF = "ref"  # spec 7.4: HMAC(k_fp, "ref" || fingerprint)


def _require_key(k_fp: object) -> bytes:
    if not isinstance(k_fp, bytes) or len(k_fp) != KEY_BYTES:
        # Names the key, never its value or length.
        raise ValueError(f"the fingerprint key must be exactly {KEY_BYTES} bytes")
    return k_fp


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _require_hex64(name: str, value: object) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


def _require_utc(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a tz-aware UTC datetime")
    return value


def _mac(k_fp: bytes, *fields: object) -> bytes:
    return hmac.new(_require_key(k_fp), canonical_encode(*fields), hashlib.sha256).digest()


def calendar_key(k_fp: bytes, provider: str, canonical_calendar_id: str) -> str:
    """Per physical calendar (round-3 SF-4): HMAC(k_fp, provider || canonical id)."""
    _require_text("provider", provider)
    _require_text("canonical_calendar_id", canonical_calendar_id)
    return _mac(k_fp, _DOMAIN_CALENDAR, provider, canonical_calendar_id).hex()


def contact_tag(k_fp: bytes, contact: Contact) -> str:
    """HMAC(k_fp, the E.164 phone, or the lowercased email if there is no phone)."""
    if not isinstance(contact, Contact):
        raise TypeError("contact must be a Contact")
    identity = contact.identity
    if identity is None:
        raise ValueError("a contact needs a phone or an email for its tag")
    return _mac(k_fp, _DOMAIN_CONTACT, identity).hex()


def fingerprint(
    k_fp: bytes,
    calendar_key: str,
    start: datetime,
    end: datetime,
    contact_tag: str,
    appointment_type: str,
) -> str:
    """HMAC(k_fp, calendar_key || start || end || contact_tag || appointment_type).

    No name and no binding ID (spec 7.4). Also the booking's `request_key`.
    """
    _require_hex64("calendar_key", calendar_key)
    _require_hex64("contact_tag", contact_tag)
    _require_text("appointment_type", appointment_type)
    _require_utc("start", start)
    _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")
    return _mac(
        k_fp, _DOMAIN_FINGERPRINT, calendar_key, encode_time(start), encode_time(end), contact_tag, appointment_type
    ).hex()


def booking_ref(k_fp: bytes, fingerprint: str) -> str:
    """The first 6 characters of Crockford-base32(HMAC(k_fp, "ref" || fingerprint)),
    uppercase: the first 30 bits of the MAC, 5 bits per character."""
    _require_text("fingerprint", fingerprint)
    bits = int.from_bytes(_mac(k_fp, _DOMAIN_REF, fingerprint)[:4], "big") >> 2
    return "".join(_CROCKFORD[(bits >> (5 * (_REF_CHARS - 1 - i))) & 31] for i in range(_REF_CHARS))


# --- claim cells -----------------------------------------------------------------


def floor5(value: datetime) -> datetime:
    _require_utc("time", value)
    return value.replace(minute=value.minute - value.minute % CELL_MINUTES, second=0, microsecond=0)


def ceil5(value: datetime) -> datetime:
    floored = floor5(value)
    return floored if floored == value else floored + _CELL


def cell_range(start: datetime, end: datetime, buffer_minutes: int) -> list[datetime]:
    """Every 5-minute UTC cell from `floor5(start)` up to, not including,
    `ceil5(end + buffer)` (spec 7.4 "Claim cells"): one-sided, the buffer
    follows the booking."""
    _require_utc("start", start)
    _require_utc("end", end)
    if isinstance(buffer_minutes, bool) or not isinstance(buffer_minutes, int):
        raise TypeError("buffer_minutes must be an int")
    if buffer_minutes < 0:
        raise ValueError("buffer_minutes must not be negative")
    if end <= start:
        raise ValueError("end must be after start")
    first = floor5(start)
    stop = ceil5(end + timedelta(minutes=buffer_minutes))
    count = (stop - first) // _CELL
    return [first + i * _CELL for i in range(count)]


@dataclass(frozen=True)
class CellKey:
    """One claim cell on one physical calendar. `name` is the store's object
    name: `{calendar_key}/{yyyymmddTHHMMZ}` (no colons, no path tricks: the
    calendar key is 64 hex characters, the time is fixed-width)."""

    calendar_key: str
    start: datetime

    def __post_init__(self) -> None:
        _require_hex64("calendar_key", self.calendar_key)
        _require_utc("start", self.start)
        if self.start != floor5(self.start):
            raise ValueError("a cell starts on the 5-minute UTC grid")

    @property
    def name(self) -> str:
        s = self.start
        return f"{self.calendar_key}/{s.year:04d}{s.month:02d}{s.day:02d}T{s.hour:02d}{s.minute:02d}Z"

    def __repr__(self) -> str:
        return f"CellKey({self.name})"


# --- calendar identity ---------------------------------------------------------------


class CalendarIdentityCache:
    """Caches each calendar reference's canonical identity (spec 7.4
    "Definitions": resolved lazily on first use, then cached).

    Keyed on `(provider, calendar_id, credential_secret_name)`. A failure
    propagates and is not cached, so the next request tries again. The canonical
    identity itself is held only in memory and never logged.
    """

    def __init__(self) -> None:
        self._canonical: dict[tuple[str, str, str], str] = {}

    async def canonical_id(self, provider: CalendarProvider, cal: CalendarRef, deadline: Deadline) -> str:
        cache_key = (cal.provider, cal.calendar_id, cal.credential_secret_name)
        cached = self._canonical.get(cache_key)
        if cached is not None:
            return cached
        timeout = deadline.timeout_for(PROVIDER_TIMEOUT)
        try:
            resolved = await asyncio.wait_for(provider.resolve_calendar_identity(cal), timeout)
        except TimeoutError:
            if timeout < PROVIDER_TIMEOUT:
                raise DeadlineExceeded() from None
            raise ProviderTimeout(maybe_committed=False, reason="timeout") from None
        if not isinstance(resolved, str) or not resolved or len(resolved) > _MAX_CANONICAL_ID:
            # An adapter bug: refuse rather than key claims on a bad identity.
            raise ProviderConfigError(reason="calendar_identity_invalid")
        self._canonical[cache_key] = resolved
        return resolved

    async def calendar_key(
        self, provider: CalendarProvider, cal: CalendarRef, k_fp: bytes, deadline: Deadline
    ) -> str:
        return calendar_key(k_fp, cal.provider, await self.canonical_id(provider, cal, deadline))
