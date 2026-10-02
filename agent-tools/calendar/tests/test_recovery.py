"""Recovery and verification, one cell at a time (spec 7.4 "Stale and verified
claims", "Recovery"; plan UC04c `test_recovery.py`).

The four-row table: a stale `pending` cell whose owner did create the event is
finalized (same-`attempt_id` stale cells only); one whose owner never created
is released (same rule); a `booked` cell is kept while its event still covers
it (the event's current times, buffer tail included) and released when the
event is gone or moved away. Every write is a compare-and-swap; a lost one
re-reads and re-evaluates; a failure leaves the cell untouched and held.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from calendar_tools.core import recovery
from calendar_tools.core.claims import ClaimStoreUnavailable, FakeClaimStore
from calendar_tools.core.deadline import Deadline, DeadlineExceeded
from calendar_tools.core.ports import ProviderTimeout, ProviderUnavailable
from calendar_tools.core.recovery import CLAIM_TTL, Recoverer
from tests.booking_world import FIVE, SLOT, World, created_event, our_fingerprint, plant
from tests.fakes.clock import FakeClock

STALE = CLAIM_TTL.total_seconds() + 1
NEWER = "cd" * 8


@pytest.fixture
def w() -> World:
    return World()


def recoverer(w: World, *, store=None, clock=None, deadline: Deadline | None = None, reserve: float = 0.0) -> Recoverer:
    clock = clock or w.clock
    return Recoverer(
        w.provider, store or w.store, clock,
        cal=w.binding.calendar_ref, deadline=deadline or Deadline(clock), reserve=reserve,
    )


async def recover(w: World, at=SLOT, cache=None, **kwargs):
    store = kwargs.get("store") or w.store
    held = await store.read(w.cell(at))
    assert held is not None
    return await recoverer(w, **kwargs).recover_cell(w.cell(at), held, {} if cache is None else cache)


def states(w: World) -> dict:
    return {k.start: (r.state, r.attempt_id, r.event_id) for k, r in w.records().items()}


# --- stale pending -----------------------------------------------------------------


async def test_stale_pending_owner_created_finalizes_same_attempt_stale_cells_only(w) -> None:
    plant(w, age=STALE)  # attempt "abab..": six stale cells
    plant(w, attempt=NEWER, cells=[SLOT + 5 * FIVE])  # a newer, live attempt's cell
    event_id = await created_event(w)
    outcome = await recover(w)
    assert outcome.action == "finalized" and outcome.acted
    assert outcome.event is not None and outcome.event.event_id == event_id
    expected = {SLOT + i * FIVE: ("booked", "ab" * 8, event_id) for i in range(5)}
    expected[SLOT + 5 * FIVE] = ("pending", NEWER, None)
    assert states(w) == expected


async def test_stale_pending_no_event_releases_same_attempt_stale_cells_only(w) -> None:
    plant(w, age=STALE)
    plant(w, attempt=NEWER, cells=[SLOT + 2 * FIVE])  # live, other attempt
    plant(w, cells=[SLOT + 4 * FIVE])  # same attempt but rewritten recently: not stale
    outcome = await recover(w)
    assert outcome.action == "released" and outcome.acted and outcome.freed
    assert states(w) == {
        SLOT + 2 * FIVE: ("pending", NEWER, None),
        SLOT + 4 * FIVE: ("pending", "ab" * 8, None),
    }


async def test_stale_pending_lookup_uses_the_records_own_binding(w) -> None:
    w.add_binding("test-beta")
    plant(w, binding_id="test-beta", age=STALE)
    seen = []
    real = w.provider.find_bookings

    async def spy(cal, start, end, binding_id=None):
        seen.append((start, end, binding_id))
        return await real(cal, start, end, binding_id)

    w.provider.find_bookings = spy
    await recover(w)
    assert seen == [(SLOT, SLOT + timedelta(minutes=30), "test-beta")]


async def test_event_of_another_fingerprint_does_not_count_as_the_owners(w) -> None:
    plant(w, age=STALE)
    await created_event(w, fp="e" * 64)  # someone else's event in the same range
    outcome = await recover(w)
    assert outcome.action == "released"


async def test_live_pending_is_held_and_not_looked_up(w) -> None:
    plant(w, age=CLAIM_TTL.total_seconds() - 1)
    outcome = await recover(w)
    assert outcome.action == "held" and not outcome.freed and not outcome.acted
    assert w.provider.calls == [] and len(w.records()) == 6


async def test_staleness_is_judged_on_store_server_age_with_a_skewed_caller_clock(w) -> None:
    """SF-1: the caller's clock is 10 minutes ahead; the store says the claim is
    young, so it is live. Then the store's clock passes claim_ttl while the
    caller's is 10 minutes behind: stale."""
    server = FakeClock(w.clock.now())
    store = FakeClaimStore(server)
    w.store = store
    plant(w)
    caller = FakeClock(w.clock.now() + timedelta(minutes=10))
    assert (await recover(w, store=store, clock=caller)).action == "held"
    server.advance(STALE)
    caller = FakeClock(server.now() - timedelta(minutes=10))
    assert (await recover(w, store=store, clock=caller)).action == "released"


