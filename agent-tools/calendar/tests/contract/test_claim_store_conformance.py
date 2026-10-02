"""Claim-store conformance suite (spec 7.4 "The claim store"; plan UC04a).

Parametrized over every `ClaimStore` through a small harness: `store`, a way
to advance the store's **server** clock, and a way to inject one failure.
UC06 appends "azure_blob" (respx-mocked Blob REST) to STORES.

A new claim store is done when this suite passes, not when its own tests pass.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from calendar_tools.core.claims import (
    ClaimRecord,
    Claimed,
    ClaimStore,
    ClaimStoreUnavailable,
    FakeClaimStore,
    Held,
    PreconditionFailed,
)
from calendar_tools.core.contact import Contact
from calendar_tools.core.identity import CellKey, calendar_key, cell_range, contact_tag, fingerprint
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FINGERPRINT_KEY

STORES = ["fake"]
OPERATIONS = ("try_claim", "replace", "release", "read")

K = FINGERPRINT_KEY
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
CAL_A = calendar_key(K, "fake", "cal-0001")
CAL_B = calendar_key(K, "fake", "cal-0002")
CONTACT = Contact(name="Jordan Example", phone="+16135550123", email="jordan@example.com")
TAG = contact_tag(K, CONTACT)


class FakeStoreHarness:
    def __init__(self) -> None:
        self.server_clock = FakeClock(T0 - timedelta(days=1))
        self.store = FakeClaimStore(self.server_clock)

    def advance_server(self, seconds: float) -> None:
        self.server_clock.advance(seconds)

    def inject(self, operation: str) -> None:
        self.store.fail_next(operation)

    def plant_raw(self, cell: CellKey, data: bytes) -> None:
        self.store.plant_raw(cell, data)


HARNESSES = {"fake": FakeStoreHarness}


@pytest.fixture(params=STORES)
def h(request):
    return HARNESSES[request.param]()


def record(
    cal: str = CAL_A,
    start: datetime = T0,
    minutes: int = 30,
    *,
    attempt: str = "a1" * 8,
    appointment_type: str = "phone_call",
    binding_id: str = "test-alpha",
) -> ClaimRecord:
    end = start + timedelta(minutes=minutes)
    fp = fingerprint(K, cal, start, end, TAG, appointment_type)
    return ClaimRecord.pending(
        calendar_key=cal,
        start=start,
        end=end,
        buffer_minutes=0,
        fingerprint=fp,
        attempt_id=attempt,
        contact_tag=TAG,
        binding_id=binding_id,
    )


def key(cal: str = CAL_A, start: datetime = T0) -> CellKey:
    return CellKey(cal, start)


async def test_is_a_claim_store(h):
    assert isinstance(h.store, ClaimStore)


async def test_try_claim_on_empty_cell_is_claimed(h):
    result = await h.store.try_claim(key(), record())
    assert isinstance(result, Claimed)
    assert isinstance(result.etag, str) and result.etag


async def test_second_try_claim_is_held_with_the_first_record(h):
    first = record(attempt="a1" * 8)
    claimed = await h.store.try_claim(key(), first)
    result = await h.store.try_claim(key(), record(attempt="b2" * 8))
    assert isinstance(result, Held)
    assert result.record == first
    assert result.etag == claimed.etag
    assert result.age >= timedelta(0)


async def test_read_of_an_empty_cell_is_none(h):
    assert await h.store.read(key()) is None


async def test_read_returns_record_etag_and_age(h):
    rec = record()
    claimed = await h.store.try_claim(key(), rec)
    h.advance_server(7)
    held = await h.store.read(key())
    assert isinstance(held, Held)
    assert (held.record, held.etag, held.age) == (rec, claimed.etag, timedelta(seconds=7))


async def test_replace_with_the_right_etag_succeeds(h):
    claimed = await h.store.try_claim(key(), record())
    booked = record().booked("evt-0001")
    new_etag = await h.store.replace(key(), booked, claimed.etag)
    assert isinstance(new_etag, str) and new_etag != claimed.etag
    held = await h.store.read(key())
    assert held is not None and held.record == booked and held.etag == new_etag


async def test_replace_with_a_wrong_etag_is_precondition_failed(h):
    claimed = await h.store.try_claim(key(), record())
    await h.store.replace(key(), record().booked("evt-0001"), claimed.etag)
    with pytest.raises(PreconditionFailed):
        await h.store.replace(key(), record().booked("evt-0002"), claimed.etag)  # stale etag
    held = await h.store.read(key())
    assert held is not None and held.record.event_id == "evt-0001"


async def test_replace_of_an_empty_cell_is_precondition_failed(h):
    with pytest.raises(PreconditionFailed):
        await h.store.replace(key(), record(), '"no-such-etag"')
    assert await h.store.read(key()) is None


async def test_release_with_the_right_etag_deletes(h):
    claimed = await h.store.try_claim(key(), record())
    await h.store.release(key(), claimed.etag)
    assert await h.store.read(key()) is None
    again = await h.store.try_claim(key(), record(attempt="c3" * 8))
    assert isinstance(again, Claimed)


async def test_release_with_a_wrong_etag_is_precondition_failed(h):
    claimed = await h.store.try_claim(key(), record())
    new_etag = await h.store.replace(key(), record().booked("evt-0001"), claimed.etag)
    with pytest.raises(PreconditionFailed):
        await h.store.release(key(), claimed.etag)
    held = await h.store.read(key())
    assert held is not None and held.etag == new_etag


async def test_release_of_an_empty_cell_is_a_no_op(h):
    # Plan UC06: "404 counts as released". Safe: a cell claimed again since
    # carries a new etag, so a stale release still fails its precondition.
    await h.store.release(key(), '"no-such-etag"')
    assert await h.store.read(key()) is None


async def test_release_after_a_reclaim_with_the_old_etag_is_precondition_failed(h):
    first = await h.store.try_claim(key(), record())
    await h.store.release(key(), first.etag)
    second = await h.store.try_claim(key(), record(attempt="f6" * 8))
    with pytest.raises(PreconditionFailed):
        await h.store.release(key(), first.etag)
    held = await h.store.read(key())
    assert held is not None and held.etag == second.etag


async def test_an_etag_is_valid_only_on_its_own_cell(h):
    a = await h.store.try_claim(key(start=T0), record())
    await h.store.try_claim(key(start=T0 + timedelta(minutes=5)), record())
    with pytest.raises(PreconditionFailed):
        await h.store.release(key(start=T0 + timedelta(minutes=5)), a.etag)


async def test_same_cell_time_on_two_calendar_keys_is_independent(h):
    a = await h.store.try_claim(key(CAL_A), record(CAL_A))
    b = await h.store.try_claim(key(CAL_B), record(CAL_B))
    assert isinstance(a, Claimed) and isinstance(b, Claimed)
    await h.store.release(key(CAL_A), a.etag)
    held = await h.store.read(key(CAL_B))
    assert held is not None and held.record.calendar_key == CAL_B


async def test_adjacent_cells_are_independent(h):
    rec = record()
    for cell in cell_range(rec.start, rec.end, rec.buffer_minutes):
        assert isinstance(await h.store.try_claim(key(start=cell), rec), Claimed)


async def test_age_is_server_time_only(h):
    # Round-3 SF-1: the caller's clock plays no part, so skew can't make a live
    # claim look stale. The store is never given the caller's clock at all; for
    # the fake this guards the contract, and a harness for a real store (UC06's
    # emulator) must drive its Date/Last-Modified from `advance_server` only.
    caller_clock = FakeClock(T0)
    await h.store.try_claim(key(), record())
    h.advance_server(30)
    baseline = (await h.store.read(key())).age
    assert baseline == timedelta(seconds=30)
    for skew in (timedelta(minutes=10), timedelta(minutes=-10)):
        caller_clock.set(T0 + skew)
        assert (await h.store.read(key())).age == baseline
        held = await h.store.try_claim(key(), record(attempt="d4" * 8))
        assert isinstance(held, Held) and held.age == baseline


async def test_replace_resets_age(h):
    claimed = await h.store.try_claim(key(), record())
    h.advance_server(200)
    await h.store.replace(key(), record().booked("evt-0001"), claimed.etag)
    h.advance_server(3)
    assert (await h.store.read(key())).age == timedelta(seconds=3)


async def test_fifty_concurrent_try_claims_on_one_cell_yield_exactly_one_claimed(h):
    records = [record(attempt=f"{i:016x}") for i in range(50)]
    results = await asyncio.gather(*(h.store.try_claim(key(), r) for r in records))
    claimed = [i for i, r in enumerate(results) if isinstance(r, Claimed)]
    assert len(claimed) == 1
    winner = records[claimed[0]]
    held = [r for r in results if isinstance(r, Held)]
    assert len(held) == 49
    assert all(r.record == winner for r in held)
    final = await h.store.read(key())
    assert final is not None and final.record == winner


async def test_record_must_belong_to_the_cell(h):
    with pytest.raises(ValueError):
        await h.store.try_claim(key(CAL_B), record(CAL_A))  # other calendar
    with pytest.raises(ValueError):
        await h.store.try_claim(key(start=T0 + timedelta(hours=2)), record())  # outside the range
    claimed = await h.store.try_claim(key(), record())
    with pytest.raises(ValueError):
        await h.store.replace(key(), record(CAL_B), claimed.etag)
    assert await h.store.read(key(CAL_B)) is None


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_injected_failure_raises_claim_store_unavailable_and_changes_nothing(h, operation):
    claimed = await h.store.try_claim(key(), record())
    h.inject(operation)
    call = {
        "try_claim": lambda: h.store.try_claim(key(), record(attempt="e5" * 8)),
        "replace": lambda: h.store.replace(key(), record().booked("evt-0001"), claimed.etag),
        "release": lambda: h.store.release(key(), claimed.etag),
        "read": lambda: h.store.read(key()),
    }[operation]
    with pytest.raises(ClaimStoreUnavailable) as info:
        await call()
    assert info.value.diagnostic == "claim_store_unreachable"
    held = await h.store.read(key())
    assert held is not None and held.etag == claimed.etag and held.record == record()
    await call()  # one-shot


@pytest.mark.parametrize("operation", ["read", "try_claim"])
@pytest.mark.parametrize("data", [b"not json", b"{}", b'{"v": 2}', b"\xff\xfe"], ids=["text", "empty", "v2", "bytes"])
async def test_a_corrupt_stored_record_is_unavailable_never_empty(h, operation, data):
    h.plant_raw(key(), data)
    call = {
        "read": lambda: h.store.read(key()),
        "try_claim": lambda: h.store.try_claim(key(), record()),
    }[operation]
    with pytest.raises(ClaimStoreUnavailable) as info:
        await call()
    assert info.value.reason == "record_invalid"


async def test_record_round_trip_holds_no_contact_pii(h):
    rec = record()
    await h.store.try_claim(key(), rec)
    held = await h.store.read(key())
    assert held is not None and held.record == rec
    raw = rec.to_json()
    doc = json.loads(raw)
    assert ClaimRecord.from_json(raw) == rec
    assert not {"name", "phone", "email", "contact", "notes"} & set(doc)
    text = raw.decode("utf-8")
    for pii in ("6135550123", "Jordan", "jordan@example.com", "example.com"):
        assert pii not in text
