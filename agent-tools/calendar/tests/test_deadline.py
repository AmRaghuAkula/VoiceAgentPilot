"""Request deadline and clock (spec section 7.5, plan P9, P17)."""

from __future__ import annotations

import re
from datetime import UTC, timedelta

import pytest

from calendar_tools.core import deadline as dl
from calendar_tools.core.clock import SystemClock, SystemNonceSource
from tests.fakes.clock import FakeClock


def test_constants():
    assert dl.TOTAL_BUDGET == 8.0
    assert dl.RESERVE == 3.0
    assert dl.PROVIDER_TIMEOUT == 3.0
    assert dl.CLAIM_TIMEOUT == 1.0
    assert dl.EXPECTED_CLAIM_LATENCY == 0.1


def test_remaining_counts_down_with_fake_clock():
    clock = FakeClock()
    d = dl.Deadline(clock)
    assert d.remaining() == pytest.approx(8.0)
    clock.advance(2.5)
    assert d.remaining() == pytest.approx(5.5)
    assert not d.expired()


def test_custom_total():
    clock = FakeClock()
    d = dl.Deadline(clock, total=2.0)
    clock.advance(1.0)
    assert d.remaining() == pytest.approx(1.0)


def test_expired_at_exact_boundary():
    clock = FakeClock()
    d = dl.Deadline(clock)
    clock.advance(7.999)
    assert not d.expired()
    clock.advance(0.001)
    assert d.remaining() == pytest.approx(0.0)
    assert d.expired()


def test_remaining_never_negative():
    clock = FakeClock()
    d = dl.Deadline(clock)
    clock.advance(20)
    assert d.remaining() == 0.0
    assert d.expired()


def test_timeout_for_returns_limit_when_budget_ample():
    d = dl.Deadline(FakeClock())
    assert d.timeout_for(dl.PROVIDER_TIMEOUT) == pytest.approx(3.0)


def test_timeout_for_caps_to_remaining_minus_reserve():
    clock = FakeClock()
    d = dl.Deadline(clock)
    clock.advance(6.0)  # 2.0 left
    assert d.timeout_for(3.0) == pytest.approx(2.0)
    assert d.timeout_for(3.0, reserve=0.5) == pytest.approx(1.5)


def test_timeout_for_raises_at_or_below_zero():
    clock = FakeClock()
    d = dl.Deadline(clock)
    clock.advance(7.0)  # 1.0 left
    with pytest.raises(dl.DeadlineExceeded):
        d.timeout_for(3.0, reserve=1.0)
    with pytest.raises(dl.DeadlineExceeded):
        d.timeout_for(3.0, reserve=2.0)
    clock.advance(5.0)
    with pytest.raises(dl.DeadlineExceeded):
        d.timeout_for(3.0)


def test_deadline_exceeded_diagnostic():
    assert dl.DeadlineExceeded.diagnostic == "deadline_exceeded"


@pytest.mark.parametrize(
    ("cells", "fits"),
    [(1, True), (10, True), (11, False), (48, False)],
)
def test_claim_cells_budget_p17(cells, fits):
    # 10 cells: 10 x 0.1 + 3 + 1 = 5.0 s <= 8 - 3; 11 cells: 5.1 s > 5.
    assert dl.claim_cells_fit_budget(cells) is fits


def test_max_claim_cells():
    assert dl.MAX_CLAIM_CELLS == 10


def test_system_clock_is_utc_aware():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert abs((now - __import__("datetime").datetime.now(UTC)).total_seconds()) < 5


async def test_system_clock_sleep_zero():
    await SystemClock().sleep(0)


def test_system_nonce_source():
    src = SystemNonceSource()
    a, b = src.new_hex(16), src.new_hex(16)
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    assert a != b


async def test_fake_clock_sleep_advances():
    clock = FakeClock()
    start = clock.now()
    await clock.sleep(1.5)
    assert clock.now() - start == timedelta(seconds=1.5)
    assert clock.sleeps == [1.5]
