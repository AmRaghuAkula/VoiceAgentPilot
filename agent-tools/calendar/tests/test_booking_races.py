"""Races and lifecycle (spec 7.4 "Why this is correct", 13.1; plan UC04b
`test_booking_races.py`), driven by concurrent `book()` tasks and the fake
claim store's barrier hooks. The invariant checked everywhere: no two
`booked` outcomes overlap on one physical calendar, every created event has
all of its cells, and a retry of our own booking never answers `taken`."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from datetime import timedelta

import pytest

from calendar_tools.core.recovery import CLAIM_TTL
from tests.booking_world import OTHER_PHONE, PHONE, SLOT, World

FIVE = timedelta(minutes=5)
PHONES = ["+16135550101", "+16135550102", "+16135550103", "+16135550104"]


def assert_no_overlapping_bookings(w: World, binding_id: str = "test-alpha") -> None:
    events = sorted(w.events(binding_id), key=lambda e: e.start)
    for a, b in itertools.combinations(events, 2):
        assert not (a.start < b.end and b.start < a.end), "two overlapping bookings"
    records = w.records()
    for event in events:
        cells = [k for k, r in records.items() if r.event_id == event.event_id]
        start = event.start.replace(minute=event.start.minute - event.start.minute % 5)
        expected = []
        t = start
        while t < event.end:
            expected.append(t)
            t += FIVE
        assert set(expected) <= {k.start for k in cells}


async def test_same_fingerprint_double_invocation_creates_once() -> None:
    w = World()
    body = w.body()
    first, second = await asyncio.gather(
        w.service.book(w.binding, body, w.ctx()), w.service.book(w.binding, body, w.ctx())
    )
    assert {first["status"], second["status"]} == {"booked"}
    assert sorted([first["replayed"], second["replayed"]]) == [False, True]
    assert first["booking"]["booking_ref"] == second["booking"]["booking_ref"]
    assert w.provider.calls.count("create_event") == 1
    assert len(w.events()) == 1


@pytest.mark.parametrize("cell_index", [0, 3, 5])
@pytest.mark.parametrize("where", ["after_claim", "before_finalize"])
async def test_retry_between_claim_and_create_never_answers_taken(cell_index, where) -> None:
    """N2/BLOCK-3: the original is parked holding claims (part or all of its
    cells, or all of them with the event created but not finalized) while an
    identical retry arrives."""
    w = World()
    body = w.body()
    if where == "after_claim":
        gate = w.hooks.gate("after", "try_claim", w.cell(SLOT + cell_index * FIVE))
    else:
        gate = w.hooks.gate("before", "replace", w.cell(SLOT + cell_index * FIVE))
    original = asyncio.create_task(w.service.book(w.binding, body, w.ctx()))
    await gate.reached.wait()
    w.clock.on_sleep.append(gate.open)  # the original continues while the retry polls
    retry = await w.service.book(w.binding, body, w.ctx())
    first = await original
    assert first["status"] == "booked" and first["replayed"] is False
    assert retry["status"] == "booked" and retry["replayed"] is True
    assert w.provider.calls.count("create_event") == 1


async def test_overlapping_different_contacts_exactly_one_booked() -> None:
    w = World()
    results = await asyncio.gather(
        w.service.book(w.binding, w.body(phone=PHONE), w.ctx()),
        w.service.book(w.binding, w.body(phone=OTHER_PHONE), w.ctx()),
    )
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["booked", "slot_unavailable"]
    assert [r["detail"]["reason"] for r in results if r["status"] == "slot_unavailable"] == ["taken"]
    assert len(w.events()) == 1
    assert_no_overlapping_bookings(w)


# The round-2 chain (N1/BLOCK-1) with mixed durations: W overlaps X and Y,
# X overlaps Y, Y overlaps ours.
CHAIN = [
    (SLOT, 45),  # W 14:00-14:45
    (SLOT + timedelta(minutes=30), 30),  # X 14:30-15:00
    (SLOT + timedelta(minutes=40), 45),  # Y 14:40-15:25
    (SLOT + timedelta(minutes=60), 30),  # ours 15:00-15:30
]


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
async def test_chain_with_mixed_durations_never_double_books(order) -> None:
    w = World()
    bodies = [w.body(start=s, duration=d, phone=PHONES[i]) for i, (s, d) in enumerate(CHAIN)]
    results = await asyncio.gather(*(w.service.book(w.binding, bodies[i], w.ctx()) for i in order))
    assert all(r["status"] in ("booked", "slot_unavailable") for r in results)
    assert any(r["status"] == "booked" for r in results)
    assert len(w.events()) == sum(r["status"] == "booked" for r in results)
    assert_no_overlapping_bookings(w)


async def test_orphan_loser_chain_never_double_books() -> None:
    """A loser briefly holds cells (spec 7.4 residual risk): a third attempt in
    that window gets a spurious `taken`, and no two bookings ever overlap."""
    w = World()
    a = w.body(start=SLOT + timedelta(minutes=30), phone=PHONES[0])  # A 14:30-15:00
    b = w.body(start=SLOT + timedelta(minutes=15), phone=PHONES[1])  # B 14:15-14:45 (the loser)
    c = w.body(start=SLOT, phone=PHONES[2])  # C 14:00-14:30
    gate_a = w.hooks.gate("after", "try_claim", w.cell(SLOT + timedelta(minutes=55)))
    task_a = asyncio.create_task(w.service.book(w.binding, a, w.ctx()))
    await gate_a.reached.wait()  # A holds all its cells, nothing created yet
    gate_b = w.hooks.gate("after", "try_claim", w.cell(SLOT + timedelta(minutes=25)))
    task_b = asyncio.create_task(w.service.book(w.binding, b, w.ctx()))
    await gate_b.reached.wait()  # B holds 14:15-14:25 and is about to hit A's 14:30
    c_first = await w.service.book(w.binding, c, w.ctx())
    assert c_first == {"status": "slot_unavailable", "detail": {"reason": "taken"}}  # spurious, harmless
    gate_b.open()
    assert (await task_b)["detail"]["reason"] == "taken"
    gate_a.open()
    assert (await task_a)["status"] == "booked"
    # The orphaned cells are free again: C books on a fresh offer.
    assert (await w.service.book(w.binding, w.body(start=SLOT, phone=PHONES[2]), w.ctx()))["status"] == "booked"
    assert len(w.events()) == 2
    assert_no_overlapping_bookings(w)


def _shared_calendar_world(**alpha: object) -> World:
    w = World(**alpha)
    # "primary" on alpha and an explicit ID on beta name one physical calendar (SF-4).
    w.provider.canonical_ids["primary"] = "cal-explicit-0001"
    w.add_binding("test-beta", calendar_id="cal-explicit-0001", credential_secret_name="cal-binding-test-beta-fake")
    assert w.cal_key("test-alpha") == w.cal_key("test-beta")
    return w


@pytest.mark.parametrize("beta_first", [False, True])
async def test_two_bindings_sharing_one_calendar_racing(beta_first) -> None:
    """BLOCK-2: two bindings on one physical calendar, overlapping slots."""
    w = _shared_calendar_world()
    calls = [
        w.service.book(w.bindings["test-alpha"], w.body(phone=PHONE), w.ctx()),
        w.service.book(
            w.bindings["test-beta"], w.body(start=SLOT + timedelta(minutes=15), phone=OTHER_PHONE, binding_id="test-beta"), w.ctx()
        ),
    ]
    if beta_first:
        calls.reverse()
    results = await asyncio.gather(*calls)
    assert sorted(r["status"] for r in results) == ["booked", "slot_unavailable"]
    assert len(w.events()) == 1
    assert_no_overlapping_bookings(w)


async def test_two_bindings_with_different_buffers_keep_the_buffer_tail() -> None:
    """SF-2: alpha (buffer 15) books 14:00-14:30, so 14:30-14:45 is its tail;
    beta (buffer 0) sees no busy overlap at 14:30 but the claim cell is held."""
    w = _shared_calendar_world(buffer_minutes=15, allowed_durations_minutes=[30])
    assert (await w.book(w.body()))["status"] == "booked"
    beta_at = lambda minutes: w.body(  # noqa: E731
        start=SLOT + timedelta(minutes=minutes), phone=OTHER_PHONE, binding_id="test-beta"
    )
    resp = await w.book(beta_at(30), binding_id="test-beta")
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "taken"}}
    assert (w.last_ctx.diagnostic, w.last_ctx.reason) == ("claim_conflict", "claim_held")
    assert (await w.book(beta_at(45), binding_id="test-beta"))["status"] == "booked"
    assert_no_overlapping_bookings(w)


async def test_buffer_config_change_after_booking_does_not_disturb_it() -> None:
    """SF-2: the stamped buffer, not the current config, protects the tail."""
    w = World(buffer_minutes=15, allowed_durations_minutes=[30])
    body = w.body()
    assert (await w.book(body))["status"] == "booked"
    w.replace_binding(buffer_minutes=0)
    assert (await w.book(body))["replayed"] is True
    resp = await w.book(w.body(start=SLOT + timedelta(minutes=30), phone=OTHER_PHONE))
    assert resp["detail"]["reason"] == "taken"
    # The stamped-buffer claim cell, not calendar busy time, protects the tail.
    assert (w.last_ctx.diagnostic, w.last_ctx.reason) == ("claim_conflict", "claim_held")
    assert {r.buffer_minutes for r in w.records().values()} == {15}
    assert len(w.events()) == 1


async def test_many_concurrent_identical_requests_create_once() -> None:
    w = World()
    body = w.body()
    results = await asyncio.gather(*(w.service.book(w.binding, body, w.ctx()) for _ in range(6)))
    assert all(r["status"] == "booked" for r in results)
    assert sum(not r["replayed"] for r in results) == 1
    assert w.provider.calls.count("create_event") == 1


async def test_many_concurrent_different_contacts_one_slot() -> None:
    w = World()
    phones = [f"+1613555010{i}" for i in range(6)]
    results = await asyncio.gather(*(w.service.book(w.binding, w.body(phone=p), w.ctx()) for p in phones))
    assert sum(r["status"] == "booked" for r in results) == 1
    assert len(w.events()) == 1
    assert_no_overlapping_bookings(w)



# --- UC04c: recovery and lifecycle (spec 7.4 "Recovery", F14, F15, SF-3, SF-7, SF-8) ---

STALE = CLAIM_TTL.total_seconds() + 1


async def _die(w: World, task: asyncio.Task, gate) -> None:
    """The instance running `task` dies at `gate`: nothing after it runs."""
    await gate.reached.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_f15_owner_died_before_create_then_another_contact_books_the_slot() -> None:
    w = World()
    gate = w.hooks.gate("after", "try_claim", w.cell(SLOT + 5 * FIVE))  # all six claimed
    await _die(w, asyncio.create_task(w.book()), gate)
    assert {r.state for r in w.records().values()} == {"pending"} and w.events() == []
    # Within claim_ttl the slot stays protected.
    assert (await w.book(w.body(phone=OTHER_PHONE)))["detail"]["reason"] == "taken"
    w.clock.advance(STALE)
    resp = await w.book(w.body(phone=OTHER_PHONE))
    assert resp["status"] == "booked"
    assert (w.last_ctx.diagnostic, w.last_ctx.reason) == ("stale_claim_recovered", "stale_released")
    assert len(w.events()) == 1
    assert_no_overlapping_bookings(w)


async def test_f15_owner_died_after_create_then_the_identical_retry_replays() -> None:
    w = World()
    gate = w.hooks.gate("before", "replace", w.cell(SLOT))  # created, not finalized
    await _die(w, asyncio.create_task(w.book()), gate)
    [event] = w.events()
    w.clock.advance(STALE)
    # Another contact cannot take it (busy time and the recovered claims).
    assert (await w.book(w.body(phone=OTHER_PHONE)))["detail"]["reason"] == "taken"
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert {(r.state, r.event_id) for r in w.records().values()} == {("booked", event.event_id)}
    assert w.provider.calls.count("create_event") == 1
    assert_no_overlapping_bookings(w)


async def test_f15_stale_cells_finalized_by_another_contacts_recovery() -> None:
    """The owner created and died; the event is hidden from busy time (e.g. a
    transparent copy) so another contact reaches step 6: recovery finalizes
    the owner's cells and the other contact gets `taken`, never a double booking."""
    w = World()
    gate = w.hooks.gate("before", "replace", w.cell(SLOT))
    await _die(w, asyncio.create_task(w.book()), gate)
    [event] = w.events()
    event.transparency = "transparent"
    w.clock.advance(STALE)
    resp = await w.book(w.body(phone=OTHER_PHONE))
    assert resp == {"status": "slot_unavailable", "detail": {"reason": "taken"}}
    assert {(r.state, r.event_id) for r in w.records().values()} == {("booked", event.event_id)}
    assert len(w.events()) == 1


