"""Adapter-author entry point (plan P2): re-exports the core port unchanged."""

from calendar_tools.core.ports import (
    BookingMeta,
    BookingRecord,
    CalendarProvider,
    CalendarRef,
    Interval,
    NewEvent,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    SecretInvalid,
    SecretStoreUnavailable,
)

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
