"""The step-7 uncertain-create path (spec 7.4 step 7, 7.5, F7; plan UC04c
`test_booking_uncertain_create.py`).

An uncertain create (`ProviderTimeout`, `ProviderUnavailable`, or any other
non-definitive `ProviderError`) runs `find_bookings([start, end), binding)` for
our fingerprint: found -> step 8; not found -> one retry, then check again;
more than one event with our fingerprint -> keep one, delete the rest
(`duplicate_event_removed`); still unconfirmed -> `booking_unconfirmed` with
the claims left `pending` (`create_unconfirmed`, alerted).
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from calendar_tools.core import booking
from calendar_tools.core.ports import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from tests.booking_world import World


@pytest.fixture
def w() -> World:
    return World()


def logged(w: World) -> tuple[str | None, str | None]:
    return w.last_ctx.diagnostic, w.last_ctx.reason


def committed_then(w: World, exc: ProviderError, *, hide: int = 0, dedupe: bool = True):
    """A create that reaches the vendor and commits, but whose answer is lost.
    `hide` keeps it out of the next N `find_bookings`; `dedupe=False` defeats
    the vendor's request-key dedupe (a later retry makes a second event)."""
    real = w.provider.create_event

    async def create(cal, event):
        if not dedupe:
            event = dataclasses.replace(event, request_key="f" * 64)
        created = await real(cal, event)
        w.provider._store(cal)[created.event_id].hidden_reads = hide
        raise exc

    return create


UNCERTAIN = [
    ProviderTimeout(maybe_committed=True),
    ProviderTimeout(maybe_committed=False),
    ProviderUnavailable("http_503"),
    ProviderError("vendor_other"),
]


@pytest.mark.parametrize("exc", UNCERTAIN, ids=lambda e: type(e).__name__ + str(getattr(e, "maybe_committed", "")))
async def test_uncertain_create_that_committed_is_found_and_finalized(w, monkeypatch, exc) -> None:
    monkeypatch.setattr(w.provider, "create_event", committed_then(w, exc))
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is False
    assert logged(w) == (None, None)
    [event] = w.events()
    assert {(r.state, r.event_id) for r in w.records().values()} == {("booked", event.event_id)}


async def test_lookup_empty_then_one_retry_succeeds(w) -> None:
    w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is False
    assert w.provider.calls.count("create_event") == 2
    assert len(w.events()) == 1
    assert {r.state for r in w.records().values()} == {"booked"}


async def test_retry_also_uncertain_is_unconfirmed_and_claims_stay_pending(w) -> None:
    w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
    w.provider.fail_next("create_event", ProviderUnavailable("http_503"))
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "provider_unavailable")
    records = w.records()
    assert len(records) == 6 and {r.state for r in records.values()} == {"pending"}
    assert w.provider.calls.count("create_event") == 2  # exactly one retry
    # lookup after the first create, and again after the retry
    assert w.provider.calls.count("find_bookings") == 1 + 2


async def test_retry_uncertain_but_committed_is_found_by_the_second_check(w, monkeypatch) -> None:
    w.provider.fail_next("create_event", ProviderUnavailable("http_503"))
    calls = {"n": 0}
    fail_then_commit = committed_then(w, ProviderTimeout(maybe_committed=True))
    real = w.provider.create_event

    async def create(cal, event):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(cal, event)  # raises the queued 503, commits nothing
        return await fail_then_commit(cal, event)

    monkeypatch.setattr(w.provider, "create_event", create)
    resp = await w.book()
    assert resp["status"] == "booked"
    assert len(w.events()) == 1 and {r.state for r in w.records().values()} == {"booked"}


@pytest.mark.parametrize("exc", [ProviderAuthError("consent_revoked"), ProviderConfigError("insufficient_scope")])
async def test_definitive_refusal_on_the_retry_never_releases(w, exc) -> None:
    """The first create may have committed: a refusal of the retry proves
    nothing about it, so the claims stay pending and the answer is unknown."""
    w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
    w.provider.fail_next("create_event", exc)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "provider_timeout")
    assert {r.state for r in w.records().values()} == {"pending"} and len(w.records()) == 6


async def test_lookup_failure_is_unconfirmed_without_a_retry(w, monkeypatch) -> None:
    w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
    real = w.provider.find_bookings
    seen = {"n": 0}

    async def find(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] == 2:  # the step-7 lookup (the first is step 3's)
            raise ProviderUnavailable("http_503")
        return await real(*args, **kwargs)

    monkeypatch.setattr(w.provider, "find_bookings", find)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "lookup_failed")
    assert w.provider.calls.count("create_event") == 1
    assert {r.state for r in w.records().values()} == {"pending"} and len(w.records()) == 6


