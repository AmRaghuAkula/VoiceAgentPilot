"""`FakeKeyRing` (plan P18, UC03): the `KeyRing` the core's tests use.

Fixed test bytes, `rotate()`, `fail_next(exc)` and a `load_calls` counter. It
never performs I/O.
"""

from __future__ import annotations

from collections import deque

from calendar_tools.core.deadline import Deadline
from calendar_tools.core.keys import KeySet
from calendar_tools.core.ports import ProviderError

# Fixed, obviously fake 32-byte keys.
SLOT_KEY_1 = bytes(range(32))
SLOT_KEY_2 = bytes(range(32, 64))
SLOT_KEY_3 = bytes(range(64, 96))
FINGERPRINT_KEY = bytes(range(96, 128))


def make_keys(slot_current: bytes = SLOT_KEY_1, slot_previous: bytes | None = None) -> KeySet:
    return KeySet(slot_current=slot_current, slot_previous=slot_previous, fingerprint=FINGERPRINT_KEY)


class FakeKeyRing:
    def __init__(self, keys: KeySet | None = None) -> None:
        self.keys = keys if keys is not None else make_keys()
        self.load_calls = 0
        self.deadlines: list[Deadline] = []
        self._failures: deque[ProviderError] = deque()

    def rotate(self, new_slot_current: bytes) -> None:
        """The old current key becomes `slot_previous` (spec 9.2)."""
        self.keys = KeySet(
            slot_current=new_slot_current,
            slot_previous=self.keys.slot_current,
            fingerprint=self.keys.fingerprint,
        )

    def fail_next(self, exc: ProviderError) -> None:
        self._failures.append(exc)

    async def load(self, deadline: Deadline) -> KeySet:
        self.load_calls += 1
        self.deadlines.append(deadline)
        if self._failures:
            raise self._failures.popleft()
        return self.keys
