"""Slot tokens (spec 7.2, plan UC03 and P13), the canonical HMAC encoding
(spec 7.4) and the `KeySet` value object (plan P18)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_tools.core import tokens
from calendar_tools.core.encoding import canonical_encode
from calendar_tools.core.keys import KeySet
from calendar_tools.core.tokens import InvalidSlot, SlotMisaligned, VerifiedSlot
from tests.fakes.keyring import FINGERPRINT_KEY, SLOT_KEY_1, SLOT_KEY_2, SLOT_KEY_3, make_keys

TORONTO = ZoneInfo("America/Toronto")
START = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)  # Tue 14:00 Toronto
EXPIRY = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
BINDING = "test-alpha"
OTHER = "test-beta"
ALPHABET = set("abcdefghijklmnopqrstuvwxyz234567")


def _issue(keys: KeySet | None = None, binding: str = BINDING, start: datetime = START, duration: int = 30) -> str:
    return tokens.issue(keys or make_keys(), binding, start, duration, EXPIRY)


# --- canonical encoding ------------------------------------------------------


def test_canonical_encode_length_prefixes_each_field() -> None:
    assert canonical_encode("ab", b"\x00\x01") == b"\x00\x00\x00\x02ab\x00\x00\x00\x02\x00\x01"


def test_canonical_encode_is_unambiguous() -> None:
    assert canonical_encode("a|b", "c") != canonical_encode("a", "b|c")
    assert canonical_encode("ab", "") != canonical_encode("a", "b")


def test_canonical_encode_utf8_and_times() -> None:
    assert canonical_encode("é") == b"\x00\x00\x00\x02\xc3\xa9"
    when = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
    assert canonical_encode(when) == canonical_encode("2026-10-06T18:00Z")


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 10, 6, 18, 0),
        datetime(2026, 10, 6, 14, 0, tzinfo=TORONTO),
        datetime(2026, 10, 6, 18, 0, 59, tzinfo=UTC),  # minute precision: never truncated silently
        datetime(2026, 10, 6, 18, 0, 0, 1, tzinfo=UTC),
        5,
        None,
    ],
)
def test_canonical_encode_rejects_naive_non_utc_and_other_types(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_encode(bad)


# --- KeySet ------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slot_current": b"short"},
        {"slot_current": bytes(33)},
        {"slot_previous": bytes(31)},
        {"fingerprint": b""},
        {"slot_current": "x" * 32},
    ],
)
def test_keyset_rejects_keys_that_are_not_32_bytes(kwargs) -> None:
    values = {"slot_current": SLOT_KEY_1, "slot_previous": None, "fingerprint": FINGERPRINT_KEY}
    values.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        KeySet(**values)


def test_keyset_repr_shows_no_key_bytes() -> None:
    keys = make_keys(SLOT_KEY_1, SLOT_KEY_2)
    text = repr(keys) + str(keys)
    for key in (SLOT_KEY_1, SLOT_KEY_2, FINGERPRINT_KEY):
        assert repr(key) not in text
        assert key.hex() not in text
        assert base64.b64encode(key).decode() not in text


def test_keyset_is_frozen() -> None:
    keys = make_keys()
    with pytest.raises(AttributeError):
        keys.slot_current = SLOT_KEY_2  # type: ignore[misc]


# --- issue and verify ----------------------------------------------------------


def test_round_trip() -> None:
    token = _issue()
    result = tokens.verify(make_keys(), BINDING, token, START)
    assert isinstance(result, VerifiedSlot)
    assert result.start == START
    assert result.duration_minutes == 30
    assert result.end == START + timedelta(minutes=30)
    assert result.expiry == EXPIRY
    assert result.start.utcoffset() == timedelta(0)


def test_length_and_alphabet() -> None:
    token = _issue()
    assert token.startswith("v1.")
    body = token[3:]
    assert len(body) == 39
    assert set(body) <= ALPHABET
    assert "=" not in token
    assert token == token.lower()


def test_layout_matches_the_plan() -> None:
    token = _issue()
    body = token[3:].upper()
    raw = base64.b32decode(body + "=" * (-len(body) % 8))
    payload, mac = raw[:14], raw[14:]
    assert len(mac) == 10
    start_min, duration, expiry_min = struct.unpack(">IHI", payload[:10])
    assert start_min * 60 == int(START.timestamp())
    assert duration == 30
    assert expiry_min * 60 == int(EXPIRY.timestamp())
    assert payload[10:] == hashlib.sha256(BINDING.encode()).digest()[:4]
    expected = hmac.new(SLOT_KEY_1, canonical_encode("slot|v1", BINDING, payload), hashlib.sha256).digest()[:10]
    assert mac == expected


def test_every_single_character_flip_is_refused() -> None:
    token = _issue()
    keys = make_keys()
    for i in range(3, len(token)):
        for ch in "abcdefghijklmnopqrstuvwxyz234567":
            if ch == token[i]:
                continue
            tampered = token[:i] + ch + token[i + 1 :]
            result = tokens.verify(keys, BINDING, tampered, START)
            assert isinstance(result, InvalidSlot), (i, ch)
            assert result.reason in {"malformed", "wrong_binding"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: "v2." + t[3:],
        lambda t: "V1." + t[3:],
        lambda t: t[3:],
        lambda t: "v1" + t[3:],
        lambda t: t + "a",
        lambda t: t[:-1],
        lambda t: t.upper(),
        lambda t: t + "=",
        lambda t: t[:10] + "1" + t[11:],
        lambda t: t[:10] + "-" + t[11:],
        lambda t: "",
        lambda t: " " + t,
    ],
)
def test_wrong_version_or_shape_is_malformed(mutate) -> None:
    result = tokens.verify(make_keys(), BINDING, mutate(_issue()), START)
    assert result == InvalidSlot("malformed")


@pytest.mark.parametrize("bad", [None, 7, b"v1.abc", ["v1."]])
def test_non_string_token_is_malformed(bad) -> None:
    assert tokens.verify(make_keys(), BINDING, bad, START) == InvalidSlot("malformed")


def test_token_from_another_binding_is_wrong_binding() -> None:
    token = _issue(binding=BINDING)
    assert tokens.verify(make_keys(), OTHER, token, START) == InvalidSlot("wrong_binding")


def test_misaligned_start_with_a_valid_mac_is_malformed() -> None:
    # Built in-test with the key, so only the alignment check can refuse it.
    start_min = int(START.timestamp()) // 60 + 1
    payload = struct.pack(">IHI", start_min, 30, int(EXPIRY.timestamp()) // 60)
    payload += hashlib.sha256(BINDING.encode()).digest()[:4]
    mac = hmac.new(SLOT_KEY_1, canonical_encode("slot|v1", BINDING, payload), hashlib.sha256).digest()[:10]
    token = "v1." + base64.b32encode(payload + mac).decode().lower().rstrip("=")
    when = datetime.fromtimestamp(start_min * 60, UTC)
    assert tokens.verify(make_keys(), BINDING, token, when) == InvalidSlot("malformed")


def test_misaligned_end_with_a_valid_mac_is_malformed() -> None:
    payload = struct.pack(">IHI", int(START.timestamp()) // 60, 32, int(EXPIRY.timestamp()) // 60)
    payload += hashlib.sha256(BINDING.encode()).digest()[:4]
    mac = hmac.new(SLOT_KEY_1, canonical_encode("slot|v1", BINDING, payload), hashlib.sha256).digest()[:10]
    token = "v1." + base64.b32encode(payload + mac).decode().lower().rstrip("=")
    assert tokens.verify(make_keys(), BINDING, token, START) == InvalidSlot("malformed")


def test_verify_accepts_an_expired_token_and_check_expiry_rejects_it() -> None:
    token = _issue()
    result = tokens.verify(make_keys(), BINDING, token, START)
    assert isinstance(result, VerifiedSlot)  # verify() never checks expiry
    assert tokens.check_expiry(result, EXPIRY - timedelta(seconds=1)) is result
    assert tokens.check_expiry(result, EXPIRY) == InvalidSlot("expired")  # now == expiry is expired
    assert tokens.check_expiry(result, EXPIRY + timedelta(days=1)) == InvalidSlot("expired")


def test_check_expiry_requires_aware_utc_now() -> None:
    result = tokens.verify(make_keys(), BINDING, _issue(), START)
    with pytest.raises(ValueError):
        tokens.check_expiry(result, datetime(2026, 10, 5, 12, 0))  # type: ignore[arg-type]


@pytest.mark.parametrize("delta", [timedelta(minutes=1), timedelta(minutes=-1), timedelta(seconds=1)])
def test_start_mismatch(delta) -> None:
    assert tokens.verify(make_keys(), BINDING, _issue(), START + delta) == InvalidSlot("start_mismatch")


def test_start_with_any_offset_is_the_same_instant() -> None:
    local = START.astimezone(TORONTO)
    assert isinstance(tokens.verify(make_keys(), BINDING, _issue(), local), VerifiedSlot)


def test_naive_start_is_read_in_the_binding_zone() -> None:
    naive = datetime(2026, 10, 6, 14, 0)  # 14:00 Toronto == 18:00Z
    assert isinstance(tokens.verify(make_keys(), BINDING, _issue(), naive, tz=TORONTO), VerifiedSlot)
    utc_naive = datetime(2026, 10, 6, 18, 0)
    assert tokens.verify(make_keys(), BINDING, _issue(), utc_naive, tz=TORONTO) == InvalidSlot("start_mismatch")


def test_naive_start_without_a_zone_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        tokens.verify(make_keys(), BINDING, _issue(), datetime(2026, 10, 6, 14, 0))


def test_previous_key_accepted_and_unknown_key_rejected() -> None:
    token = _issue(keys=make_keys(SLOT_KEY_1))
    rotated = make_keys(SLOT_KEY_2, SLOT_KEY_1)
    assert isinstance(tokens.verify(rotated, BINDING, token, START), VerifiedSlot)
    unknown = make_keys(SLOT_KEY_2, SLOT_KEY_3)
    assert tokens.verify(unknown, BINDING, token, START) == InvalidSlot("malformed")
    no_previous = make_keys(SLOT_KEY_2)
    assert tokens.verify(no_previous, BINDING, token, START) == InvalidSlot("malformed")


def test_new_tokens_use_the_current_key() -> None:
    rotated = make_keys(SLOT_KEY_2, SLOT_KEY_1)
    token = _issue(keys=rotated)
    assert isinstance(tokens.verify(make_keys(SLOT_KEY_2), BINDING, token, START), VerifiedSlot)
    assert tokens.verify(make_keys(SLOT_KEY_1), BINDING, token, START) == InvalidSlot("malformed")


def test_mac_comparison_uses_compare_digest(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(tokens.hmac, "compare_digest", spy)
    token = _issue(keys=make_keys(SLOT_KEY_1))
    assert isinstance(tokens.verify(make_keys(SLOT_KEY_2, SLOT_KEY_1), BINDING, token, START), VerifiedSlot)
    mac_calls = [c for c in calls if len(c[0]) == 10]
    assert len(mac_calls) == 2  # current key tried, then previous


@pytest.mark.parametrize(
    "start",
    [
        START + timedelta(minutes=1),
        START + timedelta(seconds=30),
        START + timedelta(microseconds=1),
    ],
)
def test_misaligned_issuance_is_refused(start) -> None:
    with pytest.raises(SlotMisaligned):
        tokens.issue(make_keys(), BINDING, start, 30, EXPIRY)


def test_misaligned_end_issuance_is_refused() -> None:
    with pytest.raises(SlotMisaligned):
        tokens.issue(make_keys(), BINDING, START, 32, EXPIRY)


@pytest.mark.parametrize(
    ("start", "duration", "expiry"),
    [
        (datetime(2026, 10, 6, 18, 0), 30, EXPIRY),
        (START, 30, datetime(2026, 10, 5, 12, 30)),
        (START, 0, EXPIRY),
        (START, 70000, EXPIRY),
        (START, True, EXPIRY),
    ],
)
def test_issue_rejects_bad_arguments(start, duration, expiry) -> None:
    with pytest.raises((ValueError, TypeError)):
        tokens.issue(make_keys(), BINDING, start, duration, expiry)


def test_slot_misaligned_carries_its_diagnostic() -> None:
    assert SlotMisaligned.diagnostic == "slot_misaligned"
