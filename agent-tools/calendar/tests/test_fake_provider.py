"""FakeCalendarProvider's own test knobs (beyond the conformance suite)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from calendar_tools.core.ports import BookingMeta, CalendarRef, NewEvent, ProviderUnavailable
from calendar_tools.providers.fake import FakeCalendarProvider

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
CAL = CalendarRef("fake", "primary", "cal-binding-test-alpha-fake")
META = BookingMeta("cal-tools-v1", "test-alpha", "fp", "ct", "B7K2Q9")


def _event(key="rk") -> NewEvent:
    return NewEvent(T0, T0 + timedelta(minutes=30), "UTC", "t", "d", META, key)


def test_default_and_custom_name():
    assert FakeCalendarProvider().name == "fake"
    assert FakeCalendarProvider(name="fake_b").name == "fake_b"


async def test_created_visible_after_hides_new_events_from_find_bookings():
    p = FakeCalendarProvider()
    p.created_visible_after = 2
    created = await p.create_event(CAL, _event())
    assert await p.find_bookings(CAL, T0, T0 + timedelta(hours=1)) == []
    assert await p.find_bookings(CAL, T0, T0 + timedelta(hours=1)) == []
    found = await p.find_bookings(CAL, T0, T0 + timedelta(hours=1))
    assert [r.event_id for r in found] == [created.event_id]


async def test_created_visible_after_does_not_hide_busy_time():
    p = FakeCalendarProvider()
    p.created_visible_after = 5
    await p.create_event(CAL, _event())
    assert await p.get_busy(CAL, T0, T0 + timedelta(hours=1))


async def test_fail_next_is_one_shot_and_per_method():
    p = FakeCalendarProvider()
    p.fail_next("get_busy", ProviderUnavailable())
    assert await p.find_bookings(CAL, T0, T0 + timedelta(hours=1)) == []
    with pytest.raises(ProviderUnavailable):
        await p.get_busy(CAL, T0, T0 + timedelta(hours=1))
    assert await p.get_busy(CAL, T0, T0 + timedelta(hours=1)) == []


async def test_fail_next_queues_in_order():
    p = FakeCalendarProvider()
    first, second = ProviderUnavailable(reason="a"), ProviderUnavailable(reason="b")
    p.fail_next("get_event", first)
    p.fail_next("get_event", second)
    with pytest.raises(ProviderUnavailable) as one:
        await p.get_event(CAL, "x")
    with pytest.raises(ProviderUnavailable) as two:
        await p.get_event(CAL, "x")
    assert (one.value.reason, two.value.reason) == ("a", "b")


def test_fail_next_rejects_unknown_method_and_non_core_exception():
    p = FakeCalendarProvider()
    with pytest.raises(ValueError):
        p.fail_next("get_everything", ProviderUnavailable())
    with pytest.raises(TypeError):
        p.fail_next("get_busy", RuntimeError("vendor"))  # type: ignore[arg-type]


async def test_failing_create_creates_nothing():
    p = FakeCalendarProvider()
    p.fail_next("create_event", ProviderUnavailable())
    with pytest.raises(ProviderUnavailable):
        await p.create_event(CAL, _event())
    assert await p.find_bookings(CAL, T0, T0 + timedelta(hours=1)) == []


async def test_calls_are_recorded():
    p = FakeCalendarProvider()
    await p.get_busy(CAL, T0, T0 + timedelta(hours=1))
    await p.create_event(CAL, _event())
    assert p.calls == ["get_busy", "create_event"]


async def test_all_day_and_tentative_events_block_by_default():
    p = FakeCalendarProvider()
    day = datetime(2026, 10, 6, tzinfo=UTC)
    p.add_external_event(CAL, day, day + timedelta(days=1), all_day=True)
    p.add_external_event(CAL, T0, T0 + timedelta(hours=1), status="tentative")
    busy = await p.get_busy(CAL, T0, day + timedelta(days=1))
    assert len(busy) == 2


def test_add_external_event_validates_inputs():
    p = FakeCalendarProvider()
    with pytest.raises(ValueError):
        p.add_external_event(CAL, T0, T0 + timedelta(hours=1), status="maybe")
    with pytest.raises(ValueError):
        p.add_external_event(CAL, T0, T0 + timedelta(hours=1), transparency="foggy")
    with pytest.raises(ValueError):
        p.add_external_event(CAL, T0 + timedelta(hours=1), T0)


async def test_get_busy_rejects_non_utc_bounds():
    p = FakeCalendarProvider()
    with pytest.raises(ValueError):
        await p.get_busy(CAL, datetime(2026, 10, 5), T0)


async def test_dedupe_does_not_resurrect_a_deleted_event():
    p = FakeCalendarProvider()
    first = await p.create_event(CAL, _event("rk-1"))
    await p.delete_event(CAL, first.event_id)
    second = await p.create_event(CAL, _event("rk-1"))
    assert second.event_id != first.event_id
