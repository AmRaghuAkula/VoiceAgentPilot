"""Recovery and verification of claim cells (spec 7.4 "Stale and verified
claims", "Recovery"; plan UC04c).

Recovery is judged **per cell**, on the cell's own record:

| Cell met | Check | Outcome |
| --- | --- | --- |
| Stale `pending` | `find_bookings` over the record's `[start, end)` with the record's own `binding_id` finds an event carrying its fingerprint | `finalized`: the owner did create. The cells of the range that carry the **same `attempt_id`** and are themselves stale become `booked` + `event_id` |
| Stale `pending` | No such event | `released`: the owner never booked. The same-attempt stale cells are released; a newer, live attempt's cells are never touched |
| `booked` | The event exists and the cell lies in `[event.start, event.end + record.buffer)` (the event's **current** times, as claim cells: `floor5` .. `ceil5`) | `kept`: it keeps blocking, buffer tail included |
| `booked` | `get_event` returns None, or the cell is outside that range | `released` (this cell only) |

A `pending` cell younger than `CLAIM_TTL` (storage-server age only, SF-1) is
`held`: a live attempt, never touched. Every write is a compare-and-swap with
the etag that was read; a lost one re-reads the cell and re-evaluates (bounded
by `MAX_CAS_ROUNDS`, then `held`). A `find_bookings`, `get_event` or claim-store
failure, or no budget left, leaves the cell untouched: `failed`, treated as
held, with the cause in `error` (its own `diagnostic`/`reason`).

`get_event` runs at most once per distinct `event_id` per request: the caller
passes one `verify_cache` dict for the whole request. Only answers are cached
(an event, or None for gone), never a failure.

Recovery decides on the event's private metadata (fingerprint) and times only.
Event titles and descriptions hold caller-written text and are never read
here (F4: untrusted).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeVar

from calendar_tools.core.claims import (
    ClaimRecord,
    ClaimStore,
    ClaimStoreUnavailable,
    Held,
    PreconditionFailed,
)
from calendar_tools.core.clock import Clock
from calendar_tools.core.deadline import CLAIM_TIMEOUT, PROVIDER_TIMEOUT, Deadline, DeadlineExceeded
from calendar_tools.core.identity import CELL_MINUTES, CellKey, ceil5, floor5
from calendar_tools.core.ports import BookingRecord, CalendarProvider, CalendarRef, ProviderError, ProviderTimeout

__all__ = [
    "CLAIM_TTL",
    "MAX_CAS_ROUNDS",
    "Recoverer",
    "RecoveryOutcome",
    "cells_of",
]

# A pending claim older than this (store-server time) is stale: far longer than
# the 8 s request deadline, so its owning attempt has certainly ended.
CLAIM_TTL = timedelta(seconds=120)
# A recoverer that keeps losing compare-and-swaps on one cell stops and treats
# the cell as held (another writer is active on it).
MAX_CAS_ROUNDS = 3

Action = Literal["released", "finalized", "kept", "held", "failed"]
T = TypeVar("T")


@dataclass(frozen=True)
class RecoveryOutcome:
    """What recovery left in the cell it was given.

    - `released`: the cell is empty now (`acted` says whether this recoverer
      wrote the release, or found it already emptied by someone else).
    - `finalized`: a stale pending cell whose owner did create is now `booked`
      (`event` is that booking).
    - `kept`: a verified `booked` cell (`event` is its booking).
    - `held`: a live claim (or contention): untouched.
    - `failed`: a provider, claim-store or deadline failure: untouched,
      treated as held; `error` carries the diagnostic and reason.
    """

    action: Action
    held: Held | None = None
    event: BookingRecord | None = None
    error: Exception | None = None
    acted: bool = False
    reason: str | None = None  # what was recovered, for the log line's `reason`
    # True when a lost compare-and-swap made recovery judge a re-read record,
    # which may belong to a different holder than the one the caller met: the
    # caller must re-classify the cell rather than act on its first reading.
    rechecked: bool = False

    @property
    def freed(self) -> bool:
        return self.action == "released"


class _LostCas(Exception):
    """A compare-and-swap on the cell met lost: re-read and re-evaluate."""


def cells_of(record: ClaimRecord) -> list[CellKey]:
    """Every cell of a record's range, in ascending order."""
    step = timedelta(minutes=CELL_MINUTES)
    count = (record.last_cell - record.first_cell) // step + 1
    return [CellKey(record.calendar_key, record.first_cell + i * step) for i in range(count)]


