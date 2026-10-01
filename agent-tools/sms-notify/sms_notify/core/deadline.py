"""The end-to-end request deadline (spec K5).

Each operation's timeout is the smaller of its budget and the time left before the deadline.
The current request's deadline is carried in a context variable so adapters behind the
fixed ``Notifier`` signature (plan P1) can size their own timeouts.
"""

from __future__ import annotations

from contextvars import ContextVar

from sms_notify.ports import Clock

TOTAL_SECONDS = 5.0
TWILIO_POST_BUDGET = 3.5
KEY_VAULT_BUDGET = 1.5
JWKS_BUDGET = 1.5
STORAGE_BUDGET = 0.5


class Deadline:
    def __init__(self, clock: Clock, total: float = TOTAL_SECONDS) -> None:
        self._clock = clock
        self._end = clock.monotonic() + total

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock.monotonic())

    def budget(self, operation_budget: float) -> float:
        return min(operation_budget, self.remaining())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0


current_deadline: ContextVar[Deadline | None] = ContextVar("sms_notify_deadline", default=None)


def budget_for(operation_budget: float) -> float:
    """The timeout for an operation under the current request's deadline (or its own budget)."""
    deadline = current_deadline.get()
    return operation_budget if deadline is None else deadline.budget(operation_budget)
