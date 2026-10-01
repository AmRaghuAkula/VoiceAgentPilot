"""Ports: the interfaces the framework-free core depends on.

The Notifier-shaped types are the contract copied into the plan (plan section 2, P1).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


# --- Notifier shape (plan P1) ---------------------------------------------------------


@dataclass(frozen=True)
class NotifierConfig:
    notifier: str
    channel_config: Mapping[str, JsonValue]  # e.g. the sender number
    credential_secret_name: str | None  # None = the service's managed identity
    recipients: tuple[str, ...]  # from config only; validated by validate_config


@dataclass(frozen=True)
class OutboundMessage:
    subject: str | None  # None for SMS
    text_body: str  # plain text, already normalized
    idempotency_key: str  # here: the sha256 of the normalized text


@dataclass(frozen=True)
class NotifierCapabilities:
    formats: frozenset[str]
    max_body_chars: int
    native_idempotency: bool
    leaves_boundary: bool
    residency: str  # "ca" | "us" | "global" | "configurable"


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str | None  # opaque; logged for tracing


class Notifier(Protocol):
    name: ClassVar[str]

    def capabilities(self, cfg: NotifierConfig) -> NotifierCapabilities: ...

    def validate_config(self, cfg: NotifierConfig) -> None: ...

    async def send(self, cfg: NotifierConfig, message: OutboundMessage) -> SendResult: ...


# --- State, secrets, clock --------------------------------------------------------------


@dataclass(frozen=True)
class StoredItem:
    body: bytes
    etag: str


class StateStore(Protocol):
    """A small key/blob store with conditional writes.

    Every method raises ``StateStoreUnavailable`` when the store can't be reached.
    """

    async def get(self, key: str, *, timeout: float) -> StoredItem | None:
        """Return the item, or None if it doesn't exist."""
        ...

    async def create(self, key: str, body: bytes, *, timeout: float) -> str | None:
        """Create only if absent (If-None-Match: *). Return the new etag, or None if it exists."""
        ...

    async def replace(self, key: str, body: bytes, etag: str, *, timeout: float) -> str | None:
        """Replace only if the etag matches (If-Match). Return the new etag, or None on a lost race."""
        ...


class SecretSource(Protocol):
    """Raises ``SecretStoreUnavailable`` when the secret can't be read."""

    async def get_secret(self, name: str, *, timeout: float) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC wall-clock time."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; used for deadlines and caches."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
