"""Identity HMACs, claim cells and the calendar-identity cache (spec 7.4
"Definitions", "HMAC encoding", "Claim cells"; plan UC04a)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from calendar_tools.core import identity
from calendar_tools.core.contact import Contact
from calendar_tools.core.deadline import Deadline, DeadlineExceeded
from calendar_tools.core.encoding import canonical_encode, iso_utc_minute
from calendar_tools.core.identity import (
    CalendarIdentityCache,
    booking_ref,
    calendar_key,
    contact_tag,
    fingerprint,
)
from calendar_tools.core.ports import CalendarRef, ProviderTimeout, ProviderUnavailable
from calendar_tools.providers.fake import FakeCalendarProvider
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FINGERPRINT_KEY

K = FINGERPRINT_KEY
OTHER_K = bytes(range(128, 160))
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=30)
HEX64 = re.compile(r"[0-9a-f]{64}")
CROCKFORD = re.compile(r"[0-9A-HJKMNP-TV-Z]{6}")

PHONE = Contact(name="Jordan Example", phone="+16135550123", email=None)
EMAIL = Contact(name="Jordan Example", phone=None, email="jordan@example.com")


def _cal_key() -> str:
    return calendar_key(K, "fake", "cal-0001")


# --- canonical encoding -------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("ab", "c"), ("a", "bc")),
        (("", "abc"), ("abc", "")),
        (("a", ""), ("a",)),
        ((b"\x00\x00\x00\x01a",), ("", "a")),
    ],
)
def test_canonical_encoding_is_injective_on_tricky_pairs(left, right):
    assert canonical_encode(*left) != canonical_encode(*right)


def test_canonical_encoding_layout():
    assert canonical_encode("ab", b"\x01") == b"\x00\x00\x00\x02ab\x00\x00\x00\x01\x01"


def test_iso_utc_minute():
    assert iso_utc_minute(T0) == "2026-10-05T14:00Z"


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 10, 5, 14, 0),
        datetime(2026, 10, 5, 10, 0, tzinfo=timezone(timedelta(hours=-4))),
        datetime(2026, 10, 5, 14, 0, 30, tzinfo=UTC),
    ],
    ids=["naive", "non-utc", "seconds"],
)
def test_iso_utc_minute_refuses_non_utc_or_sub_minute(bad):
    with pytest.raises(ValueError):
        iso_utc_minute(bad)


# --- calendar_key ---------------------------------------------------------------


def test_calendar_key_is_hex_hmac_of_provider_and_canonical_id():
    key = calendar_key(K, "fake", "cal-0001")
    assert HEX64.fullmatch(key)
    expected = hmac.new(K, canonical_encode("calendar_key|v1", "fake", "cal-0001"), hashlib.sha256).hexdigest()
    assert key == expected


def test_calendar_key_differs_by_provider_id_and_key():
    base = calendar_key(K, "fake", "cal-0001")
    assert calendar_key(K, "fake", "cal-0002") != base
    assert calendar_key(K, "other", "cal-0001") != base
    assert calendar_key(OTHER_K, "fake", "cal-0001") != base


@pytest.mark.parametrize(("provider", "cal_id"), [("", "cal-0001"), ("fake", ""), (None, "x"), ("fake", 3)])
def test_calendar_key_rejects_empty_or_non_string(provider, cal_id):
    with pytest.raises((ValueError, TypeError)):
        calendar_key(K, provider, cal_id)


def test_identity_functions_reject_a_wrong_size_key():
    with pytest.raises(ValueError):
        calendar_key(b"short", "fake", "cal-0001")
    with pytest.raises(ValueError):
        contact_tag(b"short", PHONE)


# --- contact_tag ----------------------------------------------------------------


def test_contact_tag_uses_phone_when_present():
    tag = contact_tag(K, PHONE)
    assert HEX64.fullmatch(tag)
    both = Contact(name=None, phone="+16135550123", email="jordan@example.com")
    assert contact_tag(K, both) == tag


def test_contact_tag_uses_lowercased_email_without_phone():
    upper = Contact(name=None, phone=None, email="Jordan@Example.COM")
    assert contact_tag(K, upper) == contact_tag(K, EMAIL)


def test_adding_a_phone_to_an_email_only_contact_changes_the_tag():
    # Accepted (spec 7.4, 9.3): the identity is the phone when there is one.
    with_phone = Contact(name=None, phone="+16135550123", email="jordan@example.com")
    assert contact_tag(K, with_phone) != contact_tag(K, EMAIL)


def test_contact_tag_ignores_the_name():
    other_name = Contact(name="Jordan Q. Example", phone="+16135550123", email=None)
    assert contact_tag(K, other_name) == contact_tag(K, PHONE)


def test_contact_tag_requires_an_identity():
    with pytest.raises(ValueError):
        contact_tag(K, Contact(name="Jordan Example", phone=None, email=None))


# --- fingerprint ----------------------------------------------------------------


def test_fingerprint_shape_and_determinism():
    fp = fingerprint(K, _cal_key(), T0, T1, contact_tag(K, PHONE), "phone_call")
    assert HEX64.fullmatch(fp)
    assert fp == fingerprint(K, _cal_key(), T0, T1, contact_tag(K, PHONE), "phone_call")


def test_fingerprint_is_unchanged_by_the_name_spelling():
    a = Contact(name="Jordan Example", phone="+16135550123", email=None)
    b = Contact(name="jordan  example", phone="+16135550123", email=None)
    fa = fingerprint(K, _cal_key(), T0, T1, contact_tag(K, a), "phone_call")
    fb = fingerprint(K, _cal_key(), T0, T1, contact_tag(K, b), "phone_call")
    assert fa == fb


def test_fingerprint_excludes_binding_id():
    # Two bindings sharing one calendar produce the same fingerprint (spec 7.4):
    # the function has no binding_id parameter at all.
    import inspect

    assert "binding_id" not in inspect.signature(fingerprint).parameters


@pytest.mark.parametrize("field", ["calendar_key", "start", "end", "contact_tag", "appointment_type"])
def test_fingerprint_changes_with_each_input(field):
    args = {
        "calendar_key": _cal_key(),
        "start": T0,
        "end": T1,
        "contact_tag": contact_tag(K, PHONE),
        "appointment_type": "phone_call",
    }
    base = fingerprint(K, **args)
    changed = dict(args)
    changed[field] = {
        "calendar_key": calendar_key(K, "fake", "cal-0002"),
        "start": T0 - timedelta(minutes=5),
        "end": T1 + timedelta(minutes=5),
        "contact_tag": contact_tag(K, EMAIL),
        "appointment_type": "in_person",
    }[field]
    assert fingerprint(K, **changed) != base


def test_fingerprint_refuses_non_utc_times():
    with pytest.raises(ValueError):
        fingerprint(K, _cal_key(), datetime(2026, 10, 5, 14, 0), T1, contact_tag(K, PHONE), "phone_call")


def test_fingerprint_refuses_end_not_after_start():
    with pytest.raises(ValueError):
        fingerprint(K, _cal_key(), T0, T0, contact_tag(K, PHONE), "phone_call")


# --- booking_ref ----------------------------------------------------------------


def test_booking_ref_shape():
    fp = fingerprint(K, _cal_key(), T0, T1, contact_tag(K, PHONE), "phone_call")
    ref = booking_ref(K, fp)
    assert CROCKFORD.fullmatch(ref)
    assert ref == booking_ref(K, fp)


def test_booking_ref_is_the_crockford_prefix_of_the_ref_hmac():
    fp = "ab" * 32
    mac = hmac.new(K, canonical_encode("ref", fp), hashlib.sha256).digest()
    bits = int.from_bytes(mac[:4], "big") >> 2  # the first 30 bits
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    expected = "".join(alphabet[(bits >> (5 * (5 - i))) & 31] for i in range(6))
    assert booking_ref(K, fp) == expected


def test_booking_refs_differ_across_fingerprints():
    refs = {booking_ref(K, f"{i:064x}") for i in range(200)}
    assert len(refs) > 190  # 30 bits: collisions at this size are vanishingly rare


# --- CalendarIdentityCache --------------------------------------------------------


def _provider() -> FakeCalendarProvider:
    p = FakeCalendarProvider()
    p.canonical_ids.update({"primary": "cal-0001"})
    return p


PRIMARY = CalendarRef("fake", "primary", "cal-binding-test-alpha-fake")
EXPLICIT = CalendarRef("fake", "cal-0001", "cal-binding-test-alpha-fake")
RECONSENT = CalendarRef("fake", "cal-0001", "cal-binding-test-alpha-fake-2")
OTHER = CalendarRef("fake", "cal-0002", "cal-binding-test-alpha-fake")


async def test_calendar_key_equal_for_primary_and_canonical_id_via_the_cache():
    # Round-3 SF-4: per physical calendar, not per spelling or per credential.
    p = _provider()
    cache = CalendarIdentityCache()
    deadline = Deadline(FakeClock())
    a = await cache.calendar_key(p, PRIMARY, K, deadline)
    b = await cache.calendar_key(p, EXPLICIT, K, deadline)
    c = await cache.calendar_key(p, RECONSENT, K, deadline)
    d = await cache.calendar_key(p, OTHER, K, deadline)
    assert a == b == c == calendar_key(K, "fake", "cal-0001")
    assert d != a


async def test_cache_resolves_each_reference_once():
    p = _provider()
    cache = CalendarIdentityCache()
    deadline = Deadline(FakeClock())
    for _ in range(3):
        await cache.calendar_key(p, PRIMARY, K, deadline)
    assert p.calls.count("resolve_calendar_identity") == 1
    await cache.calendar_key(p, EXPLICIT, K, deadline)
    assert p.calls.count("resolve_calendar_identity") == 2


async def test_cache_failure_propagates_and_is_not_cached():
    p = _provider()
    p.fail_next("resolve_calendar_identity", ProviderUnavailable(reason="http_503"))
    cache = CalendarIdentityCache()
    deadline = Deadline(FakeClock())
    with pytest.raises(ProviderUnavailable):
        await cache.calendar_key(p, PRIMARY, K, deadline)
    assert await cache.calendar_key(p, PRIMARY, K, deadline) == calendar_key(K, "fake", "cal-0001")
    assert p.calls.count("resolve_calendar_identity") == 2


async def test_cache_refuses_when_the_deadline_is_spent():
    p = _provider()
    clock = FakeClock()
    deadline = Deadline(clock)
    clock.advance(9)
    with pytest.raises(DeadlineExceeded):
        await CalendarIdentityCache().calendar_key(p, PRIMARY, K, deadline)
    assert p.calls == []


class _SlowProvider(FakeCalendarProvider):
    async def resolve_calendar_identity(self, cal):
        await asyncio.sleep(5)
        return "never"


async def test_cache_times_out_as_provider_timeout(monkeypatch):
    monkeypatch.setattr(identity, "PROVIDER_TIMEOUT", 0.01)
    with pytest.raises(ProviderTimeout) as info:
        await CalendarIdentityCache().calendar_key(_SlowProvider(), PRIMARY, K, Deadline(FakeClock()))
    assert info.value.maybe_committed is False


@pytest.mark.parametrize("bad", ["", "a" * 1025, 42, None])
async def test_cache_rejects_an_unusable_canonical_id(bad):
    class _Odd(FakeCalendarProvider):
        async def resolve_calendar_identity(self, cal):
            return bad

    from calendar_tools.core.ports import ProviderConfigError

    with pytest.raises(ProviderConfigError) as info:
        await CalendarIdentityCache().calendar_key(_Odd(), PRIMARY, K, Deadline(FakeClock()))
    assert info.value.reason == "calendar_identity_invalid"


async def test_cache_key_includes_the_provider_name():
    class _Other(FakeCalendarProvider):
        name = "fake-b"

    cache = CalendarIdentityCache()
    deadline = Deadline(FakeClock())
    a = await cache.calendar_key(_provider(), EXPLICIT, K, deadline)
    other_ref = CalendarRef("fake-b", "cal-0001", "cal-binding-test-alpha-fake")
    b = await cache.calendar_key(_Other(), other_ref, K, deadline)
    assert a != b
