"""`FakeCalendarProvider`: the in-memory adapter the core's tests use.

It passes the same conformance suite as every real adapter (spec section 6.5),
so it can't drift from real adapter semantics. Free/busy starts at spec section
6.2's floor (cancelled and transparent events don't block; everything else,
tentative and all-day included, blocks) and is aligned with Google's verified
behavior in UC08b.

Test knobs:
- `canonical_ids`: calendar_id -> canonical ID for `resolve_calendar_identity`
  (an unmapped ID is its own canonical ID). Events are stored per canonical ID,
  so two spellings of one calendar share one event store.
- `fail_next(method, exc)`: the next call of `method` raises `exc` (a core
  exception) and does nothing else. Queued per method, one-shot.
- `created_visible_after`: a newly created event is hidden from the next N
  `find_bookings` calls (eventual-consistency simulation for UC04c). It still
  blocks busy time.
- `add_external_event` / `cancel_event`: plant a non-service event, or cancel
  any event, as a human editing the calendar would.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar

from calendar_tools.core.ports import (
    BookingMeta,
    BookingRecord,
    CalendarRef,
    Interval,
    NewEvent,
    ProviderError,
)

_METHODS = frozenset(
    {
        "resolve_calendar_identity",
        "get_busy",
        "find_bookings",
        "get_event",
        "create_event",
        "delete_event",
    }
)
_STATUSES = frozenset({"confirmed", "tentative", "cancelled"})
_TRANSPARENCIES = frozenset({"opaque", "transparent"})


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a tz-aware UTC datetime")


@dataclass
class _FakeEvent:
    event_id: str
    start: datetime
    end: datetime
    status: str = "confirmed"
    transparency: str = "opaque"
    all_day: bool = False
    meta: BookingMeta | None = None  # private metadata; set only on service-created events
    request_key: str | None = None
    title: str = ""
    description: str = ""
    timezone: str = "UTC"
    deleted: bool = False
    hidden_reads: int = 0

    @property
    def active(self) -> bool:
        return not self.deleted and self.status != "cancelled"

    @property
    def blocks(self) -> bool:
        return self.active and self.transparency != "transparent"

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start

    def record(self) -> BookingRecord:
        assert self.meta is not None
        return BookingRecord(event_id=self.event_id, start=self.start, end=self.end, meta=self.meta)


class FakeCalendarProvider:
    name: ClassVar[str] = "fake"

    def __init__(self, name: str = "fake") -> None:
        # An instance name lets tests register two fakes under different names (G4).
        self.name = name  # type: ignore[misc]
        self.canonical_ids: dict[str, str] = {}
        self.created_visible_after = 0
        self.calls: list[str] = []
        self._events: dict[str, dict[str, _FakeEvent]] = defaultdict(dict)
        self._failures: dict[str, deque[ProviderError]] = defaultdict(deque)
        self._counter = 0

    # --- test knobs ---------------------------------------------------------

    def fail_next(self, method: str, exc: ProviderError) -> None:
        if method not in _METHODS:
            raise ValueError("unknown provider method")
        if not isinstance(exc, ProviderError):
            raise TypeError("adapters raise only core exceptions")
        self._failures[method].append(exc)

    def add_external_event(
        self,
        cal: CalendarRef,
        start: datetime,
        end: datetime,
        *,
        status: str = "confirmed",
        transparency: str = "opaque",
        all_day: bool = False,
    ) -> str:
        _require_utc("start", start)
        _require_utc("end", end)
        if end <= start:
            raise ValueError("end must be after start")
        if status not in _STATUSES or transparency not in _TRANSPARENCIES:
            raise ValueError("unknown status or transparency")
        event = _FakeEvent(
            event_id=self._new_id(),
            start=start,
            end=end,
            status=status,
            transparency=transparency,
            all_day=all_day,
        )
        self._store(cal)[event.event_id] = event
        return event.event_id

    def cancel_event(self, cal: CalendarRef, event_id: str) -> None:
        event = self._store(cal).get(event_id)
        if event is not None:
            event.status = "cancelled"

    # --- CalendarProvider ---------------------------------------------------

    async def resolve_calendar_identity(self, cal: CalendarRef) -> str:
        self._enter("resolve_calendar_identity")
        return self._canonical(cal)

    async def get_busy(self, cal: CalendarRef, start: datetime, end: datetime) -> list[Interval]:
        self._enter("get_busy")
        _require_utc("start", start)
        _require_utc("end", end)
        events = [e for e in self._store(cal).values() if e.blocks and e.overlaps(start, end)]
        return [Interval(e.start, e.end) for e in sorted(events, key=lambda e: (e.start, e.end))]

    async def find_bookings(
        self,
        cal: CalendarRef,
        start: datetime,
        end: datetime,
        binding_id: str | None = None,
    ) -> list[BookingRecord]:
        self._enter("find_bookings")
        _require_utc("start", start)
        _require_utc("end", end)
        found: list[_FakeEvent] = []
        for event in self._store(cal).values():
            if event.meta is None or not event.active or not event.overlaps(start, end):
                continue
            if binding_id is not None and event.meta.binding_id != binding_id:
                continue
            if event.hidden_reads > 0:
                event.hidden_reads -= 1
                continue
            found.append(event)
        return [e.record() for e in sorted(found, key=lambda e: (e.start, e.event_id))]

    async def get_event(self, cal: CalendarRef, event_id: str) -> BookingRecord | None:
        self._enter("get_event")
        event = self._store(cal).get(event_id)
        if event is None or event.meta is None or not event.active:
            return None
        return event.record()

    async def create_event(self, cal: CalendarRef, event: NewEvent) -> BookingRecord:
        self._enter("create_event")
        store = self._store(cal)
        for existing in store.values():  # native retry dedupe on request_key
            if existing.active and existing.meta is not None and existing.request_key == event.request_key:
                return existing.record()
        created = _FakeEvent(
            event_id=self._new_id(),
            start=event.start,
            end=event.end,
            meta=event.meta,
            request_key=event.request_key,
            title=event.title,
            description=event.description,
            timezone=event.timezone,
            hidden_reads=self.created_visible_after,
        )
        store[created.event_id] = created
        return created.record()

    async def delete_event(self, cal: CalendarRef, event_id: str) -> None:
        self._enter("delete_event")
        event = self._store(cal).get(event_id)
        if event is not None:
            event.deleted = True

    # --- internals ----------------------------------------------------------

    def _enter(self, method: str) -> None:
        self.calls.append(method)
        queue = self._failures.get(method)
        if queue:
            raise queue.popleft()

    def _canonical(self, cal: CalendarRef) -> str:
        return self.canonical_ids.get(cal.calendar_id, cal.calendar_id)

    def _store(self, cal: CalendarRef) -> dict[str, _FakeEvent]:
        return self._events[self._canonical(cal)]

    def _new_id(self) -> str:
        self._counter += 1
        return f"evt-{self._counter:04d}"
