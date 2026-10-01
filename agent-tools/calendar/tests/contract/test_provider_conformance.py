"""Provider conformance suite (spec section 6.5).

Parametrized over every adapter through a small harness: `provider`, three
calendar references (`cal`, `alias_cal` naming the same physical calendar,
`other_cal` naming a different one), and vendor-specific hooks to plant an
external event, cancel an event and inject one failure of a given class.
UC08b appends "google" (a respx-mocked Google API) to PROVIDERS.

A new adapter is done when this suite passes, not when its own tests pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from calendar_tools.core.ports import (
    BookingMeta,
    BookingRecord,
    CalendarProvider,
    CalendarRef,
    Interval,
    NewEvent,
    ProviderAuthError,
    ProviderConfigError,
    ProviderTimeout,
    ProviderUnavailable,
)
from calendar_tools.providers.fake import FakeCalendarProvider

PROVIDERS = ["fake"]

READ_METHODS = frozenset({"resolve_calendar_identity", "get_busy", "find_bookings", "get_event"})
WRITE_METHODS = frozenset({"create_event", "delete_event"})

T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
WINDOW_START = T0 - timedelta(hours=4)
WINDOW_END = T0 + timedelta(hours=8)


class FakeHarness:
    def __init__(self) -> None:
        self.provider = FakeCalendarProvider()
        self.provider.canonical_ids.update({"primary": "cal-0001", "cal-0001": "cal-0001"})
        self.cal = CalendarRef("fake", "primary", "cal-binding-test-alpha-fake")
        self.alias_cal = CalendarRef("fake", "cal-0001", "cal-binding-test-alpha-fake")
        self.other_cal = CalendarRef("fake", "cal-0002", "cal-binding-test-alpha-fake")

    async def add_external(self, cal, start, end, *, status="confirmed", transparency="opaque", all_day=False):
        return self.provider.add_external_event(
            cal, start, end, status=status, transparency=transparency, all_day=all_day
        )

    async def cancel(self, cal, event_id):
        self.provider.cancel_event(cal, event_id)

    def inject(self, method: str, failure: str) -> None:
        exc = {
            "unavailable": ProviderUnavailable(reason="http_503"),
            "auth": ProviderAuthError(reason="http_401"),
            "config": ProviderConfigError(reason="calendar_not_found"),
            # A write whose request may have reached the vendor is maybe-committed.
            "timeout": ProviderTimeout(maybe_committed=method in WRITE_METHODS),
        }[failure]
        self.provider.fail_next(method, exc)


HARNESSES = {"fake": FakeHarness}


@pytest.fixture(params=PROVIDERS)
def provider_under_test(request):
    return HARNESSES[request.param]()


def _meta(binding_id="test-alpha", ref="B7K2Q9", fingerprint="fp-1") -> BookingMeta:
    return BookingMeta(
        service_tag="cal-tools-v1",
        binding_id=binding_id,
        fingerprint=fingerprint,
        contact_tag="ct-1",
        booking_ref=ref,
    )


def _new_event(start=T0, minutes=30, *, meta=None, request_key="rk-1") -> NewEvent:
    return NewEvent(
        start=start,
        end=start + timedelta(minutes=minutes),
        timezone="America/Toronto",
        title="Phone call: Jordan Example",
        description="Booked by an AI agent.",
        meta=meta or _meta(),
        request_key=request_key,
    )


def _covers(busy: list[Interval], start: datetime, end: datetime) -> bool:
    return any(i.start <= start and i.end >= end for i in busy)


def _overlaps(busy: list[Interval], start: datetime, end: datetime) -> bool:
    return any(i.start < end and i.end > start for i in busy)


def _assert_utc(dt: datetime) -> None:
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(0)


async def test_is_a_calendar_provider(provider_under_test):
    assert isinstance(provider_under_test.provider, CalendarProvider)
    assert isinstance(provider_under_test.provider.name, str)


async def test_resolve_identity_maps_two_spellings_to_one(provider_under_test):
    h = provider_under_test
    a = await h.provider.resolve_calendar_identity(h.cal)
    b = await h.provider.resolve_calendar_identity(h.alias_cal)
    c = await h.provider.resolve_calendar_identity(h.other_cal)
    assert a == b
    assert a != c


async def test_busy_includes_service_created_events(provider_under_test):
    h = provider_under_test
    await h.provider.create_event(h.cal, _new_event())
    busy = await h.provider.get_busy(h.cal, WINDOW_START, WINDOW_END)
    assert _covers(busy, T0, T0 + timedelta(minutes=30))


async def test_busy_includes_external_events(provider_under_test):
    h = provider_under_test
    await h.add_external(h.cal, T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    busy = await h.provider.get_busy(h.cal, WINDOW_START, WINDOW_END)
    assert _covers(busy, T0 + timedelta(hours=1), T0 + timedelta(hours=2))


async def test_cancelled_and_transparent_do_not_block(provider_under_test):
    h = provider_under_test
    cancelled = await h.add_external(h.cal, T0, T0 + timedelta(hours=1))
    await h.cancel(h.cal, cancelled)
    await h.add_external(h.cal, T0 + timedelta(hours=2), T0 + timedelta(hours=3), transparency="transparent")
    await h.add_external(h.cal, T0 + timedelta(hours=4), T0 + timedelta(hours=5), status="cancelled")
    busy = await h.provider.get_busy(h.cal, WINDOW_START, WINDOW_END)
    assert not _overlaps(busy, T0, T0 + timedelta(hours=6))


async def test_busy_is_per_calendar_and_shared_across_spellings(provider_under_test):
    h = provider_under_test
    await h.add_external(h.cal, T0, T0 + timedelta(hours=1))
    assert _overlaps(await h.provider.get_busy(h.alias_cal, WINDOW_START, WINDOW_END), T0, T0 + timedelta(hours=1))
    assert not _overlaps(await h.provider.get_busy(h.other_cal, WINDOW_START, WINDOW_END), T0, T0 + timedelta(hours=1))


async def test_busy_window_is_half_open(provider_under_test):
    h = provider_under_test
    await h.add_external(h.cal, T0 - timedelta(hours=1), T0)
    busy = await h.provider.get_busy(h.cal, T0, T0 + timedelta(hours=1))
    assert not _overlaps(busy, T0, T0 + timedelta(hours=1))


async def test_metadata_round_trip(provider_under_test):
    h = provider_under_test
    event = _new_event()
    created = await h.provider.create_event(h.cal, event)
    assert isinstance(created, BookingRecord)
    assert created.meta == event.meta
    assert (created.start, created.end) == (event.start, event.end)

    found = await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END)
    assert [r.event_id for r in found] == [created.event_id]
    assert found[0].meta == event.meta
    assert (found[0].start, found[0].end) == (event.start, event.end)

    got = await h.provider.get_event(h.cal, created.event_id)
    assert got is not None
    assert got.meta == event.meta
    assert got.event_id == created.event_id


async def test_find_bookings_ignores_external_events(provider_under_test):
    h = provider_under_test
    await h.add_external(h.cal, T0, T0 + timedelta(hours=1))
    assert await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END) == []


async def test_find_bookings_binding_filter(provider_under_test):
    h = provider_under_test
    a = await h.provider.create_event(h.cal, _new_event(meta=_meta("test-alpha", "AAAAAA"), request_key="rk-a"))
    b = await h.provider.create_event(
        h.cal, _new_event(T0 + timedelta(hours=1), meta=_meta("test-beta", "BBBBBB"), request_key="rk-b")
    )
    all_ids = {r.event_id for r in await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END)}
    alpha = await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END, binding_id="test-alpha")
    beta = await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END, binding_id="test-beta")
    assert all_ids == {a.event_id, b.event_id}
    assert [r.event_id for r in alpha] == [a.event_id]
    assert [r.event_id for r in beta] == [b.event_id]


async def test_find_bookings_overlap_window(provider_under_test):
    h = provider_under_test
    inside = await h.provider.create_event(h.cal, _new_event(T0, request_key="rk-in"))
    await h.provider.create_event(h.cal, _new_event(T0 + timedelta(days=2), request_key="rk-out"))
    found = await h.provider.find_bookings(h.cal, T0 + timedelta(minutes=10), T0 + timedelta(minutes=20))
    assert [r.event_id for r in found] == [inside.event_id]


@pytest.mark.parametrize("binding_id", [None, "test-alpha"])
async def test_find_bookings_never_returns_cancelled_or_deleted(provider_under_test, binding_id):
    h = provider_under_test
    deleted = await h.provider.create_event(h.cal, _new_event(T0, request_key="rk-d"))
    cancelled = await h.provider.create_event(h.cal, _new_event(T0 + timedelta(hours=1), request_key="rk-c"))
    kept = await h.provider.create_event(h.cal, _new_event(T0 + timedelta(hours=2), request_key="rk-k"))
    await h.provider.delete_event(h.cal, deleted.event_id)
    await h.cancel(h.cal, cancelled.event_id)
    found = await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END, binding_id=binding_id)
    assert [r.event_id for r in found] == [kept.event_id]


async def test_get_event_none_after_delete_or_cancel(provider_under_test):
    h = provider_under_test
    created = await h.provider.create_event(h.cal, _new_event(request_key="rk-1"))
    other = await h.provider.create_event(h.cal, _new_event(T0 + timedelta(hours=1), request_key="rk-2"))
    await h.provider.delete_event(h.cal, created.event_id)
    await h.cancel(h.cal, other.event_id)
    assert await h.provider.get_event(h.cal, created.event_id) is None
    assert await h.provider.get_event(h.cal, other.event_id) is None


async def test_get_event_unknown_and_external_are_none(provider_under_test):
    h = provider_under_test
    external = await h.add_external(h.cal, T0, T0 + timedelta(hours=1))
    assert await h.provider.get_event(h.cal, "no-such-event") is None
    assert await h.provider.get_event(h.cal, external) is None


async def test_delete_twice_is_success(provider_under_test):
    h = provider_under_test
    created = await h.provider.create_event(h.cal, _new_event())
    await h.provider.delete_event(h.cal, created.event_id)
    await h.provider.delete_event(h.cal, created.event_id)
    await h.provider.delete_event(h.cal, "never-existed")


async def test_deleted_event_no_longer_blocks(provider_under_test):
    h = provider_under_test
    created = await h.provider.create_event(h.cal, _new_event())
    await h.provider.delete_event(h.cal, created.event_id)
    busy = await h.provider.get_busy(h.cal, WINDOW_START, WINDOW_END)
    assert not _overlaps(busy, T0, T0 + timedelta(minutes=30))


async def test_exact_retry_dedupe(provider_under_test):
    h = provider_under_test
    first = await h.provider.create_event(h.cal, _new_event(request_key="rk-same"))
    second = await h.provider.create_event(h.cal, _new_event(request_key="rk-same"))
    assert second.event_id == first.event_id
    found = await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END)
    assert len(found) == 1


@pytest.mark.parametrize("removal", ["delete", "cancel"])
async def test_retry_after_original_removed_creates_a_new_active_event(provider_under_test, removal):
    # Spec 8.2 / 13.1 "409-on-ID": once the original event is gone, the same
    # request_key must yield a new, active booking, never the removed one.
    h = provider_under_test
    first = await h.provider.create_event(h.cal, _new_event(request_key="rk-again"))
    if removal == "delete":
        await h.provider.delete_event(h.cal, first.event_id)
    else:
        await h.cancel(h.cal, first.event_id)
    second = await h.provider.create_event(h.cal, _new_event(request_key="rk-again"))
    assert second.event_id != first.event_id
    found = await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END)
    assert [r.event_id for r in found] == [second.event_id]
    assert await h.provider.get_event(h.cal, second.event_id) is not None
    busy = await h.provider.get_busy(h.cal, WINDOW_START, WINDOW_END)
    assert _covers(busy, T0, T0 + timedelta(minutes=30))


async def test_distinct_request_keys_create_distinct_events(provider_under_test):
    h = provider_under_test
    a = await h.provider.create_event(h.cal, _new_event(request_key="rk-a"))
    b = await h.provider.create_event(h.cal, _new_event(request_key="rk-b"))
    assert a.event_id != b.event_id


FAILURE_EXPECTATIONS = {
    "unavailable": ProviderUnavailable,
    "auth": ProviderAuthError,
    "config": ProviderConfigError,
    "timeout": ProviderTimeout,
}


def _call(h, method: str):
    p = h.provider
    return {
        "resolve_calendar_identity": lambda: p.resolve_calendar_identity(h.cal),
        "get_busy": lambda: p.get_busy(h.cal, WINDOW_START, WINDOW_END),
        "find_bookings": lambda: p.find_bookings(h.cal, WINDOW_START, WINDOW_END),
        "get_event": lambda: p.get_event(h.cal, "evt-x"),
        "create_event": lambda: p.create_event(h.cal, _new_event(request_key="rk-fail")),
        "delete_event": lambda: p.delete_event(h.cal, "evt-x"),
    }[method]()


@pytest.mark.parametrize(
    "method",
    ["resolve_calendar_identity", "get_busy", "find_bookings", "get_event", "create_event", "delete_event"],
)
@pytest.mark.parametrize("failure", list(FAILURE_EXPECTATIONS))
async def test_error_mapping(provider_under_test, method, failure):
    h = provider_under_test
    h.inject(method, failure)
    with pytest.raises(FAILURE_EXPECTATIONS[failure]) as info:
        await _call(h, method)
    if failure == "timeout":
        # Spec 6.2: maybe_committed=True only for a write whose request may have
        # reached the vendor. Reads are never maybe-committed; a create timeout
        # always is (the core's uncertain path depends on it); a delete may be either.
        if method == "create_event":
            assert info.value.maybe_committed is True
        elif method in READ_METHODS:
            assert info.value.maybe_committed is False
        else:
            assert isinstance(info.value.maybe_committed, bool)
    # The failure is one-shot: the next call succeeds.
    await _call(h, method)


@pytest.mark.parametrize(
    "bad",
    [datetime(2026, 10, 5, 14, 0), datetime(2026, 10, 5, 10, 0, tzinfo=timezone(timedelta(hours=-4)))],
    ids=["naive", "non-utc"],
)
def test_non_utc_input_rejected_at_the_dataclass(bad):
    with pytest.raises(ValueError):
        _new_event(start=bad)


async def test_outputs_are_utc(provider_under_test):
    h = provider_under_test
    await h.provider.create_event(h.cal, _new_event())
    await h.add_external(h.cal, T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    for interval in await h.provider.get_busy(h.cal, WINDOW_START, WINDOW_END):
        _assert_utc(interval.start)
        _assert_utc(interval.end)
    for record in await h.provider.find_bookings(h.cal, WINDOW_START, WINDOW_END):
        _assert_utc(record.start)
        _assert_utc(record.end)
