"""The canonical HMAC input encoding (spec section 7.4, "HMAC encoding").

Each field is length-prefixed: a 4-byte big-endian length, then the bytes.
Strings are UTF-8. Times are ISO-8601 UTC with a `Z` suffix and minute
precision; a time with seconds or microseconds is refused rather than truncated,
so two different instants can never encode the same. Used by slot tokens (UC03)
and the identity HMACs (UC04a).
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta

_MAX_FIELD = 0xFFFFFFFF


def encode_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("times must be tz-aware UTC")
    if value.second or value.microsecond:
        raise ValueError("times are encoded at minute precision")
    return value.strftime("%Y-%m-%dT%H:%MZ")


def _field_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, datetime):
        return encode_time(value).encode("ascii")
    raise TypeError("canonical_encode takes str, bytes or datetime fields")


def canonical_encode(*fields: object) -> bytes:
    out = bytearray()
    for value in fields:
        data = _field_bytes(value)
        if len(data) > _MAX_FIELD:
            raise ValueError("field too long")
        out += struct.pack(">I", len(data))
        out += data
    return bytes(out)
