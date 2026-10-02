"""The slot engine (spec section 7.1; plan UC03, P16). Pure: no I/O, no clock.

1. Bookable windows are built per local date in the binding's wall-clock time,
   and each boundary is resolved to UTC: an ambiguous time (fall-back) uses its
   first occurrence (`fold=0`); a time that does not exist (spring-forward)
   moves forward to the first instant that exists.
2. Candidate starts sit on a grid anchored on each window's UTC start and step
   in elapsed time. A candidate whose local wall time is the repeated hour's
   second occurrence (`fold=1`) is dropped, so no two offered slots share one
   spoken time. The caller's time filter only removes candidates; it never
   shifts the grid (round-3 SF-6). A candidate must lie inside its window,
   start at or after `now + min_notice`, and not overlap busy time widened by
   `buffer` on each side.
3. Selection: `earliest` takes the first `max_slots`; `spread` takes the
   earliest of each day in turn, then the second, until the cap, returned in
   time order. `more_available` is true if any free candidate was left out.

`reason`: `ok`; `beyond_booking_horizon` (the range starts past the horizon);
`outside_bookable_hours` (no bookable time in the effective range once the
filter and minimum notice are applied); `fully_booked` (bookable time existed
but all of it was busy).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from calendar_tools.core.bindings import WEEKDAYS
from calendar_tools.core.ports import Interval

SlotReason = Literal["ok", "fully_booked", "outside_bookable_hours", "beyond_booking_horizon"]
SELECTIONS = frozenset({"earliest", "spread"})
_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class SlotResult:
    slots: list[Interval]
    more_available: bool
    reason: SlotReason
    # The UTC span from the first bookable window's start to the last one's end
    # in the effective range (before the time filter); None if there is none.
    searched: Interval | None = None


def _require_utc(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a tz-aware UTC datetime")


def resolve_local(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """A local wall time as a UTC instant (P16): `fold=0` when ambiguous; when
    it does not exist, the first instant that does."""
    naive = datetime.combine(day, wall)
    first = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if first.astimezone(tz).replace(tzinfo=None) == naive:
        return first
    # In a gap: the two folds bracket the transition. Walk forward to the first
    # minute whose wall time is at or after the requested one.
    second = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    instant, end = min(first, second), max(first, second)
    while instant < end and instant.astimezone(tz).replace(tzinfo=None) < naive:
        instant += _MINUTE
    return instant


def _windows(
    start_day: date, end_day: date, hours: Mapping[str, Sequence[tuple[time, time]]], tz: ZoneInfo
) -> list[tuple[date, datetime, datetime]]:
    out: list[tuple[date, datetime, datetime]] = []
    day = start_day
    while day <= end_day:
        for wall_start, wall_end in hours.get(WEEKDAYS[day.weekday()], ()):
            start = resolve_local(day, wall_start, tz)
            end = resolve_local(day, wall_end, tz)
            if end > start:  # a window wholly inside a DST gap has no time
                out.append((day, start, end))
        day += timedelta(days=1)
    return out


def _merged_busy(busy: Sequence[Interval], buffer: timedelta) -> tuple[list[datetime], list[datetime]]:
    spans = sorted((b.start - buffer, b.end + buffer) for b in busy)
    starts: list[datetime] = []
    ends: list[datetime] = []
    for s, e in spans:
        if starts and s <= ends[-1]:
            ends[-1] = max(ends[-1], e)
        else:
            starts.append(s)
            ends.append(e)
    return starts, ends


def _is_busy(start: datetime, end: datetime, starts: list[datetime], ends: list[datetime]) -> bool:
    # The last merged busy span starting before `end` is the only one that can overlap.
    i = bisect.bisect_left(starts, end) - 1
    return i >= 0 and ends[i] > start


def _select(free: list[tuple[date, Interval]], selection: str, max_slots: int) -> list[Interval]:
    if selection == "earliest":
        return [slot for _, slot in free[:max_slots]]
    by_day: dict[date, list[Interval]] = {}
    for day, slot in free:
        by_day.setdefault(day, []).append(slot)
    chosen: list[Interval] = []
    rank = 0
    while len(chosen) < max_slots:
        picked = False
        for day_slots in by_day.values():
            if rank < len(day_slots) and len(chosen) < max_slots:
                chosen.append(day_slots[rank])
                picked = True
        if not picked:
            break
        rank += 1
    return sorted(chosen, key=lambda s: s.start)


def compute_slots(
    *,
    window_start_local: date,
    window_end_local: date,
    bookable_hours: Mapping[str, Sequence[tuple[time, time]]],
    tz: ZoneInfo,
    busy: Sequence[Interval],
    duration: int,
    step: int,
    buffer: int,
    min_notice: int,
    now: datetime,
    horizon_end: date,
    earliest_time: time | None,
    latest_time: time | None,
    selection: str,
    max_slots: int,
) -> SlotResult:
    """Spec 7.1 steps 1-3. Durations, step, buffer and notice are minutes;
    `horizon_end` is the last bookable local date (inclusive)."""
    _require_utc("now", now)
    if selection not in SELECTIONS:
        raise ValueError("unknown slot selection")
    if duration <= 0 or step <= 0 or buffer < 0 or min_notice < 0 or max_slots <= 0:
        raise ValueError("durations, step and max_slots must be positive")

    if window_start_local > horizon_end:
        return SlotResult([], False, "beyond_booking_horizon")
    end_day = min(window_end_local, horizon_end)
    windows = _windows(window_start_local, end_day, bookable_hours, tz)
    searched = Interval(windows[0][1], windows[-1][2]) if windows else None

    length = timedelta(minutes=duration)
    stride = timedelta(minutes=step)
    not_before = now + timedelta(minutes=min_notice)
    busy_starts, busy_ends = _merged_busy(busy, timedelta(minutes=buffer))

    any_candidate = False
    free: list[tuple[date, Interval]] = []
    for day, win_start, win_end in windows:
        start = win_start
        while start + length <= win_end:
            end = start + length
            candidate_start, candidate_end = start, end
            start += stride
            wall = candidate_start.astimezone(tz)
            if wall.fold == 1:
                continue  # the repeated hour's second occurrence (P16)
            if earliest_time is not None and wall.time() < earliest_time:
                continue
            if latest_time is not None and candidate_end.astimezone(tz).time() > latest_time:
                continue
            if candidate_start < not_before:
                continue
            any_candidate = True
            if _is_busy(candidate_start, candidate_end, busy_starts, busy_ends):
                continue
            free.append((day, Interval(candidate_start, candidate_end)))

    if not free:
        reason: SlotReason = "fully_booked" if any_candidate else "outside_bookable_hours"
        return SlotResult([], False, reason, searched)
    chosen = _select(free, selection, max_slots)
    return SlotResult(chosen, len(free) > len(chosen), "ok", searched)