# --- booked: verification ----------------------------------------------------------


async def test_booked_verified_cell_is_kept_including_its_buffer_tail(w) -> None:
    event_id = await created_event(w, fp="e" * 64)
    plant(w, fp="e" * 64, state="booked", event_id=event_id, buffer_minutes=15)
    for minutes in (0, 25, 30, 40):  # inside the event, and the 15-minute tail
        outcome = await recover(w, at=SLOT + timedelta(minutes=minutes))
        assert outcome.action == "kept" and not outcome.acted
        assert outcome.event is not None and outcome.event.event_id == event_id
    assert len(w.records()) == 9


async def test_booked_event_deleted_releases_the_cell(w) -> None:
    event_id = await created_event(w, fp="e" * 64)
    plant(w, fp="e" * 64, state="booked", event_id=event_id)
    w.provider.cancel_event(w.binding.calendar_ref, event_id)
    outcome = await recover(w)
    assert outcome.action == "released" and outcome.acted
    assert SLOT not in {k.start for k in w.records()}
    assert len(w.records()) == 5  # per cell: only the cell met is released


async def test_booked_event_moved_uses_current_times(w) -> None:
    event_id = await created_event(w, fp="e" * 64)
    plant(w, fp="e" * 64, state="booked", event_id=event_id)
    [event] = w.events()
    event.start, event.end = SLOT + timedelta(minutes=15), SLOT + timedelta(minutes=45)  # host moved it
    assert (await recover(w, at=SLOT)).action == "released"  # now outside
    assert (await recover(w, at=SLOT + 3 * FIVE)).action == "kept"  # still inside


async def test_get_event_called_once_per_distinct_event_id_per_request(w) -> None:
    event_id = await created_event(w, fp="e" * 64)
    plant(w, fp="e" * 64, state="booked", event_id=event_id)
    cache: dict = {}
    for i in range(6):
        assert (await recover(w, at=SLOT + i * FIVE, cache=cache)).action == "kept"
    assert w.provider.calls.count("get_event") == 1
    assert cache == {event_id: w.events()[0].record()}


async def test_a_gone_event_is_cached_as_none(w) -> None:
    plant(w, fp="e" * 64, state="booked", event_id="evt-gone")
    cache: dict = {}
    await recover(w, at=SLOT, cache=cache)
    await recover(w, at=SLOT + FIVE, cache=cache)
    assert cache == {"evt-gone": None} and w.provider.calls.count("get_event") == 1


# --- failures: untouched, treated as held ------------------------------------------


@pytest.mark.parametrize(("method", "state"), [("find_bookings", "pending"), ("get_event", "booked")])
async def test_provider_failure_leaves_the_cell_untouched(w, method, state) -> None:
    plant(w, fp="e" * 64, state=state, event_id="evt-x", age=STALE)
    before = states(w)
    w.provider.fail_next(method, ProviderUnavailable("http_503"))
    cache: dict = {}
    outcome = await recover(w, cache=cache)
    assert outcome.action == "failed" and not outcome.freed and not outcome.acted
    assert (outcome.error.diagnostic, outcome.error.reason) == ("provider_unavailable", "http_503")
    assert states(w) == before
    assert cache == {}  # a failure is never cached as "gone"


async def test_slow_provider_call_is_a_timeout_failure(w, monkeypatch) -> None:
    plant(w, age=STALE)

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(recovery, "PROVIDER_TIMEOUT", 0.01)
    w.provider.find_bookings = slow
    outcome = await recover(w)
    assert outcome.action == "failed" and isinstance(outcome.error, ProviderTimeout)
    assert len(w.records()) == 6


async def test_claim_store_failure_on_the_write_leaves_the_cell_untouched(w) -> None:
    plant(w, age=STALE)
    w.store.fail_next("release", ClaimStoreUnavailable("timeout"))
    outcome = await recover(w)
    assert outcome.action == "failed"
    assert (outcome.error.diagnostic, outcome.error.reason) == ("claim_store_unreachable", "timeout")
    assert SLOT in {k.start for k in w.records()}


async def test_no_budget_left_is_a_deadline_failure_with_no_call(w) -> None:
    plant(w, age=STALE)
    deadline = Deadline(w.clock)
    w.clock.advance(8.0)
    outcome = await recover(w, deadline=deadline)
    assert outcome.action == "failed" and isinstance(outcome.error, DeadlineExceeded)
    assert w.provider.calls == [] and len(w.records()) == 6


