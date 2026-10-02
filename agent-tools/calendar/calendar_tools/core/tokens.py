"""Slot tokens (spec section 7.2; plan UC03, P13).

A token proves only that "this service offered this slot on this binding
recently". It is not a reservation.

Layout: payload = start_epoch_min:u32 | duration_min:u16 | expiry_epoch_min:u32
| sha256(binding_id)[:4] (14 bytes); mac = HMAC-SHA256(key,
canonical_encode("slot|v1", binding_id, payload))[:10]; token = "v1." +
lowercase base32(payload | mac) without padding (39 characters).

Verification order (P13): version and shape -> binding hash (`wrong_binding`)
-> MAC, current key then previous, constant-time (`malformed`) -> 5-minute UTC
alignment of start and end (`malformed`) -> `start` equality
(`start_mismatch`). `verify()` never checks expiry; `check_expiry` does, at
booking step 4 (spec 7.4).

Pure and synchronous: both functions take an already-loaded `KeySet`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

from calendar_tools.core.encoding import canonical_encode
from calendar_tools.core.keys import KeySet

VERSION_PREFIX = "v1."
MAC_DOMAIN = "slot|v1"
ALIGN_MINUTES = 5
PAYLOAD_BYTES = 14
MAC_BYTES = 10
BODY_CHARS = 39

_PAYLOAD = struct.Struct(">IHI")
_BODY = re.compile(r"[a-z2-7]{39}")
_U32 = 2**32
_U16 = 2**16

InvalidReason = Literal["malformed", "wrong_binding", "start_mismatch", "expired"]


class SlotMisaligned(Exception):
    """Issuance refused: a start or end not on the 5-minute UTC grid is a server
    bug (spec 7.2). The caller logs `slot_misaligned` and drops the slot."""

    diagnostic: ClassVar[str] = "slot_misaligned"


@dataclass(frozen=True)
class VerifiedSlot:
    start: datetime  # tz-aware UTC
    duration_minutes: int
    expiry: datetime  # tz-aware UTC

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class InvalidSlot:
    reason: InvalidReason


def _require_utc(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a tz-aware UTC datetime")
    return value


def _epoch_minutes(value: datetime) -> int:
    return int(value.timestamp()) // 60


def _binding_hash(binding_id: str) -> bytes:
    return hashlib.sha256(binding_id.encode("utf-8")).digest()[:4]


def _mac(key: bytes, binding_id: str, payload: bytes) -> bytes:
    return hmac.new(key, canonical_encode(MAC_DOMAIN, binding_id, payload), hashlib.sha256).digest()[:MAC_BYTES]


def _aligned(value: datetime) -> bool:
    return value.second == 0 and value.microsecond == 0 and value.minute % ALIGN_MINUTES == 0


def issue(keys: KeySet, binding_id: str, start: datetime, duration_min: int, expiry: datetime) -> str:
    _require_utc("start", start)
    _require_utc("expiry", expiry)
    if not isinstance(duration_min, int) or isinstance(duration_min, bool):
        raise TypeError("duration_min must be an int")
    if not 0 < duration_min < _U16:
        raise ValueError("duration_min out of range")
    end = start + timedelta(minutes=duration_min)
    if not (_aligned(start) and _aligned(end)):
        raise SlotMisaligned()
    start_min, expiry_min = _epoch_minutes(start), _epoch_minutes(expiry)
    if not (0 <= start_min < _U32 and 0 <= expiry_min < _U32):
        raise ValueError("time out of range")
    payload = _PAYLOAD.pack(start_min, duration_min, expiry_min) + _binding_hash(binding_id)
    raw = payload + _mac(keys.slot_current, binding_id, payload)
    return VERSION_PREFIX + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


def _decode(token: object) -> bytes | None:
    if not isinstance(token, str) or not token.startswith(VERSION_PREFIX):
        return None
    body = token[len(VERSION_PREFIX) :]
    if not _BODY.fullmatch(body):
        return None
    try:
        raw = base64.b32decode(body.upper() + "=" * (-len(body) % 8))
    except (binascii.Error, ValueError):
        return None
    # The last character carries unused bits; only the one canonical spelling of
    # these bytes is accepted, so a token cannot be altered and still verify.
    if base64.b32encode(raw).decode("ascii").lower().rstrip("=") != body:
        return None
    if len(raw) != PAYLOAD_BYTES + MAC_BYTES:
        return None
    return raw


def _as_utc(start: datetime, tz: ZoneInfo | None) -> datetime:
    if not isinstance(start, datetime):
        raise TypeError("start must be a datetime")
    if start.tzinfo is None:
        if tz is None:
            raise ValueError("a naive start needs the binding's timezone")
        start = start.replace(tzinfo=tz)
    return start.astimezone(UTC)


def verify(
    keys: KeySet, binding_id: str, token: object, start: datetime, tz: ZoneInfo | None = None
) -> VerifiedSlot | InvalidSlot:
    """Check a token against this binding and the request's `start` (a naive
    `start` is read in `tz`, the binding's zone). Never checks expiry."""
    claimed_start = _as_utc(start, tz)
    raw = _decode(token)
    if raw is None:
        return InvalidSlot("malformed")
    payload, mac = raw[:PAYLOAD_BYTES], raw[PAYLOAD_BYTES:]
    # P13: the binding hash is plaintext in the payload, so checking it before
    # the MAC reveals nothing the token doesn't already show.
    if not hmac.compare_digest(payload[10:], _binding_hash(binding_id)):
        return InvalidSlot("wrong_binding")
    mac_ok = False
    for key in keys.slot_keys():
        # Every key is tried, so the time taken doesn't reveal which one matched.
        mac_ok |= hmac.compare_digest(_mac(key, binding_id, payload), mac)
    if not mac_ok:
        return InvalidSlot("malformed")
    start_min, duration, expiry_min = _PAYLOAD.unpack(payload[:10])
    if duration == 0 or start_min % ALIGN_MINUTES or (start_min + duration) % ALIGN_MINUTES:
        return InvalidSlot("malformed")
    slot_start = datetime.fromtimestamp(start_min * 60, UTC)
    if claimed_start != slot_start:
        return InvalidSlot("start_mismatch")
    return VerifiedSlot(
        start=slot_start,
        duration_minutes=duration,
        expiry=datetime.fromtimestamp(expiry_min * 60, UTC),
    )


def check_expiry(verified: VerifiedSlot, now: datetime) -> VerifiedSlot | InvalidSlot:
    """`now >= expiry` is expired (spec 7.4 step 4)."""
    _require_utc("now", now)
    if now >= verified.expiry:
        return InvalidSlot("expired")
    return verified
