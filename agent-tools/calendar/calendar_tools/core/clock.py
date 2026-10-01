"""Injected time and randomness (plan P9): core logic never reads
`datetime.now()` or `secrets` directly, so every test is deterministic."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, tz-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class NonceSource(Protocol):
    def new_hex(self, nbytes: int) -> str:
        """`nbytes` random bytes as lowercase hex (for `attempt_id`, `request_id`)."""
        ...


class SystemNonceSource:
    def new_hex(self, nbytes: int) -> str:
        return secrets.token_hex(nbytes)