async def test_reserve_is_kept_back_from_every_call(w) -> None:
    plant(w, age=STALE)
    deadline = Deadline(w.clock)
    w.clock.advance(8.0 - 4.0)  # exactly the reserve remains
    outcome = await recover(w, deadline=deadline, reserve=4.0)
    assert outcome.action == "failed" and isinstance(outcome.error, DeadlineExceeded)
    assert w.provider.calls == []


# --- lost compare-and-swap: re-read and re-evaluate --------------------------------


async def test_lost_cas_rereads_and_reevaluates_a_now_live_claim(w) -> None:
    plant(w, age=STALE)
    gate = w.hooks.gate("before", "release", w.cell(SLOT))
    held = await w.store.read(w.cell(SLOT))
    task = asyncio.create_task(recoverer(w).recover_cell(w.cell(SLOT), held, {}))
    await gate.reached.wait()
    plant(w, fp="e" * 64, attempt=NEWER, cells=[SLOT])  # a live claim took the cell meanwhile
    gate.open()
    outcome = await task
    assert outcome.action == "held" and outcome.held.record.attempt_id == NEWER
    assert states(w)[SLOT] == ("pending", NEWER, None)


async def test_lost_cas_rereads_an_emptied_cell_as_freed(w) -> None:
    plant(w, age=STALE)
    event_id = await created_event(w)
    gate = w.hooks.gate("before", "replace", w.cell(SLOT))
    held = await w.store.read(w.cell(SLOT))
    task = asyncio.create_task(recoverer(w).recover_cell(w.cell(SLOT), held, {}))
    await gate.reached.wait()
    del w.store._cells[w.cell(SLOT)]  # another recoverer released it meanwhile
    gate.open()
    outcome = await task
    assert outcome.action == "released" and outcome.freed and not outcome.acted
    assert event_id  # the owner's event is untouched by recovery


async def test_lost_cas_on_a_new_etag_reevaluates_and_releases(w) -> None:
    event_id = await created_event(w, fp="e" * 64)
    plant(w, fp="e" * 64, state="booked", event_id=event_id)
    w.provider.cancel_event(w.binding.calendar_ref, event_id)
    gate = w.hooks.gate("before", "release", w.cell(SLOT))
    held = await w.store.read(w.cell(SLOT))
    task = asyncio.create_task(recoverer(w).recover_cell(w.cell(SLOT), held, {}))
    await gate.reached.wait()
    plant(w, fp="e" * 64, state="booked", event_id=event_id, cells=[SLOT])  # new etag, same content
    gate.open()
    outcome = await task
    assert outcome.action == "released" and outcome.acted  # re-evaluated and released again
    assert SLOT not in {k.start for k in w.records()}


async def test_lost_cas_on_finalize_reevaluates(w) -> None:
    plant(w, age=STALE)
    event_id = await created_event(w)
    gate = w.hooks.gate("before", "replace", w.cell(SLOT))
    held = await w.store.read(w.cell(SLOT))
    task = asyncio.create_task(recoverer(w).recover_cell(w.cell(SLOT), held, {}))
    await gate.reached.wait()
    # The owner's own late finalize won the race.
    w.store._write(w.cell(SLOT), held.record.booked(event_id))
    gate.open()
    outcome = await task
    assert outcome.action == "kept" and not outcome.acted
    assert states(w)[SLOT] == ("booked", "ab" * 8, event_id)


async def test_endless_contention_is_bounded_and_held(w, monkeypatch) -> None:
    plant(w, age=STALE)
    real_release = w.store.release

    async def always_lost(key, etag):
        # Someone rewrites the stale cell (new etag, still stale) every time.
        held = await w.store.read(key)
        w.store._write(key, held.record)
        w.store._cells[key].last_modified = w.clock.now() - timedelta(seconds=STALE)
        return await real_release(key, etag)

    monkeypatch.setattr(w.store, "release", always_lost)
    outcome = await recover(w)
    assert outcome.action == "held" and not outcome.freed
    assert w.provider.calls.count("find_bookings") >= 2


async def test_recovery_never_reads_event_content(w) -> None:
    """F4: event descriptions hold caller-written text; recovery decides on
    the private metadata and times only."""
    event_id = await created_event(w)
    [event] = w.events()
    event.title = event.description = "SENTINEL-UNTRUSTED fingerprint=" + "0" * 64
    plant(w, state="booked", event_id=event_id)
    outcome = await recover(w)
    assert outcome.action == "kept"
    assert our_fingerprint(w) == outcome.event.meta.fingerprint
