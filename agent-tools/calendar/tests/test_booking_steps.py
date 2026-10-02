"""The booking algorithm step by step (spec 7.4 steps 1-8, 7.5, 12 F1/F6/F8/F9/F14;
plan UC04b `test_booking_steps.py`, including the documented conservative
interim). Every non-OK path asserts the diagnostic and reason it sets for the
request's log line (plan section 1, "Reason codes")."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from calendar_tools.core import booking
from calendar_tools.core.booking import CLAIM_TTL, SERVICE_TAG
from calendar_tools.core.claims import ClaimRecord, ClaimStoreUnavailable
from calendar_tools.core.identity import contact_tag, fingerprint
from calendar_tools.core.contact import Contact
from calendar_tools.core.ports import (
    BookingMeta,
    NewEvent,
    ProviderAuthError,
    ProviderConfigError,
    ProviderTimeout,
    ProviderUnavailable,
    SecretInvalid,
    SecretStoreUnavailable,
)
from tests.booking_world import NAME, OTHER_PHONE, PHONE, SLOT, World, created_event, our_fingerprint, plant
from tests.fakes.keyring import FINGERPRINT_KEY, SLOT_KEY_2, SLOT_KEY_3, make_keys

FIVE = timedelta(minutes=5)


@pytest.fixture
def w() -> World:
    return World()


def logged(w: World) -> tuple[str | None, str | None]:
    return w.last_ctx.diagnostic, w.last_ctx.reason


# --- happy path --------------------------------------------------------------------


async def test_happy_path_books(w) -> None:
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is False
    b = resp["booking"]
    assert b["start"] == "2026-10-06T14:00:00-04:00" and b["end"] == "2026-10-06T14:30:00-04:00"
    assert b["display"] == "Tuesday, October 6 at 2:00 PM"
    assert b["timezone"] == "America/Toronto"
    assert b["appointment_type"] == {"id": "phone_call", "label": "Phone call"}
    assert "host" not in b
    assert len(b["booking_ref"]) == 6 and set(b["booking_ref"]) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert logged(w) == (None, None) and w.last_ctx.replayed is False
    # The event: section 7.3 content, BookingMeta stamped, no attendees.
    [event] = w.events()
    assert event.title == "Phone call: Jordan Example"
    assert event.description == f"Ref: {b['booking_ref']}"
    assert event.meta.service_tag == SERVICE_TAG and event.meta.binding_id == "test-alpha"
    assert event.meta.fingerprint == our_fingerprint(w) and event.request_key == event.meta.fingerprint
    assert event.meta.booking_ref == b["booking_ref"]
    assert event.timezone == "America/Toronto"
    # Every cell booked with the event's ID; attempt_id is 32 hex characters.
    records = w.records()
    assert sorted(k.start for k in records) == [SLOT + i * FIVE for i in range(6)]
    assert {r.state for r in records.values()} == {"booked"}
    assert {r.event_id for r in records.values()} == {event.event_id}
    assert {len(r.attempt_id) for r in records.values()} == {32}
    assert w.keyring.load_calls == 1


async def test_host_display_name_is_returned_when_set() -> None:
    w = World(host_display_name="Alex")
    resp = await w.book()
    assert resp["booking"]["host"] == {"display_name": "Alex"}


async def test_buffer_extends_the_claimed_cells() -> None:
    w = World(buffer_minutes=15, allowed_durations_minutes=[30])
    await w.book()
    assert sorted(k.start for k in w.records()) == [SLOT + i * FIVE for i in range(9)]


async def test_naive_start_is_read_in_the_binding_zone(w) -> None:
    body = w.body()
    body["start"] = "2026-10-06T14:00:00"
    assert (await w.book(body))["status"] == "booked"


async def test_utc_start_is_accepted(w) -> None:
    body = w.body()
    body["start"] = "2026-10-06T18:00:00Z"
    assert (await w.book(body))["status"] == "booked"


# --- step 1 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda w, b: b.update(slot_id="v2." + b["slot_id"][3:]), "malformed"),
        (lambda w, b: b.update(slot_id=b["slot_id"][:-1] + ("a" if b["slot_id"][-1] != "a" else "b")), "malformed"),
        (lambda w, b: b.update(slot_id="garbage"), "malformed"),
        (lambda w, b: b.update(slot_id=w.token(binding_id="test-beta")), "wrong_binding"),
        (lambda w, b: b.update(start="2026-10-06T14:30:00-04:00"), "start_mismatch"),
        (lambda w, b: b.update(start="2026-10-06T14:01:00-04:00"), "start_mismatch"),
        (lambda w, b: b.update(slot_id=w.token(keys=make_keys(SLOT_KEY_3))), "malformed"),
    ],
    ids=["version", "flip", "garbage", "wrong_binding", "other_slot", "one_minute", "unknown_key"],
)
async def test_bad_token_variants(w, mutate, reason) -> None:
    body = w.body()
    mutate(w, body)
    resp = await w.book(body)
    assert resp == {"status": "invalid_slot", "detail": {"reason": reason}}
    assert logged(w) == ("invalid_slot", reason)
    assert w.store.calls == [] and w.provider.calls == []


@pytest.mark.parametrize(
    ("mutate", "fields"),
    [
        (lambda b: b.pop("slot_id"), ["slot_id"]),
        (lambda b: b.update(slot_id=7), ["slot_id"]),
        (lambda b: b.update(slot_id="x" * 129), ["slot_id"]),
        (lambda b: b.pop("start"), ["start"]),
        (lambda b: b.update(start="tomorrow at two"), ["start"]),
        (lambda b: b.update(start="2026-10-06"), ["start"]),
        (lambda b: b.update(start="0001-01-01T00:00:00+14:00"), ["start"]),
        (lambda b: b.update(start="9999-12-31T23:59:59-14:00"), ["start"]),
        (lambda b: b.update(start="1999-12-31T23:00:00Z"), ["start"]),
        (lambda b: b.update(appointment_type=None), ["appointment_type"]),
        (lambda b: b.update(appointment_type="Phone Call!"), ["appointment_type"]),
        (lambda b: b.pop("contact"), ["contact"]),
        (lambda b: b.update(contact="Jordan"), ["contact"]),
        (lambda b: b.update(contact={"name": NAME, "phone": "not a number"}), ["contact.phone"]),
        (lambda b: b.update(contact={"name": NAME}), ["contact.phone", "contact.email"]),
        (lambda b: b.update(contact={"name": "x" * 81, "phone": PHONE}), ["contact.name"]),
        (lambda b: b.update(contact={"name": NAME, "phone": PHONE, "email": "no-at-sign"}), ["contact.email"]),
        (lambda b: (b.pop("slot_id"), b.update(start=None)), ["slot_id", "start"]),
    ],
)
async def test_malformed_body_fields_are_invalid_request(w, mutate, fields) -> None:
    body = w.body()
    mutate(body)
    resp = await w.book(body)
    assert resp == {"status": "invalid_request", "detail": {"fields": fields}}
    assert logged(w) == ("invalid_request", ",".join(fields))
    # Nothing is loaded or called for a malformed body.
    assert w.keyring.load_calls == 0 and w.store.calls == [] and w.provider.calls == []


async def test_notes_ignored_unless_accepted(w) -> None:
    body = w.body(notes=["not", "a", "string"])
    assert (await w.book(body))["status"] == "booked"
    assert "Notes" not in w.events()[0].description


async def test_notes_wrong_type_when_accepted_is_invalid_request() -> None:
    w = World(accept_notes=True)
    resp = await w.book(w.body(notes=42))
    assert resp == {"status": "invalid_request", "detail": {"fields": ["notes"]}}


async def test_notes_written_to_the_description_when_accepted() -> None:
    w = World(accept_notes=True, event_description_template="{notes_block}Ref: {booking_ref}")
    resp = await w.book(w.body(notes="Alpha: one\nBeta: two"))
    assert resp["status"] == "booked"
    assert w.events()[0].description == f"Notes: Alpha: one Beta: two\nRef: {resp['booking']['booking_ref']}"


async def test_calendar_key_resolution_failure_is_calendar_unavailable(w) -> None:
    w.provider.fail_next("resolve_calendar_identity", ProviderUnavailable("dns"))
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("provider_unavailable", "dns")
    assert w.store.calls == []


async def test_secret_store_unavailable_before_any_claim_or_provider_call(w) -> None:
    w.keyring.fail_next(SecretStoreUnavailable())
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("secret_store_unreachable", None)
    assert w.store.calls == [] and w.provider.calls == []


async def test_secret_invalid_before_any_claim_or_provider_call(w) -> None:
    w.keyring.fail_next(SecretInvalid("fingerprint-key", "wrong_length"))
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("secret_invalid", "fingerprint-key.wrong_length")
    assert w.store.calls == [] and w.provider.calls == []


async def test_key_load_timeout(w, monkeypatch) -> None:
    async def slow(deadline):
        await asyncio.sleep(5)

    monkeypatch.setattr(booking, "SECRET_TIMEOUT", 0.01)
    monkeypatch.setattr(w.keyring, "load", slow)
    resp = await w.book()
    assert resp["status"] == "calendar_unavailable"
    assert logged(w) == ("secret_store_unreachable", "timeout")


async def test_keys_loaded_exactly_once_per_booking(w) -> None:
    await w.book()
    assert w.keyring.load_calls == 1
    await w.book()  # a replay
    assert w.keyring.load_calls == 2


# --- step 2 ------------------------------------------------------------------------


async def test_own_booked_and_event_exists_replays_with_current_times(w) -> None:
    first = await w.book()
    # The host moves the event 30 minutes later (claims are not touched).
    [event] = w.events()
    event.start, event.end = SLOT + timedelta(minutes=30), SLOT + timedelta(minutes=60)
    w.provider.calls.clear()
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert resp["booking"]["start"] == "2026-10-06T14:30:00-04:00"
    assert resp["booking"]["booking_ref"] == first["booking"]["booking_ref"]
    assert w.last_ctx.replayed is True and logged(w) == (None, None)
    assert "create_event" not in w.provider.calls and len(w.events()) == 1


async def test_own_booked_event_gone_releases_and_answers_cancelled(w) -> None:
    await w.book()
    [event] = w.events()
    w.provider.cancel_event(w.binding.calendar_ref, event.event_id)
    resp = await w.book()
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "cancelled"}}
    assert logged(w) == ("slot_unavailable", "cancelled")
    assert w.records() == {}


async def test_cancelled_release_keeps_another_attempts_cells(w) -> None:
    await w.book()
    [event] = w.events()
    w.provider.cancel_event(w.binding.calendar_ref, event.event_id)
    # A later cell now belongs to a different attempt (same fingerprint, other event).
    plant(w, state="booked", attempt="cd" * 8, event_id="evt-other", cells=[SLOT + 5 * FIVE])
    await w.book()
    assert [k.start for k in w.records()] == [SLOT + 5 * FIVE]


async def test_get_event_failure_is_calendar_unavailable(w) -> None:
    await w.book()
    w.provider.fail_next("get_event", ProviderUnavailable())
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("provider_unavailable", None)


async def test_first_cell_read_failure_is_calendar_unavailable(w) -> None:
    w.store.fail_next("read")
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("claim_store_unreachable", "injected")
    assert "create_event" not in w.provider.calls


async def test_corrupt_first_cell_is_calendar_unavailable(w) -> None:
    w.store.plant_raw(w.cell(SLOT), b"{not json")
    resp = await w.book()
    assert resp["status"] == "calendar_unavailable"
    assert logged(w) == ("claim_store_unreachable", "record_invalid")


async def test_own_live_pending_then_booked_replays(w) -> None:
    record = plant(w)

    def finish() -> None:
        if len(w.clock.sleeps) == 3:
            for key in list(w.records()):
                w.store._write(key, record.booked("evt-9"))

    w.clock.on_sleep.append(finish)
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert resp["booking"]["start"] == "2026-10-06T14:00:00-04:00"
    assert w.clock.sleeps == [0.25, 0.25, 0.25]
    assert "create_event" not in w.provider.calls


async def test_own_pending_disappears_continues_to_step_3_and_books(w) -> None:
    plant(w)

    def vanish() -> None:
        for key in list(w.records()):
            del w.store._cells[key]

    w.clock.on_sleep.append(vanish)
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is False
    assert w.provider.calls.count("create_event") == 1


async def test_own_pending_at_poll_deadline_found_by_lookup_is_finalized(w) -> None:
    record = plant(w)
    event_id = await created_event(w)
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert logged(w) == (None, None)
    assert {(r.state, r.event_id, r.attempt_id) for r in w.records().values()} == {("booked", event_id, record.attempt_id)}
    assert "create_event" not in w.provider.calls


async def test_poll_stops_at_the_s3_boundary(w, monkeypatch) -> None:
    plant(w)
    seen: list[float] = []
    real = w.provider.find_bookings

    async def spy(*args, **kwargs):
        seen.append(w.last_ctx.deadline.remaining())
        return await real(*args, **kwargs)

    monkeypatch.setattr(w.provider, "find_bookings", spy)
    resp = await w.book()
    assert resp["status"] == "booking_unconfirmed"
    assert logged(w) == ("replay_poll_timeout", None)
    # The SF-7 lookup still has a provider call, a read and one compare-and-swap of budget.
    assert 5.0 <= seen[0] < 5.0 + 0.25 + 1e-9
    assert len(w.clock.sleeps) == 12  # 8 s - 12 x 0.25 s = exactly the 5 s reserve


async def test_own_pending_lookup_failure_is_unconfirmed(w) -> None:
    plant(w)
    w.provider.fail_next("find_bookings", ProviderUnavailable())
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("replay_poll_timeout", "lookup_failed")


async def test_read_failure_while_polling_own_pending_is_unconfirmed_not_unavailable(w) -> None:
    plant(w)
    w.clock.on_sleep.append(lambda: w.store.fail_next("read") if len(w.clock.sleeps) == 1 else None)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("claim_store_unreachable", "injected")


async def test_own_stale_pending_first_cell_is_unconfirmed_interim(w) -> None:
    plant(w)
    w.clock.advance(CLAIM_TTL.total_seconds())
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "stale_own_claim")
    assert "create_event" not in w.provider.calls


async def test_other_fingerprint_on_first_cell_continues_to_step_6(w) -> None:
    plant(w, fp="f" * 64)
    resp = await w.book()
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "taken"}}
    assert logged(w) == ("claim_conflict", "claim_held")


# --- step 3 ------------------------------------------------------------------------


async def test_calendar_record_with_our_fingerprint_replays_without_claims(w) -> None:
    await created_event(w, start=SLOT, end=SLOT + timedelta(minutes=30))
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert w.records() == {}  # nothing claimed
    assert "create_event" not in w.provider.calls


async def test_replay_via_step3_after_max_days_ahead_lowered(w) -> None:
    # A booking six days out; max_days_ahead lowered to 1 (P14: search to the token's end).
    far = SLOT + timedelta(days=4)
    body = w.body(start=far)
    assert (await w.book(body))["status"] == "booked"
    for key in list(w.records()):
        del w.store._cells[key]  # only the calendar shows it now
    w.replace_binding(max_days_ahead=1)
    resp = await w.book(body)
    assert resp["status"] == "booked" and resp["replayed"] is True


async def test_contact_limit_reached(w) -> None:
    for day in (1, 2):
        await w.book(w.body(start=SLOT + timedelta(days=day)))
    resp = await w.book()
    assert resp == {"status": "limit_reached"}
    assert logged(w) == ("limit_reached", None)


async def test_contact_limit_counts_only_this_contact(w) -> None:
    for day in (1, 2):
        await w.book(w.body(start=SLOT + timedelta(days=day)))
    assert (await w.book(w.body(phone=OTHER_PHONE)))["status"] == "booked"


async def test_find_bookings_failure_is_calendar_unavailable(w) -> None:
    w.provider.fail_next("find_bookings", ProviderAuthError("refresh_rejected"))
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("credential_rejected", "refresh_rejected")


# --- replay independence (B1) -------------------------------------------------------


async def _book_then(w: World, change) -> dict:
    body = w.body()
    first = await w.book(body)
    assert first["status"] == "booked"
    change(w)
    resp = await w.book(body)
    assert resp["booking"]["booking_ref"] == first["booking"]["booking_ref"]
    return resp


@pytest.mark.parametrize(
    "change",
    [
        lambda w: w.clock.advance(31 * 60),  # (a) token expired
        lambda w: w.clock.set(SLOT - timedelta(minutes=30)),  # (b) inside min-notice
        lambda w: w.replace_binding(bookable_hours={"mon": [["09:00", "10:00"]]}),  # (c) hours changed
        lambda w: w.replace_binding(allowed_durations_minutes=[45], default_duration_minutes=45),  # (d) duration
        lambda w: w.keyring.rotate(SLOT_KEY_2),  # (e) slot-token-key rotated, same service
    ],
    ids=["expired", "min_notice", "hours", "duration", "key_rotation"],
)
async def test_replay_is_independent_of_time_config_and_key_rotation(w, change) -> None:
    resp = await _book_then(w, change)
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert len(w.events()) == 1


async def test_replay_independence_via_calendar_only(w) -> None:
    body = w.body()
    await w.book(body)
    for key in list(w.records()):
        del w.store._cells[key]
    w.clock.advance(31 * 60)
    w.replace_binding(bookable_hours={"mon": [["09:00", "10:00"]]})
    resp = await w.book(body)
    assert resp["status"] == "booked" and resp["replayed"] is True


# --- step 4 ------------------------------------------------------------------------


async def test_expired_token_is_refused_at_step_4(w) -> None:
    body = w.body()
    w.clock.advance(30 * 60)  # now == expiry is expired
    resp = await w.book(body)
    assert resp == {"status": "invalid_slot", "detail": {"reason": "expired"}}
    assert logged(w) == ("invalid_slot", "expired")
    # Steps 2 and 3 ran first (replay never depends on time), nothing claimed.
    assert w.store.calls and all(op == "read" for op, _ in w.store.calls)
    assert w.provider.calls == ["resolve_calendar_identity", "find_bookings"]


async def test_outside_current_bookable_hours(w) -> None:
    body = w.body()
    w.replace_binding(bookable_hours={"tue": [["15:00", "19:00"]]})
    resp = await w.book(body)
    assert resp == {"status": "invalid_slot", "detail": {"reason": "outside_bookable_hours"}}
    assert logged(w) == ("invalid_slot", "outside_bookable_hours")


async def test_slot_ending_past_the_window_is_outside_bookable_hours(w) -> None:
    body = w.body()
    w.replace_binding(bookable_hours={"tue": [["13:00", "14:15"]]})
    resp = await w.book(body)
    assert resp["detail"]["reason"] == "outside_bookable_hours"


async def test_duration_no_longer_allowed(w) -> None:
    body = w.body(duration=45)
    w.replace_binding(allowed_durations_minutes=[30], default_duration_minutes=30)
    resp = await w.book(body)
    assert resp == {"status": "invalid_slot", "detail": {"reason": "duration_not_allowed"}}
    assert logged(w) == ("invalid_slot", "duration_not_allowed")


async def test_unknown_appointment_type_is_invalid_request(w) -> None:
    resp = await w.book(w.body(type_id="video_call"))
    assert resp == {"status": "invalid_request", "detail": {"fields": ["appointment_type"]}}
    assert logged(w) == ("invalid_request", "appointment_type")


async def test_required_contact_fields_checked_at_step_4(w) -> None:
    resp = await w.book(w.body(name=None))
    assert resp == {"status": "invalid_request", "detail": {"fields": ["contact.name"]}}
    w2 = World(required_contact_fields=["name", "email"])
    resp = await w2.book(w2.body())
    assert resp == {"status": "invalid_request", "detail": {"fields": ["contact.email"]}}


async def test_too_soon(w) -> None:
    w.clock.set(SLOT - timedelta(hours=23, minutes=55))
    body = w.body()  # a fresh token, issued inside the notice window
    resp = await w.book(body)
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "too_soon"}}
    assert logged(w) == ("slot_unavailable", "too_soon")


async def test_min_notice_boundary_equal_is_allowed(w) -> None:
    w.clock.set(SLOT - timedelta(hours=24))
    assert (await w.book(w.body()))["status"] == "booked"


# --- step 5 ------------------------------------------------------------------------


async def test_busy_and_first_cell_not_ours_is_taken(w) -> None:
    w.provider.add_external_event(w.binding.calendar_ref, SLOT + timedelta(minutes=10), SLOT + timedelta(minutes=20))
    resp = await w.book()
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "taken"}}
    assert logged(w) == ("claim_conflict", "busy")
    assert w.records() == {}


async def test_busy_within_buffer_is_taken() -> None:
    w = World(buffer_minutes=15, allowed_durations_minutes=[30])
    w.provider.add_external_event(w.binding.calendar_ref, SLOT - timedelta(minutes=30), SLOT - timedelta(minutes=10))
    resp = await w.book()
    assert resp["detail"]["reason"] == "taken"


async def test_busy_but_first_cell_ours_goes_back_to_step_2(w, monkeypatch) -> None:
    """A concurrent attempt of ours claimed and created between our step 2 and
    step 5: the busy time is our own booking, and the answer is a replay."""
    record = plant(w, fp="f" * 64)  # step 2 sees another fingerprint ...
    real_busy = w.provider.get_busy

    async def busy_then_ours(*args):
        # ... and by step 5 our own attempt has claimed and booked.
        for key in list(w.records()):
            del w.store._cells[key]
        event_id = await created_event(w)
        plant(w, state="booked", event_id=event_id)
        return await real_busy(*args)

    monkeypatch.setattr(w.provider, "get_busy", busy_then_ours)
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert record.fingerprint != our_fingerprint(w)
    assert "create_event" not in w.provider.calls  # the planted event is not counted (created_event clears calls)


async def test_get_busy_failure_is_calendar_unavailable(w) -> None:
    w.provider.fail_next("get_busy", ProviderConfigError("calendar_not_found"))
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("provider_config_error", "calendar_not_found")


async def _guard(w: World, monkeypatch, spare_ms: int) -> dict:
    """Spend the budget at step 5 so exactly `need + spare_ms` remains."""
    real = w.provider.get_busy

    async def spend(*args):
        need = 6 * 0.1 + 3.0 + 1.0  # 6 cells, one create, one finalize
        w.clock.set(w.clock.now() + timedelta(milliseconds=round((8.0 - need) * 1000) - spare_ms))
        return await real(*args)

    monkeypatch.setattr(w.provider, "get_busy", spend)
    return await w.book()


async def test_deadline_guard_just_enough_proceeds(w, monkeypatch) -> None:
    assert (await _guard(w, monkeypatch, 0))["status"] == "booked"


async def test_deadline_guard_one_ms_short_claims_nothing(w, monkeypatch) -> None:
    resp = await _guard(w, monkeypatch, -1)
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("deadline_exceeded", None)
    assert [op for op, _ in w.store.calls] == ["read"]  # step 2's read only, no claim
    assert w.records() == {} and "create_event" not in w.provider.calls


# --- step 6 ------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["pending", "booked"])
async def test_held_by_another_live_claim_releases_acquired_and_is_taken(w, state) -> None:
    plant(w, fp="f" * 64, state=state, start=SLOT + 3 * FIVE, cells=[SLOT + 3 * FIVE], event_id="evt-z")
    resp = await w.book()
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "taken"}}
    assert logged(w) == ("claim_conflict", "claim_held")
    assert [k.start for k in w.records()] == [SLOT + 3 * FIVE]  # our three cells released


async def test_held_by_another_stale_claim_is_still_taken_interim(w) -> None:
    plant(w, fp="f" * 64, cells=[SLOT + FIVE])
    w.clock.advance(CLAIM_TTL.total_seconds() * 10)  # far past claim_ttl
    resp = await w.book(w.body())
    assert resp["detail"]["reason"] == "taken"
    assert logged(w) == ("claim_conflict", "claim_held")


async def test_own_fingerprint_on_first_cell_goes_to_step_2(w, monkeypatch) -> None:
    """Another attempt of ours claims the first cell between our step 2 and 6."""
    real = w.provider.get_busy

    async def claim_first(*args):
        result = await real(*args)
        record = plant(w, cells=[SLOT])
        # ... and finishes while we poll.
        w.clock.on_sleep.append(lambda: w.store._write(w.cell(SLOT), record.booked("evt-q")))
        return result

    monkeypatch.setattr(w.provider, "get_busy", claim_first)
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert "create_event" not in w.provider.calls


@pytest.mark.parametrize(("stale", "reason"), [(False, "own_claim_live"), (True, "stale_own_claim")])
async def test_own_fingerprint_on_later_cell_is_unconfirmed_interim(w, monkeypatch, stale, reason) -> None:
    real = w.provider.get_busy

    async def leftover(*args):
        result = await real(*args)
        plant(w, cells=[SLOT + 2 * FIVE])
        if stale:
            w.clock.advance(2)  # older than the (shortened) claim_ttl
        return result

    monkeypatch.setattr(w.provider, "get_busy", leftover)
    if stale:
        # Shortened so the request deadline survives; only the leftover's store age matters.
        monkeypatch.setattr(booking, "CLAIM_TTL", timedelta(seconds=1))
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", reason)
    assert [k.start for k in w.records()] == [SLOT + 2 * FIVE]  # acquired cells released
    assert "create_event" not in w.provider.calls


async def test_claim_store_failure_mid_claim_releases_acquired(w) -> None:
    gate = w.hooks.gate("before", "try_claim", w.cell(SLOT + 2 * FIVE))
    task = asyncio.create_task(w.book())
    await gate.reached.wait()
    w.store.fail_next("try_claim")
    gate.open()
    resp = await task
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("claim_store_unreachable", "injected")
    assert w.records() == {}
    assert "create_event" not in w.provider.calls


async def test_release_failure_leaves_cell_pending_with_our_attempt(w) -> None:
    plant(w, fp="f" * 64, cells=[SLOT + 2 * FIVE])
    w.store.fail_next("release")
    resp = await w.book()
    assert resp["detail"]["reason"] == "taken"
    held = w.records()
    ours = [r for k, r in held.items() if r.fingerprint == our_fingerprint(w)]
    assert len(ours) == 1 and ours[0].state == "pending" and len(ours[0].attempt_id) == 32


async def test_deadline_mid_claim_releases_and_is_deadline_exceeded(w) -> None:
    gate = w.hooks.gate("after", "try_claim", w.cell(SLOT + FIVE))
    task = asyncio.create_task(w.book())
    await gate.reached.wait()
    w.clock.advance(8.0)
    gate.open()
    resp = await task
    assert resp["status"] == "calendar_unavailable"
    assert logged(w) == ("deadline_exceeded", None)
    assert w.records() == {}  # released within the grace period


# --- step 7 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "diagnostic", "reason"),
    [
        (ProviderAuthError("consent_revoked"), "credential_rejected", "consent_revoked"),
        (ProviderConfigError("insufficient_scope"), "provider_config_error", "insufficient_scope"),
        (SecretInvalid("cal-binding-test-alpha-fake", "bad_json"), "secret_invalid", "cal-binding-test-alpha-fake.bad_json"),
    ],
)
async def test_definitive_create_rejection_releases_and_is_unavailable(w, exc, diagnostic, reason) -> None:
    w.provider.fail_next("create_event", exc)
    resp = await w.book()
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == (diagnostic, reason)
    assert w.records() == {} and w.events() == []


@pytest.mark.parametrize(
    "exc",
    [ProviderTimeout(maybe_committed=True), ProviderTimeout(maybe_committed=False), ProviderUnavailable("http_503")],
)
async def test_uncertain_create_is_unconfirmed_and_claims_stay_pending(w, exc) -> None:
    w.provider.fail_next("create_event", exc)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", exc.diagnostic)
    records = w.records()
    assert len(records) == 6 and {r.state for r in records.values()} == {"pending"}
    assert w.provider.calls.count("create_event") == 1  # interim: no lookup, no retry


async def test_create_slower_than_its_timeout_is_unconfirmed(w, monkeypatch) -> None:
    async def slow(*args):
        await asyncio.sleep(5)

    monkeypatch.setattr(booking, "PROVIDER_TIMEOUT", 0.01)
    monkeypatch.setattr(w.provider, "create_event", slow)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "provider_timeout")
    assert {r.state for r in w.records().values()} == {"pending"}


async def test_retry_after_uncertain_create_with_live_pending_is_unconfirmed_not_taken(w) -> None:
    w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
    await w.book()
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("replay_poll_timeout", None)


# --- step 8 ------------------------------------------------------------------------


async def test_finalize_failure_is_still_booked(w) -> None:
    w.store.fail_next("replace")
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is False
    assert logged(w) == ("claim_finalize_failed", None)
    states = sorted(r.state for r in w.records().values())
    assert states.count("pending") == 1 and states.count("booked") == 5


async def test_retry_after_finalize_failure_replays(w) -> None:
    w.store.fail_next("replace", ClaimStoreUnavailable("timeout"))
    first = await w.book()
    # The first cell may be the one left pending; a retry must still say booked.
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert resp["booking"]["booking_ref"] == first["booking"]["booking_ref"]
    assert len(w.events()) == 1


async def test_deadline_spent_before_finalize_is_still_booked(w, monkeypatch) -> None:
    real = w.provider.create_event

    async def slow_create(*args):
        result = await real(*args)
        w.clock.advance(8.0)
        return result

    monkeypatch.setattr(w.provider, "create_event", slow_create)
    resp = await w.book()
    assert resp["status"] == "booked"
    assert logged(w) == ("claim_finalize_failed", None)


async def test_provider_ms_accumulates(w, monkeypatch) -> None:
    for method in ("find_bookings", "get_busy", "create_event"):
        real = getattr(w.provider, method)

        def slow(real=real):
            async def call(*args):
                w.clock.advance(0.05)
                return await real(*args)

            return call

        monkeypatch.setattr(w.provider, method, slow())
    await w.book()
    assert w.last_ctx.provider_ms == 150


async def test_step_methods_exist_per_spec_step() -> None:
    names = [f"_step{i}_" for i in range(1, 9)]
    methods = [m for m in dir(booking.BookingService) if m.startswith("_step")]
    assert [m[:7] for m in sorted(methods)] == names


async def test_claim_store_failure_after_own_cell_seen_is_unconfirmed(w, monkeypatch) -> None:
    """Review r1: once our own fingerprint turned up at step 6 (an attempt of
    ours may be creating), a failed re-read is `booking_unconfirmed`, never
    `calendar_unavailable`."""
    real = w.provider.get_busy

    async def claim_first(*args):
        result = await real(*args)
        plant(w, cells=[SLOT])
        w.store.fail_next("read")  # step 2's re-read, round 2
        return result

    monkeypatch.setattr(w.provider, "get_busy", claim_first)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("claim_store_unreachable", "injected")


async def test_busy_reread_failure_is_unconfirmed(w) -> None:
    """Review r1: the busy time may be our own concurrent booking."""
    w.provider.add_external_event(w.binding.calendar_ref, SLOT, SLOT + timedelta(minutes=30))
    gate = w.hooks.gate("before", "read", occurrence=2)
    task = asyncio.create_task(w.book())
    await gate.reached.wait()
    w.store.fail_next("read")
    gate.open()
    resp = await task
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("claim_store_unreachable", "injected")


async def test_slow_claims_cannot_starve_the_create(w) -> None:
    """Review r1: each claim keeps the create's and finalize's budget in
    reserve; a claim that would eat it is `deadline_exceeded`, released, and
    no event is created."""
    gate = w.hooks.gate("after", "try_claim", w.cell(SLOT + 2 * FIVE))
    task = asyncio.create_task(w.book())
    await gate.reached.wait()
    w.clock.advance(8.0 - 4.0 + 0.001)  # less than create + finalize remains
    gate.open()
    resp = await task
    assert resp == {"status": "calendar_unavailable", "retryable": True}
    assert logged(w) == ("deadline_exceeded", None)
    assert w.records() == {} and "create_event" not in w.provider.calls


async def test_sf7_lookup_uses_the_records_binding_on_a_shared_calendar() -> None:
    """Review r2: the original ran through test-beta (same physical calendar),
    created the event and never finalized; a retry through test-alpha finds it
    with the record's own binding_id."""
    w = World()
    w.provider.canonical_ids["primary"] = "cal-explicit-0001"
    w.add_binding("test-beta", calendar_id="cal-explicit-0001", credential_secret_name="cal-binding-test-beta-fake")
    record = plant(w, binding_id="test-beta")
    event_id = await created_event(w, binding_id="test-beta")
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert {(r.state, r.event_id, r.attempt_id) for r in w.records().values()} == {("booked", event_id, record.attempt_id)}
    assert "create_event" not in w.provider.calls


async def test_own_attempt_flickering_is_bounded_and_unconfirmed(w, monkeypatch) -> None:
    """Review r2: our own first cell appears at every step 6 and is gone at
    every step 2; after MAX_ROUNDS the answer is unknown, never taken."""
    real_claim, real_read = w.store.try_claim, w.store.read
    first = w.cell(SLOT)

    async def claim(key, record):
        if key == first:
            plant(w, cells=[SLOT])
        return await real_claim(key, record)

    async def read(key):
        if key == first:
            w.store._cells.pop(first, None)
        return await real_read(key)

    monkeypatch.setattr(w.store, "try_claim", claim)
    monkeypatch.setattr(w.store, "read", read)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("replay_poll_timeout", "replay_loop")
    assert w.provider.calls.count("get_busy") == booking.MAX_ROUNDS
    assert "create_event" not in w.provider.calls