async def test_two_contacts_racing_to_recover_one_stale_slot_book_once() -> None:
    w = World()
    gate = w.hooks.gate("after", "try_claim", w.cell(SLOT + 5 * FIVE))
    await _die(w, asyncio.create_task(w.book(w.body(phone=PHONES[0]))), gate)
    w.clock.advance(STALE)
    results = await asyncio.gather(
        w.service.book(w.binding, w.body(phone=PHONES[1]), w.ctx()),
        w.service.book(w.binding, w.body(phone=PHONES[2]), w.ctx()),
    )
    assert sorted(r["status"] for r in results) == ["booked", "slot_unavailable"]
    assert len(w.events()) == 1
    assert_no_overlapping_bookings(w)


async def test_sf8_host_deletes_then_retry_is_cancelled_then_the_slot_books_again() -> None:
    w = World()
    first = await w.book()
    [event] = w.events()
    w.provider.cancel_event(w.binding.calendar_ref, event.event_id)
    assert await w.book() == {"status": "slot_unavailable", "detail": {"reason": "cancelled"}}
    again = await w.book()
    assert again["status"] == "booked" and again["replayed"] is False
    assert again["booking"]["booking_ref"] == first["booking"]["booking_ref"]
    assert len(w.events()) == 1


async def test_sf8_host_deletes_then_another_contact_books_the_freed_slot() -> None:
    """No retry released the cells: verification at step 6 does."""
    w = World()
    await w.book()
    [event] = w.events()
    w.provider.cancel_event(w.binding.calendar_ref, event.event_id)
    resp = await w.book(w.body(phone=OTHER_PHONE))
    assert resp["status"] == "booked"
    assert (w.last_ctx.diagnostic, w.last_ctx.reason) == ("stale_claim_recovered", "unverified_released")
    assert len(w.events()) == 1
    assert_no_overlapping_bookings(w)