def _covers(event: BookingRecord, cell: CellKey, buffer_minutes: int) -> bool:
    """The cell lies in the event's claim range, by its current times."""
    try:
        hi = ceil5(event.end + timedelta(minutes=buffer_minutes))
    except OverflowError:
        return False
    return floor5(event.start) <= cell.start < hi


class Recoverer:
    """Recovery for one request: its provider and calendar, its deadline, and
    the budget it must leave untouched (`reserve`, e.g. a pending create)."""

    def __init__(
        self,
        provider: CalendarProvider,
        claim_store: ClaimStore,
        clock: Clock,
        *,
        cal: CalendarRef,
        deadline: Deadline,
        reserve: float = 0.0,
        claim_ttl: timedelta | None = None,
        on_provider_ms: Callable[[int], None] | None = None,
    ) -> None:
        self._provider = provider
        self._store = claim_store
        self._clock = clock
        self._cal = cal
        self._deadline = deadline
        self._reserve = reserve
        self._ttl = CLAIM_TTL if claim_ttl is None else claim_ttl
        self._on_provider_ms = on_provider_ms

    def is_stale(self, held: Held) -> bool:
        return held.record.state == "pending" and held.age >= self._ttl

    async def recover_cell(
        self, cell: CellKey, held: Held, verify_cache: dict[str, BookingRecord | None]
    ) -> RecoveryOutcome:
        if not isinstance(cell, CellKey) or not isinstance(held, Held) or not held.record.covers(cell):
            raise ValueError("held must be the record read from this cell")
        current: Held | None = held
        rechecked = False
        for _ in range(MAX_CAS_ROUNDS):
            assert current is not None
            try:
                outcome = await self._evaluate(cell, current, verify_cache)
                return dataclasses.replace(outcome, rechecked=rechecked)
            except _LostCas:
                pass
            except (ProviderError, ClaimStoreUnavailable, DeadlineExceeded) as err:
                return RecoveryOutcome("failed", held=current, error=err, rechecked=rechecked)
            rechecked = True
            try:
                current = await self._claim_call(lambda: self._store.read(cell))
            except (ClaimStoreUnavailable, DeadlineExceeded) as err:
                return RecoveryOutcome("failed", held=current, error=err, rechecked=True)
            if current is None:
                return RecoveryOutcome("released", rechecked=True)  # someone else freed it
        return RecoveryOutcome("held", held=current, rechecked=True)

    # --- the table -------------------------------------------------------------------

    async def _evaluate(
        self, cell: CellKey, held: Held, verify_cache: dict[str, BookingRecord | None]
    ) -> RecoveryOutcome:
        record = held.record
        if record.state == "booked":
            return await self._verify(cell, held, verify_cache)
        if held.age < self._ttl:
            return RecoveryOutcome("held", held=held)
        found = await self._provider_call(
            lambda: self._provider.find_bookings(self._cal, record.start, record.end, record.binding_id)
        )
        ours = [r for r in found if r.meta.fingerprint == record.fingerprint]
        siblings = await self._stale_siblings(cell, record)
        if ours:
            event = ours[0]
            try:
                booked = record.booked(event.event_id)
            except (TypeError, ValueError):
                # An event ID the record cannot hold: leave the cell alone.
                return RecoveryOutcome("held", held=held)
            await self._cas(lambda: self._store.replace(cell, booked, held.etag))
            await self._best_effort(self._store.replace(key, booked, h.etag) for key, h in siblings)
            return RecoveryOutcome("finalized", event=event, acted=True, reason="stale_finalized")
        await self._cas(lambda: self._store.release(cell, held.etag))
        await self._best_effort(self._store.release(key, h.etag) for key, h in siblings)
        return RecoveryOutcome("released", acted=True, reason="stale_released")

    async def _verify(
        self, cell: CellKey, held: Held, verify_cache: dict[str, BookingRecord | None]
    ) -> RecoveryOutcome:
        record = held.record
        event_id = record.event_id
        assert event_id is not None  # a booked record always has one
        if event_id in verify_cache:
            event = verify_cache[event_id]
        else:
            event = await self._provider_call(lambda: self._provider.get_event(self._cal, event_id))
            verify_cache[event_id] = event
        if event is not None and _covers(event, cell, record.buffer_minutes):
            return RecoveryOutcome("kept", held=held, event=event)
        await self._cas(lambda: self._store.release(cell, held.etag))
        return RecoveryOutcome("released", acted=True, reason="unverified_released")

    async def _stale_siblings(self, cell: CellKey, record: ClaimRecord) -> list[tuple[CellKey, Held]]:
        """The other cells of the range that carry the same attempt, still
        pending and themselves stale (best effort: an unreadable cell is left
        alone)."""
        others = [key for key in cells_of(record) if key != cell]
        if not others:
            return []
        try:
            timeout = self._deadline.timeout_for(CLAIM_TIMEOUT, self._reserve)
        except DeadlineExceeded:
            return []
        results = await asyncio.gather(
            *(asyncio.wait_for(self._store.read(key), timeout) for key in others), return_exceptions=True
        )
        matched: list[tuple[CellKey, Held]] = []
        for key, sibling in zip(others, results, strict=True):
            if (
                isinstance(sibling, Held)
                and sibling.record.attempt_id == record.attempt_id
                and sibling.record.fingerprint == record.fingerprint
                and self.is_stale(sibling)
            ):
                matched.append((key, sibling))
        return matched

    # --- bounded calls -----------------------------------------------------------------

    async def _provider_call(self, call: Callable[[], Awaitable[T]]) -> T:
        timeout = self._deadline.timeout_for(PROVIDER_TIMEOUT, self._reserve)
        started: datetime = self._clock.now()
        try:
            return await asyncio.wait_for(call(), timeout)
        except TimeoutError:
            if timeout < PROVIDER_TIMEOUT:
                raise DeadlineExceeded() from None
            raise ProviderTimeout(maybe_committed=False, reason="timeout") from None
        finally:
            if self._on_provider_ms is not None:
                self._on_provider_ms(round((self._clock.now() - started).total_seconds() * 1000))

    async def _claim_call(self, call: Callable[[], Awaitable[T]]) -> T:
        timeout = self._deadline.timeout_for(CLAIM_TIMEOUT, self._reserve)
        try:
            return await asyncio.wait_for(call(), timeout)
        except TimeoutError:
            if timeout < CLAIM_TIMEOUT:
                raise DeadlineExceeded() from None
            raise ClaimStoreUnavailable("timeout") from None

    async def _cas(self, call: Callable[[], Awaitable[object]]) -> None:
        try:
            await self._claim_call(call)
        except PreconditionFailed:
            raise _LostCas() from None

    async def _best_effort(self, calls) -> None:  # type: ignore[no-untyped-def]
        """Sibling writes, in parallel; a lost or failed one is left for the
        next recoverer that meets that cell."""
        pending = list(calls)
        if not pending:
            return
        try:
            timeout = self._deadline.timeout_for(CLAIM_TIMEOUT, self._reserve)
        except DeadlineExceeded:
            for coro in pending:
                coro.close()
            return
        await asyncio.gather(*(asyncio.wait_for(c, timeout) for c in pending), return_exceptions=True)
