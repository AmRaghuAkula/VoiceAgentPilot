"""The slot engine (spec 7.1; plan UC03, P16, round-3 SF-6).

Fixtures use the demo binding's shape (configuration only, fictional values):
America/Toronto, 14:00-19:00 Monday to Saturday, no Sunday, 30-minute slots,
24 h minimum notice, a 7-day horizon.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from calendar_tools.core.ports import Interval
from calendar_tools.core.slots import SlotResult, compute_slots, resolve_local

TZ = ZoneInfo("America/Toronto")
WEEKDAY_HOURS = {d: ((time(14, 0), time(19, 0)),) for d in ("mon", "tue", "wed", "thu", "fri", "sat")}
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)  # Monday 08:00 Toronto
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)
THU = date(2026, 10, 8)
SUN = date(2026, 10, 11)


def utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def local(d: date, h: int, mi: int = 0) -> datetime:
    """A Toronto wall time on an unambiguous day, as UTC."""
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=TZ).astimezone(UTC)


def run(**overrides: Any) -> SlotResult:
    args: dict[str, Any] = {
        "window_start_local": TUE,
        "window_end_local": TUE,
        "bookable_hours": WEEKDAY_HOURS,
        "tz": TZ,
        "busy": [],
        "duration": 30,
        "step": 30,
        "buffer": 0,
        "min_notice": 1440,
        "now": NOW,
        "horizon_end": date(2026, 10, 12),
        "earliest_time": None,
        "latest_time": None,
        "selection": "earliest",
        "max_slots": 10,
    }
    args.update(overrides)
    return compute_slots(**args)


def starts(result: SlotResult) -> list[datetime]:
    return [s.start for s in result.slots]


def local_hm(result: SlotResult) -> list[str]:
    return [s.start.astimezone(TZ).strftime("%a %H:%M") for s in result.slots]


# --- the window and the grid --------------------------------------------------


def test_demo_day_offers_every_half_hour_in_hours() -> None:
    result = run()
    assert local_hm(result) == [f"Tue {h:02d}:{m:02d}" for h in range(14, 19) for m in (0, 30)]
    assert result.reason == "ok"
    assert result.more_available is False


def test_single_window_exact_fit() -> None:
    result = run(bookable_hours={"tue": ((time(14, 0), time(15, 0)),)}, duration=60)
    assert starts(result) == [local(TUE, 14)]
    assert result.slots[0].end == local(TUE, 15)


def test_duration_not_fitting_the_window_tail_is_dropped() -> None:
    result = run(bookable_hours={"tue": ((time(14, 0), time(15, 0)),)}, duration=45)
    assert starts(result) == [local(TUE, 14)]


def test_two_windows_in_a_day() -> None:
    hours = {"tue": ((time(9, 0), time(10, 0)), (time(14, 0), time(15, 0)))}
    result = run(bookable_hours=hours)
    assert local_hm(result) == ["Tue 09:00", "Tue 09:30", "Tue 14:00", "Tue 14:30"]


# --- busy time and buffers ------------------------------------------------------


def test_busy_overlap_removes_slots() -> None:
    busy = [Interval(local(TUE, 15), local(TUE, 16))]
    result = run(busy=busy)
    assert "Tue 15:00" not in local_hm(result) and "Tue 15:30" not in local_hm(result)
    assert "Tue 14:30" in local_hm(result)  # ends exactly when the busy time starts
    assert "Tue 16:00" in local_hm(result)  # starts exactly when it ends


def test_buffer_widens_busy_time_on_both_sides() -> None:
    busy = [Interval(local(TUE, 15), local(TUE, 16))]
    result = run(busy=busy, buffer=15)
    hm = local_hm(result)
    assert "Tue 14:30" not in hm  # would end inside the 15-minute lead buffer
    assert "Tue 16:00" not in hm  # would start inside the 15-minute tail buffer
    assert "Tue 14:00" in hm and "Tue 16:30" in hm


def test_busy_intervals_out_of_order_and_overlapping() -> None:
    busy = [
        Interval(local(TUE, 17), local(TUE, 18)),
        Interval(local(TUE, 14), local(TUE, 15)),
        Interval(local(TUE, 14, 30), local(TUE, 15, 30)),
    ]
    assert local_hm(run(busy=busy)) == ["Tue 15:30", "Tue 16:00", "Tue 16:30", "Tue 18:00", "Tue 18:30"]


def test_all_busy_is_fully_booked() -> None:
    result = run(busy=[Interval(local(TUE, 13), local(TUE, 20))])
    assert result.slots == []
    assert result.reason == "fully_booked"
    assert result.more_available is False


# --- notice and horizon ---------------------------------------------------------


def test_min_notice_boundary_equal_is_allowed() -> None:
    first = local(TUE, 14)
    exact = run(now=first - timedelta(minutes=1440))
    assert starts(exact)[0] == first
    late = run(now=first - timedelta(minutes=1440) + timedelta(seconds=1))
    assert starts(late)[0] == local(TUE, 14, 30)


def test_one_day_notice_removes_today_and_tomorrow_morning() -> None:
    # Monday 15:10 local: nothing today, Tuesday from 15:30 on.
    now = local(date(2026, 10, 5), 15, 10)
    result = run(window_start_local=date(2026, 10, 5), window_end_local=TUE, now=now)
    assert local_hm(result)[0] == "Tue 15:30"


def test_everything_inside_notice_is_outside_bookable_hours() -> None:
    result = run(window_start_local=date(2026, 10, 5), window_end_local=date(2026, 10, 5))
    assert result.slots == []
    assert result.reason == "outside_bookable_hours"


def test_horizon_clamps_the_window_end() -> None:
    result = run(window_end_local=date(2026, 10, 30), horizon_end=WED, selection="earliest", max_slots=10)
    days = {s.start.astimezone(TZ).date() for s in run(window_end_local=date(2026, 10, 30), horizon_end=WED).slots}
    assert days <= {TUE, WED}
    full = run(window_end_local=date(2026, 10, 30), horizon_end=WED, max_slots=10, selection="spread")
    assert max(s.start.astimezone(TZ).date() for s in full.slots) == WED
    assert result.more_available is True


def test_horizon_last_day_is_included() -> None:
    result = run(window_start_local=WED, window_end_local=WED, horizon_end=WED)
    assert result.reason == "ok" and len(result.slots) == 10


def test_range_entirely_past_the_horizon() -> None:
    result = run(window_start_local=date(2026, 10, 13), window_end_local=date(2026, 10, 14), horizon_end=date(2026, 10, 12))
    assert result.slots == []
    assert result.reason == "beyond_booking_horizon"
    assert result.searched is None


# --- closed days -----------------------------------------------------------------


def test_sunday_is_closed() -> None:
    result = run(window_start_local=SUN, window_end_local=SUN)
    assert result.slots == []
    assert result.reason == "outside_bookable_hours"
    assert result.searched is None


def test_a_range_across_sunday_skips_it() -> None:
    result = run(window_start_local=date(2026, 10, 10), window_end_local=date(2026, 10, 12), max_slots=30)
    days = {s.start.astimezone(TZ).strftime("%a") for s in result.slots}
    assert days == {"Sat", "Mon"}


# --- time filter ----------------------------------------------------------------


def test_time_filter_narrows() -> None:
    result = run(earliest_time=time(15, 0), latest_time=time(16, 0))
    assert local_hm(result) == ["Tue 15:00", "Tue 15:30"]


def test_time_filter_never_widens() -> None:
    assert local_hm(run(earliest_time=time(8, 0), latest_time=time(23, 0))) == local_hm(run())


def test_earliest_time_off_grid_does_not_shift_the_grid() -> None:
    hours = {"tue": ((time(13, 0), time(15, 0)),)}
    result = run(bookable_hours=hours, earliest_time=time(13, 7))
    assert local_hm(result)[0] == "Tue 13:30"


def test_time_filter_outside_hours() -> None:
    result = run(earliest_time=time(19, 30))
    assert result.slots == [] and result.reason == "outside_bookable_hours"


def test_latest_before_earliest_finds_nothing() -> None:
    result = run(earliest_time=time(17, 0), latest_time=time(15, 0))
    assert result.slots == [] and result.reason == "outside_bookable_hours"


# --- selection ----------------------------------------------------------------------


def test_earliest_returns_the_first_n() -> None:
    result = run(window_end_local=THU, selection="earliest", max_slots=3)
    assert local_hm(result) == ["Tue 14:00", "Tue 14:30", "Tue 15:00"]
    assert result.more_available is True


def test_spread_round_robins_across_three_days_then_time_orders() -> None:
    result = run(window_end_local=THU, selection="spread", max_slots=5)
    assert local_hm(result) == ["Tue 14:00", "Tue 14:30", "Wed 14:00", "Wed 14:30", "Thu 14:00"]
    assert result.more_available is True


def test_spread_skips_days_with_fewer_slots() -> None:
    busy = [Interval(local(WED, 14), local(WED, 18, 30))]  # Wednesday: only 18:30 free
    result = run(window_end_local=THU, selection="spread", max_slots=5, busy=busy)
    assert local_hm(result) == ["Tue 14:00", "Tue 14:30", "Wed 18:30", "Thu 14:00", "Thu 14:30"]


def test_more_available_false_when_everything_fits() -> None:
    result = run(max_slots=10)
    assert len(result.slots) == 10 and result.more_available is False
    result = run(max_slots=9)
    assert len(result.slots) == 9 and result.more_available is True


# --- DST (P16) -----------------------------------------------------------------------

DST_NOW = utc(2026, 10, 1, 12)


def dst(day: date, start: time, end: time) -> SlotResult:
    return compute_slots(
        window_start_local=day,
        window_end_local=day,
        bookable_hours={"sun": ((start, end),)},
        tz=TZ,
        busy=[],
        duration=30,
        step=30,
        buffer=0,
        min_notice=0,
        now=DST_NOW,
        horizon_end=date(2027, 12, 31),
        earliest_time=None,
        latest_time=None,
        selection="earliest",
        max_slots=10,
    )


def test_spring_forward_skips_the_missing_hour() -> None:
    result = dst(date(2027, 3, 14), time(0, 0), time(4, 0))
    assert starts(result) == [utc(2027, 3, 14, h, m) for h, m in ((5, 0), (5, 30), (6, 0), (6, 30), (7, 0), (7, 30))]
    assert [s.start.astimezone(TZ).strftime("%H:%M") for s in result.slots] == [
        "00:00", "00:30", "01:00", "01:30", "03:00", "03:30",
    ]


def test_spring_forward_nonexistent_boundary_moves_forward() -> None:
    result = dst(date(2027, 3, 14), time(2, 30), time(4, 0))
    assert starts(result) == [utc(2027, 3, 14, 7, 0), utc(2027, 3, 14, 7, 30)]


def test_fall_back_drops_the_repeated_hour() -> None:
    result = dst(date(2026, 11, 1), time(0, 0), time(4, 0))
    assert starts(result) == [
        utc(2026, 11, 1, h, m) for h, m in ((4, 0), (4, 30), (5, 0), (5, 30), (7, 0), (7, 30), (8, 0), (8, 30))
    ]


def test_fall_back_ambiguous_boundary_uses_the_first_occurrence() -> None:
    result = dst(date(2026, 11, 1), time(1, 30), time(2, 0))
    assert starts(result) == [utc(2026, 11, 1, 5, 30)]


def test_no_two_offered_slots_share_a_spoken_time() -> None:
    result = dst(date(2026, 11, 1), time(0, 0), time(4, 0))
    walls = [s.start.astimezone(TZ).strftime("%H:%M") for s in result.slots]
    assert len(walls) == len(set(walls))


def test_window_entirely_inside_the_gap_is_empty() -> None:
    result = dst(date(2027, 3, 14), time(2, 0), time(2, 30))
    assert result.slots == [] and result.reason == "outside_bookable_hours"


def test_resolve_local() -> None:
    assert resolve_local(date(2027, 3, 14), time(2, 30), TZ) == utc(2027, 3, 14, 7, 0)
    assert resolve_local(date(2026, 11, 1), time(1, 30), TZ) == utc(2026, 11, 1, 5, 30)
    assert resolve_local(TUE, time(14, 0), TZ) == utc(2026, 10, 6, 18, 0)
    assert resolve_local(TUE, time(14, 0), TZ).utcoffset() == timedelta(0)


# --- shape -----------------------------------------------------------------------------


def test_intervals_are_utc_and_five_minute_aligned() -> None:
    hours = {d: ((time(9, 5), time(17, 55)),) for d in ("mon", "tue", "wed")}
    result = run(bookable_hours=hours, step=25, duration=35, window_end_local=WED, max_slots=10, selection="spread")
    assert result.slots
    for slot in result.slots + dst(date(2026, 11, 1), time(0, 0), time(4, 0)).slots:
        for t in (slot.start, slot.end):
            assert t.utcoffset() == timedelta(0)
            assert t.minute % 5 == 0 and t.second == 0 and t.microsecond == 0


def test_slots_are_returned_in_time_order() -> None:
    result = run(window_end_local=THU, selection="spread", max_slots=10)
    assert starts(result) == sorted(starts(result))


def test_searched_spans_the_bookable_windows() -> None:
    result = run(window_start_local=TUE, window_end_local=THU)
    assert result.searched == Interval(local(TUE, 14), local(THU, 19))


def test_searched_ignores_closed_days_at_the_edges() -> None:
    result = run(window_start_local=SUN, window_end_local=date(2026, 10, 12))
    assert result.searched == Interval(local(date(2026, 10, 12), 14), local(date(2026, 10, 12), 19))


@pytest.mark.parametrize("bad_now", [datetime(2026, 10, 5, 12, 0), datetime(2026, 10, 5, 8, 0, tzinfo=TZ)])
def test_now_must_be_utc(bad_now) -> None:
    with pytest.raises(ValueError):
        run(now=bad_now)


@pytest.mark.parametrize("selection", ["random", ""])
def test_unknown_selection_is_refused(selection) -> None:
    with pytest.raises(ValueError):
        run(selection=selection)
