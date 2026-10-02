"""Deterministic interleavings for `FakeClaimStore` (plan UC04a).

`BarrierHooks` is a `ClaimHooks`: the fake awaits `before(op, key)` before an
operation touches the cell and `after(op, key)` once it has committed (and
before it returns). A test arms a `Gate` on one point; the task that reaches
it parks there until the test opens the gate, so UC04b/UC04c tests can force
"the retry arrives between the original's claim and its create" and similar
orderings without sleeps.

    gate = hooks.gate("after", "try_claim", key)   # the first matching call
    task = asyncio.create_task(service.book(...))
    await gate.reached.wait()                      # the claim is committed
    ...                                            # run the competing request
    gate.open()
    await task
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from calendar_tools.core.claims import OPERATIONS
from calendar_tools.core.identity import CellKey

When = Literal["before", "after"]


@dataclass
class Gate:
    when: When
    operation: str
    key: CellKey | None
    occurrence: int
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    _open: asyncio.Event = field(default_factory=asyncio.Event)
    _seen: int = 0

    def open(self) -> None:
        self._open.set()

    def _matches(self, when: When, operation: str, key: CellKey) -> bool:
        return self.when == when and self.operation == operation and (self.key is None or self.key == key)


class BarrierHooks:
    def __init__(self) -> None:
        self.events: list[tuple[When, str, CellKey]] = []
        self._gates: list[Gate] = []

    def gate(self, when: When, operation: str, key: CellKey | None = None, *, occurrence: int = 1) -> Gate:
        """Park the `occurrence`-th matching call at `when` until `open()`."""
        if when not in ("before", "after"):
            raise ValueError("when must be 'before' or 'after'")
        if operation not in OPERATIONS:
            raise ValueError("unknown claim-store operation")
        if occurrence < 1:
            raise ValueError("occurrence starts at 1")
        g = Gate(when, operation, key, occurrence)
        self._gates.append(g)
        return g

    async def before(self, operation: str, key: CellKey) -> None:
        await self._point("before", operation, key)

    async def after(self, operation: str, key: CellKey) -> None:
        await self._point("after", operation, key)

    async def _point(self, when: When, operation: str, key: CellKey) -> None:
        self.events.append((when, operation, key))
        for g in self._gates:
            if g._matches(when, operation, key):
                g._seen += 1
                if g._seen == g.occurrence:
                    g.reached.set()
                    await g._open.wait()
