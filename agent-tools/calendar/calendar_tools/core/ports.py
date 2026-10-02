"""The provider port: section 6.2 types, the `CalendarProvider` Protocol and the
core exceptions (plan P2, P3; rev 1.6 `SecretInvalid`).

These live in `core/` (not `providers/base.py`, which re-exports them) so the
booking algorithm can use them without `core/` importing `providers.*` (G1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Protocol, runtime_checkable

__all__ = [
    "BookingMeta",
    "BookingRecord",
    "CalendarProvider",
    "CalendarRef",
    "Interval",
    "NewEvent",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "SecretInvalid",
    "SecretStoreUnavailable",
]


def _require_utc(field_name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        # The message names the field only, never the value.
        raise ValueError(f"{field_name} must be a tz-aware UTC datetime")


@dataclass(frozen=True)
class CalendarRef:  # opaque to the core
    provider: str
    calendar_id: str
    credential_secret_name: str


@dataclass(frozen=True)
class Interval:
    start: datetime  # tz-aware, UTC
    end: datetime

    def __post_init__(self) -> None:
        _require_utc("start", self.start)
        _require_utc("end", self.end)


@dataclass(frozen=True)
class BookingMeta:  # what the service stamps on every event it creates
    service_tag: str  # constant, identifies events created by this service
    binding_id: str
    fingerprint: str  # see spec section 7.4
    contact_tag: str  # HMAC(normalized contact identity)
    booking_ref: str


@dataclass(frozen=True)
class NewEvent:
    start: datetime
    end: datetime
    timezone: str
    title: str
    description: str
    meta: BookingMeta
    request_key: str  # deterministic per logical request; adapter may use for native retry dedupe

    def __post_init__(self) -> None:
        _require_utc("start", self.start)
        _require_utc("end", self.end)


@dataclass(frozen=True)
class BookingRecord:  # always an active (not cancelled/deleted) booking
    event_id: str
    start: datetime
    end: datetime
    meta: BookingMeta  # parsed back from provider-native private metadata

    def __post_init__(self) -> None:
        _require_utc("start", self.start)
        _require_utc("end", self.end)


@runtime_checkable
class CalendarProvider(Protocol):
    name: ClassVar[str]

    async def resolve_calendar_identity(self, cal: CalendarRef) -> str: ...

    async def get_busy(self, cal: CalendarRef, start: datetime, end: datetime) -> list[Interval]: ...

    async def find_bookings(
        self,
        cal: CalendarRef,
        start: datetime,
        end: datetime,
        binding_id: str | None = None,
    ) -> list[BookingRecord]: ...

    async def get_event(self, cal: CalendarRef, event_id: str) -> BookingRecord | None: ...

    async def create_event(self, cal: CalendarRef, event: NewEvent) -> BookingRecord: ...

    async def delete_event(self, cal: CalendarRef, event_id: str) -> None: ...


# --- Exceptions -------------------------------------------------------------
#
# Adapters (and secret sources) raise only these. Each class carries the spec
# section 10 `diagnostic` code; each instance an optional `reason` from a closed,
# code-defined list. A reason is never a value, a token, a contact field or
# vendor free text, and `str()` holds only the diagnostic and the reason.


# A reason code: short, code-shaped (closed lists are defined where each code is
# raised). Secret names (`<name>.<check>`) and comma-joined field names fit.
# 256 fits the longest secret reason (127-character Key Vault name + "." + check)
# and comma-joined schema field names.
REASON_PATTERN = re.compile(r"[A-Za-z0-9_.,-]{1,256}")
INVALID_REASON = "invalid_reason"
_DIGIT_RUN = re.compile(r"\d{7,}")


def is_reason_code(value: object) -> bool:
    """A reason is a code-shaped string with no phone-like digit run (D-006);
    separators are ignored for the digit-run check."""
    if not isinstance(value, str) or not REASON_PATTERN.fullmatch(value):
        return False
    return _DIGIT_RUN.search(re.sub(r"[-.,]", "", value)) is None


class ProviderError(Exception):
    """Base class. Raising it directly is a programming error, so its
    diagnostic is the alerted `unclassified`."""

    diagnostic: ClassVar[str] = "unclassified"

    def __init__(self, reason: str | None = None) -> None:
        if reason is not None and not isinstance(reason, str):
            raise TypeError("reason must be a str or None")
        if reason is not None and not is_reason_code(reason):
            # A reason is a code, never a value or vendor text: anything else is
            # replaced, so it can't reach str() or the log line. obs.log_request
            # fails the test suite (STRICT) when it sees the replacement.
            reason = INVALID_REASON
        self.reason = reason
        super().__init__(self._text())

    def _text(self) -> str:
        return self.diagnostic if self.reason is None else f"{self.diagnostic}: {self.reason}"

    def __str__(self) -> str:
        return self._text()


class ProviderUnavailable(ProviderError):
    """Vendor 5xx, 429 after retries, network error, DNS failure."""

    diagnostic: ClassVar[str] = "provider_unavailable"


class ProviderAuthError(ProviderError):
    """Token refresh rejected, revoked consent, 401/403 from the vendor."""

    diagnostic: ClassVar[str] = "credential_rejected"


class ProviderConfigError(ProviderError):
    """Calendar not found, not shared, insufficient scope, or a malformed provider
    credential (spec F5)."""

    diagnostic: ClassVar[str] = "provider_config_error"


class ProviderTimeout(ProviderError):
    """A timeout. `maybe_committed=True` only for a write whose request may have
    reached the vendor."""

    diagnostic: ClassVar[str] = "provider_timeout"

    def __init__(self, maybe_committed: bool, reason: str | None = None) -> None:
        if not isinstance(maybe_committed, bool):
            raise TypeError("maybe_committed must be a bool")
        self.maybe_committed = maybe_committed
        super().__init__(reason)


class SecretStoreUnavailable(ProviderError):
    """The secret store could not be read (plan P3). Allowed across the adapter
    boundary; maps to `calendar_unavailable` + `secret_store_unreachable`."""

    diagnostic: ClassVar[str] = "secret_store_unreachable"


SECRET_CHECKS: frozenset[str] = frozenset(
    {"missing", "empty", "not_base64", "wrong_length", "bad_json", "wrong_kind"}
)

# Key Vault secret names: 1-127 characters, letters, digits and dashes.
_SECRET_NAME = re.compile(r"[A-Za-z0-9-]{1,127}")


class SecretInvalid(SecretStoreUnavailable):
    """A secret was read but is absent or fails its shape check (rev 1.6).

    A subclass of `SecretStoreUnavailable`, so every existing mapping still
    catches it. It holds the secret's *name* and the failed check only; it has
    no field for a value.
    """

    diagnostic: ClassVar[str] = "secret_invalid"

    def __init__(self, secret_name: str, check: str) -> None:
        if not isinstance(secret_name, str) or not _SECRET_NAME.fullmatch(secret_name):
            raise ValueError("secret_name must be a Key Vault secret name")
        if check not in SECRET_CHECKS:
            raise ValueError("check must be one of the documented secret checks")
        self.secret_name = secret_name
        self.check = check
        super().__init__(f"{secret_name}.{check}")