async def test_vendor_duplicate_is_removed_keeping_one(w, monkeypatch) -> None:
    """The first create committed but stayed invisible to the first lookup and
    defeated the vendor's dedupe; the retry made a second event. The check
    after the retry finds both: keep one, delete the other."""
    first = committed_then(w, ProviderTimeout(maybe_committed=True), hide=1, dedupe=False)
    real = w.provider.create_event
    calls = {"n": 0}

    async def create(cal, event):
        calls["n"] += 1
        return await (first if calls["n"] == 1 else real)(cal, event)

    monkeypatch.setattr(w.provider, "create_event", create)
    resp = await w.book()
    assert resp["status"] == "booked" and resp["replayed"] is False
    assert logged(w) == ("duplicate_event_removed", None)
    [kept] = w.events()
    assert w.provider.calls.count("delete_event") == 1
    assert {(r.state, r.event_id) for r in w.records().values()} == {("booked", kept.event_id)}


async def test_two_events_found_by_the_first_lookup_keep_one(w, monkeypatch) -> None:
    real = w.provider.create_event

    async def create(cal, event):
        await real(cal, dataclasses.replace(event, request_key="e" * 64))
        await real(cal, event)  # a vendor-side double write
        raise ProviderTimeout(maybe_committed=True)

    monkeypatch.setattr(w.provider, "create_event", create)
    resp = await w.book()
    assert resp["status"] == "booked"
    assert logged(w) == ("duplicate_event_removed", None)
    [kept] = w.events()
    assert {r.event_id for r in w.records().values()} == {kept.event_id}


async def test_failed_duplicate_delete_is_still_booked_with_a_reason(w, monkeypatch) -> None:
    real = w.provider.create_event

    async def create(cal, event):
        await real(cal, dataclasses.replace(event, request_key="e" * 64))
        await real(cal, event)
        raise ProviderTimeout(maybe_committed=True)

    monkeypatch.setattr(w.provider, "create_event", create)
    w.provider.fail_next("delete_event", ProviderUnavailable("http_503"))
    resp = await w.book()
    assert resp["status"] == "booked"
    assert logged(w) == ("duplicate_event_removed", "delete_failed")
    assert len(w.events()) == 2


async def test_deadline_expiry_after_a_confirmed_create_is_booked(w, monkeypatch) -> None:
    real = w.provider.create_event

    async def create(cal, event):
        await real(cal, event)
        raise ProviderTimeout(maybe_committed=True)

    real_find = w.provider.find_bookings
    seen = {"n": 0}

    async def find(*args, **kwargs):
        seen["n"] += 1
        result = await real_find(*args, **kwargs)
        if seen["n"] == 2:  # the step-7 lookup found it; then the budget runs out
            w.clock.advance(8.0)
        return result

    monkeypatch.setattr(w.provider, "create_event", create)
    monkeypatch.setattr(w.provider, "find_bookings", find)
    resp = await w.book()
    assert resp["status"] == "booked"
    assert logged(w) == ("claim_finalize_failed", None)


async def test_deadline_expiry_after_an_uncertain_create_is_unconfirmed(w, monkeypatch) -> None:
    async def create(cal, event):
        w.clock.advance(8.0)
        raise ProviderTimeout(maybe_committed=True)

    monkeypatch.setattr(w.provider, "create_event", create)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "lookup_failed")
    assert {r.state for r in w.records().values()} == {"pending"}


async def test_create_slower_than_its_timeout_every_time_is_unconfirmed(w, monkeypatch) -> None:
    async def slow(*args):
        await asyncio.sleep(5)

    monkeypatch.setattr(booking, "PROVIDER_TIMEOUT", 0.01)
    monkeypatch.setattr(w.provider, "create_event", slow)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    assert logged(w) == ("create_unconfirmed", "provider_timeout")
    assert {r.state for r in w.records().values()} == {"pending"}


async def test_unconfirmed_then_identical_retry_resolves_via_the_sf7_lookup(w, monkeypatch) -> None:
    """The create committed but stayed invisible to both checks, and the retry
    failed; the next identical retry finds the event at step 2 (SF-7)."""
    commit = committed_then(w, ProviderTimeout(maybe_committed=True), hide=2)
    calls = {"n": 0}

    async def create(cal, event):
        calls["n"] += 1
        if calls["n"] == 1:
            return await commit(cal, event)
        raise ProviderTimeout(maybe_committed=True)

    monkeypatch.setattr(w.provider, "create_event", create)
    resp = await w.book()
    assert resp == {"status": "booking_unconfirmed"}
    monkeypatch.undo()
    retry = await w.book()
    assert retry["status"] == "booked" and retry["replayed"] is True
    assert len(w.events()) == 1
    assert {r.state for r in w.records().values()} == {"booked"}