async def test_sf7_host_moves_a_booking_then_the_retry_replays_the_moved_times() -> None:
    w = World()
    await w.book()
    [event] = w.events()
    event.start, event.end = SLOT + timedelta(hours=2), SLOT + timedelta(hours=2, minutes=30)
    # Another contact takes the old time: its cells fail verification (moved).
    other = await w.book(w.body(phone=OTHER_PHONE))
    assert other["status"] == "booked"
    # The identical retry: our first cell is no longer ours; step 3 finds the event.
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is True
    assert resp["booking"]["start"] == "2026-10-06T16:00:00-04:00"
    assert len(w.events()) == 2
    # The moved event no longer sits on its old claims, so only the events are compared.
    a, b = w.events()
    assert not (a.start < b.end and b.start < a.end)


async def test_claim_store_failure_before_create_is_unavailable_after_is_booked() -> None:
    """SF-3 / F14."""
    w = World()
    w.store.fail_next("try_claim")
    assert (await w.book())["status"] == "calendar_unavailable"
    assert w.events() == [] and w.records() == {}
    w.store.fail_next("replace")
    resp = await w.book()
    assert resp["status"] == "booked"
    assert (w.last_ctx.diagnostic, w.last_ctx.reason) == ("claim_finalize_failed", None)
    assert len(w.events()) == 1
