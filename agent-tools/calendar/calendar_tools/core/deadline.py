"""The per-request deadline (spec section 7.5) and the P17 budget constants.

Every external call takes its timeout from `Deadline.timeout_for`, so no call can
run past the request deadline. The binding loader (P17) and the booking step-5
guard (UC04b) share the constants below.
"""

from __future__ import annotations

from typing import ClassVar

from calendar_tools.core.clock import Clock

TOTAL_BUDGET = 8.0  # seconds per request
RESERVE = 3.0  # seconds kept back for one provider call already in flight (P17)
PROVIDER_TIMEOUT = 3.0
CLAIM_TIMEOUT = 1.0
EXPECTED_CLAIM_LATENCY = 0.1


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def claim_cells_fit_budget(cells: int) -> bool:
    """P17: `cells x EXPECTED_CLAIM_LATENCY + PROVIDER_TIMEOUT + CLAIM_TIMEOUT
    <= TOTAL_BUDGET - RESERVE`, computed in integer milliseconds."""
    cost = cells * _ms(EXPECTED_CLAIM_LATENCY) + _ms(PROVIDER_TIMEOUT) + _ms(CLAIM_TIMEOUT)
    return cost <= _ms(TOTAL_BUDGET) - _ms(RESERVE)


MAX_CLAIM_CELLS = (
    _ms(TOTAL_BUDGET) - _ms(RESERVE) - _ms(PROVIDER_TIMEOUT) - _ms(CLAIM_TIMEOUT)
) // _ms(EXPECTED_CLAIM_LATENCY)


class DeadlineExceeded(Exception):
    diagnostic: ClassVar[str] = "deadline_exceeded"


class Deadline:
    def __init__(self, clock: Clock, total: float = TOTAL_BUDGET) -> None:
        self._clock = clock
        self._total = total
        self._start = clock.now()

    def remaining(self) -> float:
        elapsed = (self._clock.now() - self._start).total_seconds()
        return max(0.0, self._total - elapsed)

    def expired(self) -> bool:
        return self.remaining() <= 0

    def timeout_for(self, per_call_limit: float, reserve: float = 0) -> float:
        timeout = min(per_call_limit, self.remaining() - reserve)
        if timeout <= 0:
            raise DeadlineExceeded()
        return timeout
