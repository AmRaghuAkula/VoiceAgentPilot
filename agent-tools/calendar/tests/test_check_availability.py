"""The `check_availability` use case (spec 5.2, 7.1, 7.5, 10, F2-F5; plan UC03,
P18 and the rev 1.6 reason-code rule).

The binding carries the demo binding's shape as configuration only, with
fictional values: America/Toronto, 14:00-19:00 Monday to Saturday (no Sunday),
30-minute slots, one day (1440 min) of minimum notice, a 7-day horizon. The
fake clock starts on Monday 2026-10-05 at 08:00 Toronto.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from calendar_tools.core import availability, tokens
from calendar_tools.core.availability import check_availability, make_check_availability
from calendar_tools.core.bindings import Binding, load_bindings
from calendar_tools.core.deadline import Deadline
from calendar_tools.core.ports import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    SecretInvalid,
    SecretStoreUnavailable,
)
from calendar_tools.core.tokens import VerifiedSlot
from calendar_tools.http.app import RequestContext
from calendar_tools.providers.fake import FakeCalendarProvider
from tests.fakes import jwt_tokens as jt
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import SLOT_KEY_1, SLOT_KEY_2, FakeKeyRing, make_keys

TZ = ZoneInfo("America/Toronto")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat")


def binding_doc(**changes: Any) -> dict[str, Any]:
    b: dict[str, Any] = {
        "enabled": True,
        "provider": "fake",
        "calendar_id": "primary",
        "credential_secret_name": "cal-binding-test-alpha-fake",
        "timezone": "America/Toronto",
        "locale": "en-CA",
        "bookable_hours": {d: [["14:00", "19:00"]] for d in DAYS},
        "default_duration_minutes": 30,
        "allowed_durations_minutes": [30, 45],
        "slot_step_minutes": 30,
        "buffer_minutes": 0,
        "min_notice_minutes": 1440,
        "max_days_ahead": 7,
        "default_search_days": 3,
        "max_slots_returned": 6,
        "slot_selection": "spread",
        "slot_token_ttl_minutes": 30,
        "appointment_types": [
            {"id": "phone_call", "label": "Phone call"},
            {"id": "in_person", "label": "In-person meeting"},
        ],
        "required_contact_fields": ["name", "phone"],
        "default_phone_region": "CA",
        "accept_notes": False,
        "max_active_bookings_per_contact": 2,
        "event_title_template": "{appointment_type_label}: {contact_name}",
        "event_description_template": "Ref: {booking_ref}",
        "allowed_principals": [jt.PRINCIPAL_A],
    }
    b.update(changes)
    return b


def make_binding(**changes: Any) -> Binding:
    doc = {"bindings": {"test-alpha": binding_doc(**changes)}}
    return load_bindings(json.dumps(doc), frozenset({"fake"}))["test-alpha"]


class World:
    def __init__(self, **binding_changes: Any) -> None:
        self.clock = FakeClock()
        self.provider = FakeCalendarProvider()
        self.keyring = FakeKeyRing()
        self.binding = make_binding(**binding_changes)
        self.ctx: RequestContext | None = None

    def new_ctx(self) -> RequestContext:
        self.ctx = RequestContext(
            request_id="0" * 31 + "1",
            principal=jt.PRINCIPAL_A,
            deadline=Deadline(self.clock),
            binding_id=self.binding.binding_id,
        )
        return self.ctx

    async def check(self, body: dict[str, Any] | None = None, ctx: RequestContext | None = None) -> dict[str, Any]:
        ctx = ctx or self.new_ctx()
        return await check_availability(
            self.binding, body or {}, ctx, provider=self.provider, keyring=self.keyring, clock=self.clock
        )

    def busy(self, start: datetime, end: datetime) -> None:
        self.provider.add_external_event(self.binding.calendar_ref, start, end)


def wall(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(UTC)


@pytest.fixture
def world() -> World:
    return World()


def slot_days(resp: dict[str, Any]) -> list[str]:
    return [s["start"][:10] for s in resp["slots"]]


# --- defaults and clamping ---------------------------------------------------------


async def test_no_body_searches_from_today_for_default_search_days(world) -> None:
    resp = await world.check()
    assert resp["status"] == "available"
    # Monday 08:00 + one day of notice: Tuesday 08:00. Default 3 days: Mon..Wed.
    assert set(slot_days(resp)) == {"2026-10-06", "2026-10-07"}
    assert resp["searched"] == {"from": "2026-10-05T14:00:00-04:00", "to": "2026-10-07T19:00:00-04:00"}
    assert world.ctx.diagnostic is None or world.ctx.diagnostic == "ok"


async def test_from_date_in_the_past_is_clamped_to_today(world) -> None:
    resp = await world.check({"from_date": "2026-09-01", "to_date": "2026-10-06"})
    assert resp["status"] == "available"
    assert resp["searched"]["from"] == "2026-10-05T14:00:00-04:00"


async def test_to_date_clamped_to_the_horizon(world) -> None:
    resp = await world.check({"from_date": "2026-10-10", "to_date": "2026-12-31"})
    assert resp["status"] == "available"
    assert resp["searched"]["to"] == "2026-10-12T19:00:00-04:00"  # today + 7 days
    assert max(slot_days(resp)) <= "2026-10-12"


async def test_default_to_date_counts_from_the_requested_from_date(world) -> None:
    resp = await world.check({"from_date": "2026-10-07"})
    assert resp["searched"] == {"from": "2026-10-07T14:00:00-04:00", "to": "2026-10-09T19:00:00-04:00"}


async def test_null_fields_are_treated_as_absent(world) -> None:
    resp = await world.check({"from_date": None, "to_date": None, "earliest_time": None, "duration_minutes": None})
    assert resp["status"] == "available"
    assert resp["duration_minutes"] == 30


# --- invalid requests ----------------------------------------------------------------


async def test_to_date_before_from_date(world) -> None:
    resp = await world.check({"from_date": "2026-10-08", "to_date": "2026-10-07"})
    assert resp == {"status": "invalid_request", "detail": {"fields": ["to_date"]}}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("invalid_request", "to_date")
    assert world.provider.calls == [] and world.keyring.load_calls == 0


async def test_range_entirely_in_the_past_is_to_date_before_from_date(world) -> None:
    resp = await world.check({"from_date": "2026-09-01", "to_date": "2026-09-03"})
    assert resp["detail"]["fields"] == ["to_date"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_date", "2026-13-01"),
        ("from_date", "2026-10-5"),
        ("from_date", "20261005"),
        ("from_date", "2026-02-30"),
        ("from_date", 20261005),
        ("to_date", "next tuesday"),
        ("to_date", ["2026-10-06"]),
        ("earliest_time", "3pm"),
        ("earliest_time", "24:00"),
        ("earliest_time", "9:00"),
        ("latest_time", "17:60"),
        ("latest_time", 17),
        ("from_date", "２０２６-10-05"),
    ],
)
async def test_bad_date_or_time_names_the_field(world, field, value) -> None:
    resp = await world.check({field: value})
    assert resp == {"status": "invalid_request", "detail": {"fields": [field]}}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("invalid_request", field)


@pytest.mark.parametrize("value", [60, 0, -30, "30", 30.5, 60.0, True, [30], float("inf")])
async def test_duration_not_allowed(world, value) -> None:
    resp = await world.check({"duration_minutes": value})
    assert resp == {
        "status": "invalid_request",
        "detail": {"fields": ["duration_minutes"], "allowed_durations_minutes": [30, 45]},
    }
    assert (world.ctx.diagnostic, world.ctx.reason) == ("invalid_request", "duration_minutes")


async def test_several_bad_fields_are_all_named_in_schema_order(world) -> None:
    resp = await world.check({"duration_minutes": 7, "latest_time": "x", "from_date": "y"})
    assert resp["detail"]["fields"] == ["from_date", "latest_time", "duration_minutes"]
    assert world.ctx.reason == "from_date,latest_time,duration_minutes"


async def test_invalid_request_never_echoes_values(world) -> None:
    resp = await world.check({"from_date": "SENTINEL-VALUE-9"})
    assert "SENTINEL" not in json.dumps(resp)
    assert "SENTINEL" not in (world.ctx.reason or "")


# --- response shape ---------------------------------------------------------------------


async def test_now_today_and_offsets_in_the_binding_zone(world) -> None:
    resp = await world.check()
    assert resp["timezone"] == "America/Toronto"
    assert resp["now"] == "2026-10-05T08:00:00-04:00"
    assert resp["today"] == {"date": "2026-10-05", "weekday": "Monday"}
    for slot in resp["slots"]:
        assert slot["start"].endswith("-04:00") and slot["end"].endswith("-04:00")


async def test_today_follows_the_zone_not_utc(world) -> None:
    world.clock.set(datetime(2026, 10, 6, 2, 0, tzinfo=UTC))  # Monday 22:00 in Toronto
    resp = await world.check()
    assert resp["today"] == {"date": "2026-10-05", "weekday": "Monday"}


async def test_slots_carry_display_tokens_and_iso_times(world) -> None:
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06"})
    first = resp["slots"][0]
    assert first["start"] == "2026-10-06T14:00:00-04:00"
    assert first["end"] == "2026-10-06T14:30:00-04:00"
    assert first["display"] == "Tuesday, October 6 at 2:00 PM"
    verified = tokens.verify(make_keys(), "test-alpha", first["slot_id"], datetime.fromisoformat(first["start"]))
    assert isinstance(verified, VerifiedSlot)
    assert verified.duration_minutes == 30
    assert verified.expiry == world.clock.now() + timedelta(minutes=30)
    assert resp["duration_minutes"] == 30
    assert len(resp["slots"]) == 6 and resp["more_available"] is True


async def test_requested_duration_is_used_and_in_the_token(world) -> None:
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06", "duration_minutes": 45})
    assert resp["duration_minutes"] == 45
    first = resp["slots"][0]
    assert first["end"] == "2026-10-06T14:45:00-04:00"
    verified = tokens.verify(make_keys(), "test-alpha", first["slot_id"], datetime.fromisoformat(first["start"]))
    assert verified.duration_minutes == 45


async def test_appointment_types_and_durations_echoed(world) -> None:
    resp = await world.check()
    assert resp["appointment_types"] == [
        {"id": "phone_call", "label": "Phone call"},
        {"id": "in_person", "label": "In-person meeting"},
    ]
    assert resp["allowed_durations_minutes"] == [30, 45]


async def test_time_filter_applies(world) -> None:
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06", "earliest_time": "15:07", "latest_time": "17:00"})
    assert [s["start"][11:16] for s in resp["slots"]] == ["15:30", "16:00", "16:30"]


async def test_busy_time_from_the_provider_removes_slots(world) -> None:
    world.busy(wall(date(2026, 10, 6), 14), wall(date(2026, 10, 6), 16))
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06"})
    assert [s["start"][11:16] for s in resp["slots"]][0] == "16:00"
    assert world.provider.calls == ["get_busy"]


# --- no availability -------------------------------------------------------------------------


async def test_fully_booked(world) -> None:
    world.busy(wall(date(2026, 10, 6), 13), wall(date(2026, 10, 6), 20))
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06"})
    assert resp["status"] == "no_availability"
    assert resp["slots"] == [] and resp["more_available"] is False
    assert resp["detail"] == {"reason": "fully_booked"}
    assert world.ctx.diagnostic in (None, "ok")


async def test_sunday_is_outside_bookable_hours_without_any_calendar_call(world) -> None:
    resp = await world.check({"from_date": "2026-10-11", "to_date": "2026-10-11"})
    assert resp["status"] == "no_availability"
    assert resp["detail"] == {"reason": "outside_bookable_hours"}
    assert resp["searched"] == {"from": "2026-10-11T00:00:00-04:00", "to": "2026-10-12T00:00:00-04:00"}
    assert world.provider.calls == [] and world.keyring.load_calls == 0


async def test_today_only_is_inside_the_notice(world) -> None:
    resp = await world.check({"from_date": "2026-10-05", "to_date": "2026-10-05"})
    assert resp["status"] == "no_availability"
    assert resp["detail"] == {"reason": "outside_bookable_hours"}


async def test_beyond_the_horizon(world) -> None:
    resp = await world.check({"from_date": "2026-10-13"})
    assert resp["status"] == "no_availability"
    assert resp["detail"] == {"reason": "beyond_booking_horizon"}
    assert "searched" not in resp
    assert world.provider.calls == [] and world.keyring.load_calls == 0


async def test_far_future_dates_do_not_overflow(world) -> None:
    resp = await world.check({"from_date": "9999-12-31"})
    assert resp["detail"] == {"reason": "beyond_booking_horizon"}
    resp = await world.check({"to_date": "9999-12-31"})
    assert resp["status"] == "available"


async def test_horizon_boundary_day_is_offered(world) -> None:
    resp = await world.check({"from_date": "2026-10-12", "to_date": "2026-10-12"})
    assert resp["status"] == "available"
    assert set(slot_days(resp)) == {"2026-10-12"}


# --- couldn't check: provider ----------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "diagnostic", "reason"),
    [
        (ProviderUnavailable(), "provider_unavailable", None),
        (ProviderUnavailable("rate_limited"), "provider_unavailable", "rate_limited"),
        (ProviderAuthError("refresh_rejected"), "credential_rejected", "refresh_rejected"),
        (ProviderConfigError("calendar_not_found"), "provider_config_error", "calendar_not_found"),
        (ProviderTimeout(maybe_committed=False), "provider_timeout", None),
    ],
)
async def test_provider_failure_is_calendar_unavailable(world, exc, diagnostic, reason) -> None:
    world.provider.fail_next("get_busy", exc)
    resp = await world.check()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == (diagnostic, reason)


async def test_provider_slower_than_its_timeout(world, monkeypatch) -> None:
    monkeypatch.setattr(availability, "PROVIDER_TIMEOUT", 0.01)

    async def slow(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(world.provider, "get_busy", slow)
    resp = await world.check()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("provider_timeout", "timeout")


async def test_provider_capped_by_the_request_deadline(world, monkeypatch) -> None:
    async def slow(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(world.provider, "get_busy", slow)
    ctx = world.new_ctx()
    world.clock.advance(8.0 - 0.01)  # 10 ms of budget left: the deadline caps the call
    resp = await world.check(ctx=ctx)
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert world.ctx.diagnostic == "deadline_exceeded"


async def test_deadline_exceeded_before_get_busy_makes_no_provider_call(world) -> None:
    ctx = world.new_ctx()
    world.clock.advance(9)
    resp = await world.check(ctx=ctx)
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert world.ctx.diagnostic == "deadline_exceeded"
    assert world.provider.calls == []


async def test_a_bare_provider_error_keeps_its_unclassified_diagnostic(world) -> None:
    world.provider.fail_next("get_busy", ProviderError())
    resp = await world.check()
    assert resp["status"] == "calendar_unavailable"
    assert world.ctx.diagnostic == "unclassified"  # the log line's STRICT guard fails such a path


async def test_provider_ms_is_recorded(world, monkeypatch) -> None:
    real = world.provider.get_busy

    async def timed(*a, **k):
        world.clock.advance(0.25)
        return await real(*a, **k)

    monkeypatch.setattr(world.provider, "get_busy", timed)
    await world.check()
    assert world.ctx.provider_ms == 250


async def test_get_busy_range_covers_the_windows_and_the_buffer() -> None:
    world = World(buffer_minutes=5)  # 45 + 5 = 50 min, inside the P17 budget
    seen: list[tuple[datetime, datetime]] = []
    real = world.provider.get_busy

    async def spy(cal, start, end):
        seen.append((start, end))
        return await real(cal, start, end)

    world.provider.get_busy = spy  # type: ignore[method-assign]
    await world.check({"from_date": "2026-10-06", "to_date": "2026-10-07"})
    assert seen == [(wall(date(2026, 10, 6), 13, 55), wall(date(2026, 10, 7), 19, 5))]


# --- keys (P18) ---------------------------------------------------------------------------------


async def test_keys_loaded_exactly_once_per_request(world) -> None:
    await world.check()
    assert world.keyring.load_calls == 1
    assert world.keyring.deadlines == [world.ctx.deadline]
    await world.check()
    assert world.keyring.load_calls == 2


async def test_key_store_unreachable(world) -> None:
    world.keyring.fail_next(SecretStoreUnavailable())
    resp = await world.check()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("secret_store_unreachable", None)
    assert world.provider.calls == []


async def test_key_invalid(world) -> None:
    world.keyring.fail_next(SecretInvalid("fingerprint-key", "wrong_length"))
    resp = await world.check()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("secret_invalid", "fingerprint-key.wrong_length")
    assert world.provider.calls == []


async def test_key_load_slower_than_its_timeout(world, monkeypatch) -> None:
    monkeypatch.setattr(availability, "SECRET_TIMEOUT", 0.01)

    async def slow(_deadline):
        await asyncio.sleep(5)

    monkeypatch.setattr(world.keyring, "load", slow)
    resp = await world.check()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("secret_store_unreachable", "timeout")
    assert world.provider.calls == []


async def test_rotation_needs_no_restart(world) -> None:
    operation = make_check_availability(world.provider, world.keyring, world.clock)
    body = {"from_date": "2026-10-06", "to_date": "2026-10-06"}
    first = await operation(world.binding, dict(body), world.new_ctx())
    world.keyring.rotate(SLOT_KEY_2)
    second = await operation(world.binding, dict(body), world.new_ctx())
    rotated = make_keys(SLOT_KEY_2, SLOT_KEY_1)
    for resp in (first, second):
        slot = resp["slots"][0]
        assert isinstance(
            tokens.verify(rotated, "test-alpha", slot["slot_id"], datetime.fromisoformat(slot["start"])), VerifiedSlot
        )
    new_only = make_keys(SLOT_KEY_2)
    slot = second["slots"][0]
    assert isinstance(tokens.verify(new_only, "test-alpha", slot["slot_id"], datetime.fromisoformat(slot["start"])), VerifiedSlot)
    old = first["slots"][0]
    assert not isinstance(tokens.verify(new_only, "test-alpha", old["slot_id"], datetime.fromisoformat(old["start"])), VerifiedSlot)


# --- issuance guard ---------------------------------------------------------------------------


async def test_misaligned_slot_is_dropped_and_logged(world, monkeypatch, caplog) -> None:
    real_issue = tokens.issue
    calls = {"n": 0}

    def flaky(keys, binding_id, start, duration, expiry):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tokens.SlotMisaligned()
        return real_issue(keys, binding_id, start, duration, expiry)

    monkeypatch.setattr(availability.tokens, "issue", flaky)
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06"})
    assert resp["status"] == "available"
    assert len(resp["slots"]) == 5
    assert any("slot_misaligned" in r.getMessage() for r in caplog.records)


async def test_every_slot_misaligned_is_calendar_unavailable(world, monkeypatch) -> None:
    def always(*_a):
        raise tokens.SlotMisaligned()

    monkeypatch.setattr(availability.tokens, "issue", always)
    resp = await world.check()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("slot_misaligned", None)


async def test_key_load_capped_by_the_request_deadline(world, monkeypatch) -> None:
    async def slow(_deadline):
        await asyncio.sleep(5)

    monkeypatch.setattr(world.keyring, "load", slow)
    ctx = world.new_ctx()
    world.clock.advance(8.0 - 0.01)  # 10 ms left: less than the 3 s key-load limit
    resp = await world.check(ctx=ctx)
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert (world.ctx.diagnostic, world.ctx.reason) == ("deadline_exceeded", None)
    assert world.provider.calls == []


@pytest.mark.parametrize("value", [30.0, 45.0])
async def test_integral_float_duration_is_accepted(world, value) -> None:
    # JSON Schema's `integer` accepts 30.0, so the contract does too.
    resp = await world.check({"from_date": "2026-10-06", "to_date": "2026-10-06", "duration_minutes": value})
    assert resp["status"] == "available"
    assert resp["duration_minutes"] == int(value)
    assert isinstance(resp["duration_minutes"], int)
