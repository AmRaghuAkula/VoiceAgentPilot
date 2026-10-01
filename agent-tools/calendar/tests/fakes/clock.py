"""Deterministic clock and nonce source for tests (plan P9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_START = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class FakeClock:
    """A `Clock` whose time moves only when a test (or `sleep`) moves it."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("FakeClock start must be tz-aware UTC")
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        if when.tzinfo is None or when.utcoffset() != timedelta(0):
            raise ValueError("FakeClock time must be tz-aware UTC")
        self._now = when

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeNonceSource:
    """A `NonceSource` returning a predictable, increasing hex sequence."""

    def __init__(self) -> None:
        self._counter = 0

    def new_hex(self, nbytes: int) -> str:
        self._counter += 1
        return self._counter.to_bytes(nbytes, "big").hex()
