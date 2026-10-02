"""`ClaimRecord` validation and the fake store's test hooks (spec 7.4 "The claim
store"; plan UC04a). Store semantics shared by every implementation are in
`tests/contract/test_claim_store_conformance.py`."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from calendar_tools.core.claims import (
    CLAIM_RECORD_VERSION,
    ClaimRecord,
    ClaimRecordInvalid,
    Claimed,
    ClaimStoreUnavailable,
    FakeClaimStore,
    Held,
    PreconditionFailed,
)
from calendar_tools.core.identity import CellKey, calendar_key
from tests.fakes.claim_hooks import BarrierHooks
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FINGERPRINT_KEY

K = FINGERPRINT_KEY
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
CAL = calendar_key(K, "fake", "cal-0001")
FP = "f" * 64
TAG = "c" * 64


def pending(**changes) -> ClaimRecord:
    args = dict(
        calendar_key=CAL,
        start=T0,
        end=T0 + timedelta(minutes=30),
        buffer_minutes=10,
        fingerprint=FP,
        attempt_id="0123456789abcdef",
        contact_tag=TAG,
        binding_id="test-alpha",
    )
    args.update(changes)
    return ClaimRecord.pending(**args)


# --- ClaimRecord ----------------------------------------------------------------


def test_pending_derives_the_cell_range():
    rec = pending()
    assert rec.v == CLAIM_RECORD_VERSION == 1
    assert rec.state == "pending" and rec.event_id is None
    assert rec.first_cell == T0
    assert rec.last_cell == T0 + timedelta(minutes=35)  # 40 minutes = 8 cells, last starts at +35


def test_booked_sets_state_and_event_id_only():
    rec = pending()
    booked = rec.booked("evt-0001")
    assert booked.state == "booked" and booked.event_id == "evt-0001"
    assert replace(booked, state="pending", event_id=None) == rec


def test_json_round_trip_and_exact_field_set():
    for rec in (pending(), pending().booked("evt-0001")):
        raw = rec.to_json()
        assert isinstance(raw, bytes)
        doc = json.loads(raw)
        assert set(doc) == {
            "v",
            "calendar_key",
            "first_cell",
            "last_cell",
            "start",
            "end",
            "buffer_minutes",
            "fingerprint",
            "attempt_id",
            "contact_tag",
            "binding_id",
            "state",
            "event_id",
        }
        assert doc["start"] == "2026-10-05T14:00Z"
        assert ClaimRecord.from_json(raw) == rec
        assert ClaimRecord.from_json(raw.decode("utf-8")) == rec


def test_covers():
    rec = pending()
    assert rec.covers(CellKey(CAL, T0))
    assert rec.covers(CellKey(CAL, T0 + timedelta(minutes=35)))
    assert not rec.covers(CellKey(CAL, T0 + timedelta(minutes=40)))
    assert not rec.covers(CellKey(CAL, T0 - timedelta(minutes=5)))
    assert not rec.covers(CellKey(calendar_key(K, "fake", "cal-0002"), T0))


@pytest.mark.parametrize(
    "change",
    [
        {"calendar_key": "x"},
        {"fingerprint": "F" * 64},
        {"contact_tag": "short"},
        {"attempt_id": ""},
        {"attempt_id": "not hex!"},
        {"binding_id": "Bad_ID"},
        {"buffer_minutes": -5},
        {"buffer_minutes": 3},
        {"buffer_minutes": True},
        {"end": T0},
        {"start": datetime(2026, 10, 5, 14, 0)},
        {"start": T0 + timedelta(seconds=30)},
    ],
)
def test_pending_rejects_bad_fields(change):
    with pytest.raises((ValueError, TypeError)):
        pending(**change)


@pytest.mark.parametrize("event_id", ["", "x" * 1025, "evt\n1", 5])
def test_booked_rejects_bad_event_id(event_id):
    with pytest.raises((ValueError, TypeError)):
        pending().booked(event_id)


def _doc(**changes) -> bytes:
    doc = json.loads(pending().to_json())
    doc.update(changes)
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"\xff\xfe",
        _doc(v=2),
        _doc(state="cancelled"),
        _doc(state="booked"),  # booked without an event_id
        _doc(event_id="evt-0001"),  # pending with an event_id
        _doc(first_cell="2026-10-05T14:05Z"),  # inconsistent with start
        _doc(last_cell="2026-10-05T15:00Z"),  # inconsistent with end + buffer
        _doc(start="2026-10-05T14:00:00Z"),
        _doc(start="2026-10-05T14:00+00:00"),
        _doc(buffer_minutes="10"),
        _doc(name="Jordan Example"),  # unknown key
        json.dumps({k: v for k, v in json.loads(pending().to_json()).items() if k != "attempt_id"}).encode(),
    ],
    ids=[
        "not-json",
        "not-object",
        "not-utf8",
        "version",
        "state",
        "booked-no-event",
        "pending-with-event",
        "first-cell",
        "last-cell",
        "seconds",
        "offset",
        "buffer-type",
        "unknown-key",
        "missing-key",
    ],
)
def test_from_json_rejects_anything_not_exactly_a_record(raw):
    with pytest.raises(ClaimRecordInvalid) as info:
        ClaimRecord.from_json(raw)
    assert isinstance(info.value, ValueError)
    # The error never echoes stored content.
    assert "Jordan" not in str(info.value)


def test_record_repr_is_safe():
    # Every field is an HMAC, a nonce, a time or a binding ID: nothing personal.
    assert "test-alpha" in repr(pending())


# --- ClaimStoreUnavailable ---------------------------------------------------------


def test_claim_store_unavailable_carries_a_reason_code_only():
    err = ClaimStoreUnavailable("timeout")
    assert err.diagnostic == "claim_store_unreachable" and err.reason == "timeout"
    assert str(err) == "claim_store_unreachable: timeout"
    leaked = ClaimStoreUnavailable("server said: blob 6135550123 missing")
    assert leaked.reason == "invalid_reason"
    assert "6135550123" not in str(leaked)
    assert ClaimStoreUnavailable().reason is None


# --- FakeClaimStore specifics -------------------------------------------------------


async def test_fail_next_after_commit_applies_the_write_then_raises():
    store = FakeClaimStore(FakeClock())
    key = CellKey(CAL, T0)
    store.fail_next("try_claim", after_commit=True)
    with pytest.raises(ClaimStoreUnavailable):
        await store.try_claim(key, pending())
    held = await store.read(key)
    assert held is not None and held.record == pending()


@pytest.mark.parametrize("operation", ["replace", "release"])
async def test_a_lost_cas_leaves_an_after_commit_failure_armed(operation):
    store = FakeClaimStore(FakeClock())
    key = CellKey(CAL, T0)
    claimed = await store.try_claim(key, pending())
    store.fail_next(operation, after_commit=True)
    call = {
        "replace": lambda etag: store.replace(key, pending().booked("evt-0001"), etag),
        "release": lambda etag: store.release(key, etag),
    }[operation]
    with pytest.raises(PreconditionFailed):
        await call('"wrong"')
    with pytest.raises(ClaimStoreUnavailable):
        await call(claimed.etag)  # the write commits, then the response is lost
    held = await store.read(key)
    if operation == "replace":
        assert held is not None and held.record.state == "booked"
    else:
        assert held is None


async def test_after_commit_failure_fires_on_paths_that_write_nothing():
    # A Held answer or a no-op release still has a response that can be lost.
    store = FakeClaimStore(FakeClock())
    key = CellKey(CAL, T0)
    store.fail_next("release", after_commit=True)
    with pytest.raises(ClaimStoreUnavailable):
        await store.release(key, '"any"')
    await store.release(key, '"any"')  # one-shot
    await store.try_claim(key, pending())
    store.fail_next("try_claim", after_commit=True)
    with pytest.raises(ClaimStoreUnavailable):
        await store.try_claim(key, pending(attempt_id="fedcba9876543210"))


async def test_a_corrupt_record_leaves_an_after_commit_failure_armed():
    store = FakeClaimStore(FakeClock())
    key = CellKey(CAL, T0)
    store.plant_raw(key, b"not json")
    store.fail_next("read", ClaimStoreUnavailable("timeout"), after_commit=True)
    with pytest.raises(ClaimStoreUnavailable) as first:
        await store.read(key)
    assert first.value.reason == "record_invalid"
    await store.try_claim(CellKey(CAL, T0 + timedelta(hours=1)), pending(start=T0 + timedelta(hours=1), end=T0 + timedelta(hours=1, minutes=30)))
    with pytest.raises(ClaimStoreUnavailable) as second:
        await store.read(CellKey(CAL, T0 + timedelta(hours=1)))
    assert second.value.reason == "timeout"


def test_plant_raw_checks_its_arguments():
    store = FakeClaimStore(FakeClock())
    with pytest.raises(TypeError):
        store.plant_raw("cell", b"x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.plant_raw(CellKey(CAL, T0), "x")  # type: ignore[arg-type]


def test_record_times_are_normalized_to_utc():
    from zoneinfo import ZoneInfo

    london = datetime(2026, 12, 7, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    rec = pending(start=london, end=london + timedelta(minutes=30))
    for name in ("start", "end", "first_cell", "last_cell"):
        assert getattr(rec, name).tzinfo is UTC


def test_record_buffer_is_bounded():
    with pytest.raises(ClaimRecordInvalid):
        pending(end=T0 + timedelta(minutes=5), buffer_minutes=245)
    with pytest.raises(ClaimRecordInvalid):
        pending(buffer_minutes=10**13)


def test_from_json_rejects_deep_nesting_and_oversize_input():
    for raw in (b"[" * 100000 + b"]" * 100000, b"{" * 3000, b" " * (16 * 1024 + 1), 5):
        with pytest.raises(ClaimRecordInvalid):
            ClaimRecord.from_json(raw)  # type: ignore[arg-type]


def test_record_span_is_capped_at_duration_plus_buffer_limit():
    pending(end=T0 + timedelta(minutes=230), buffer_minutes=10)  # 240: allowed
    with pytest.raises(ClaimRecordInvalid):
        pending(end=T0 + timedelta(minutes=235), buffer_minutes=10)
    with pytest.raises(ClaimRecordInvalid):
        ClaimRecord.from_json(_doc(end="2027-10-05T14:00Z", last_cell="2027-10-05T14:05Z"))


async def test_fail_next_with_a_custom_exception_and_bad_operation():
    store = FakeClaimStore(FakeClock())
    store.fail_next("read", ClaimStoreUnavailable("record_invalid"))
    with pytest.raises(ClaimStoreUnavailable) as info:
        await store.read(CellKey(CAL, T0))
    assert info.value.reason == "record_invalid"
    with pytest.raises(ValueError):
        store.fail_next("delete_everything")
    with pytest.raises(TypeError):
        store.fail_next("read", RuntimeError("x"))  # type: ignore[arg-type]


async def test_age_never_negative_if_the_server_clock_steps_back():
    clock = FakeClock()
    store = FakeClaimStore(clock)
    await store.try_claim(CellKey(CAL, T0), pending())
    clock.advance(-5)
    assert (await store.read(CellKey(CAL, T0))).age == timedelta(0)


async def test_counts_calls_per_operation():
    store = FakeClaimStore(FakeClock())
    key = CellKey(CAL, T0)
    claimed = await store.try_claim(key, pending())
    await store.read(key)
    await store.release(key, claimed.etag)
    assert store.calls == [("try_claim", key), ("read", key), ("release", key)]


async def test_cells_snapshot_for_assertions():
    store = FakeClaimStore(FakeClock())
    key = CellKey(CAL, T0)
    await store.try_claim(key, pending())
    assert store.records() == {key: pending()}


async def test_barrier_parks_a_call_after_commit_until_opened():
    hooks = BarrierHooks()
    store = FakeClaimStore(FakeClock(), hooks=hooks)
    key = CellKey(CAL, T0)
    gate = hooks.gate("after", "try_claim", key)
    first = asyncio.create_task(store.try_claim(key, pending()))
    await asyncio.wait_for(gate.reached.wait(), 1)
    assert not first.done()
    # The claim is already committed: a competing attempt sees it held.
    other = await store.try_claim(key, pending(attempt_id="fedcba9876543210"))
    assert isinstance(other, Held) and other.record == pending()
    gate.open()
    assert isinstance(await first, Claimed)


async def test_barrier_before_lets_another_call_win_first():
    hooks = BarrierHooks()
    store = FakeClaimStore(FakeClock(), hooks=hooks)
    key = CellKey(CAL, T0)
    gate = hooks.gate("before", "try_claim", key, occurrence=1)
    slow = asyncio.create_task(store.try_claim(key, pending()))
    await asyncio.wait_for(gate.reached.wait(), 1)
    fast = await store.try_claim(key, pending(attempt_id="fedcba9876543210"))
    assert isinstance(fast, Claimed)
    gate.open()
    assert isinstance(await slow, Held)


async def test_hooks_record_every_point_in_order():
    hooks = BarrierHooks()
    store = FakeClaimStore(FakeClock(), hooks=hooks)
    key = CellKey(CAL, T0)
    claimed = await store.try_claim(key, pending())
    with pytest.raises(PreconditionFailed):
        await store.release(key, '"wrong"')
    await store.release(key, claimed.etag)
    assert [(w, op) for w, op, _ in hooks.events] == [
        ("before", "try_claim"),
        ("after", "try_claim"),
        ("before", "release"),
        ("before", "release"),
        ("after", "release"),
    ]


def test_gate_argument_checks():
    hooks = BarrierHooks()
    with pytest.raises(ValueError):
        hooks.gate("during", "read")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        hooks.gate("before", "explode")
    with pytest.raises(ValueError):
        hooks.gate("before", "read", occurrence=0)
