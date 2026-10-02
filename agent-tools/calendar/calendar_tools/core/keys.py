"""The service keys (spec section 9.2; plan P18).

`KeySet` is a frozen per-request snapshot of the three keys, used by the pure,
synchronous token and identity HMAC functions. `KeyRing` is the live source:
each request calls `load` exactly once, so one request always sees one
consistent key set, and a rotation is picked up without a restart. Nothing in
`core/` performs I/O for keys; the Key Vault implementation is UC07's
`KeyVaultKeyRing`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from calendar_tools.core.deadline import Deadline

KEY_BYTES = 32


def _check_key(name: str, value: object) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    if len(value) != KEY_BYTES:
        # The message names the key, never its value or length.
        raise ValueError(f"{name} must be exactly {KEY_BYTES} bytes")


@dataclass(frozen=True)
class KeySet:
    slot_current: bytes = field(repr=False)
    slot_previous: bytes | None = field(repr=False)
    fingerprint: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _check_key("slot_current", self.slot_current)
        if self.slot_previous is not None:
            _check_key("slot_previous", self.slot_previous)
        _check_key("fingerprint", self.fingerprint)

    def slot_keys(self) -> tuple[bytes, ...]:
        """The slot-token keys to try, current first."""
        if self.slot_previous is None:
            return (self.slot_current,)
        return (self.slot_current, self.slot_previous)

    def __repr__(self) -> str:
        previous = "set" if self.slot_previous is not None else "none"
        return f"KeySet(slot_current=<redacted>, slot_previous=<{previous}>, fingerprint=<redacted>)"

    __str__ = __repr__


class KeyRing(Protocol):
    async def load(self, deadline: Deadline) -> KeySet:
        """The current key set. Raises `SecretStoreUnavailable` (or its subclass
        `SecretInvalid`) when the keys cannot be read or fail their shape check."""
        ...
