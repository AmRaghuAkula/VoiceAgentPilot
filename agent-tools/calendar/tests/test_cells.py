"""Claim cells and cell keys (spec 7.4 "Claim cells"; plan UC04a)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from calendar_tools.core.identity import CellKey, calendar_key, cell_range
from tests.fakes.keyring import FINGERPRINT_KEY

K = FINGERPRINT_KEY
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=30)


def _cal_key() -> str:
    return calendar_key(K, "fake", "cal-0001")


# --- cell_range -----------------------------------------------------------------


def _minutes(cells: list[datetime]) -> list[int]:
    return [int((c - T0).total_seconds() // 60) for c in cells]


@pytest.mark.parametrize(
    ("buffer", "expected_cells"),
    [(0, 6), (10, 8), (15, 9)],
)
def test_cell_range_aligned_30_minutes_with_buffer(buffer, expected_cells):
    cells = cell_range(T0, T1, buffer)
    assert len(cells) == expected_cells
    assert _minutes(cells) == list(range(0, 5 * expected_cells, 5))
    for c in cells:
        assert c.tzinfo is not None and c.utcoffset() == timedelta(0)


def test_cell_range_misaligned_input_floors_and_ceils():
    # Defensive only: offered times are always aligned (spec 7.4 "Claim cells").
    start = T0 + timedelta(minutes=2, seconds=30)
    end = start + timedelta(minutes=30)  # 14:32:30
    cells = cell_range(start, end, 0)
    assert cells[0] == T0
    assert cells[-1] == T0 + timedelta(minutes=30)  # ceil5(14:32:30) = 14:35, exclusive
    assert len(cells) == 7


def test_zero_offset_zone_is_normalized_to_utc():
    # Europe/London is UTC+0 in winter, but has DST: arithmetic must happen in UTC.
    from zoneinfo import ZoneInfo

    london = datetime(2026, 12, 7, 14, 0, tzinfo=ZoneInfo("Europe/London"))
    cells = cell_range(london, london + timedelta(minutes=30), 0)
    assert all(c.tzinfo is UTC for c in cells)
    assert CellKey(_cal_key(), london).start.tzinfo is UTC
    assert CellKey(_cal_key(), london) == CellKey(_cal_key(), datetime(2026, 12, 7, 14, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("minutes", "buffer"),
    [(245, 0), (235, 10), (5, 245), (60 * 24 * 365, 0)],
    ids=["duration", "duration-plus-buffer", "buffer", "a-year"],
)
def test_cell_range_refuses_more_than_the_bindings_limit(minutes, buffer):
    with pytest.raises(ValueError):
        cell_range(T0, T0 + timedelta(minutes=minutes), buffer)


def test_cell_range_240_minutes_is_48_cells():
    assert len(cell_range(T0, T0 + timedelta(minutes=240), 0)) == 48
    assert len(cell_range(T0, T0 + timedelta(minutes=210), 30)) == 48


@pytest.mark.parametrize(
    ("start", "end", "buffer"),
    [
        (T0, T0, 0),
        (T1, T0, 0),
        (T0, T1, -5),
        (datetime(2026, 10, 5, 14, 0), T1, 0),
    ],
    ids=["empty", "reversed", "negative-buffer", "naive"],
)
def test_cell_range_rejects_bad_input(start, end, buffer):
    with pytest.raises(ValueError):
        cell_range(start, end, buffer)


def test_cell_range_rejects_non_int_buffer():
    with pytest.raises(TypeError):
        cell_range(T0, T1, 5.0)  # type: ignore[arg-type]


# --- CellKey --------------------------------------------------------------------


def test_cell_key_name_has_no_colons():
    key = CellKey(_cal_key(), T0)
    assert key.name == f"{_cal_key()}/20261005T1400Z"
    assert ":" not in key.name


def test_cell_key_is_frozen_and_hashable():
    a = CellKey(_cal_key(), T0)
    b = CellKey(_cal_key(), T0)
    assert a == b and hash(a) == hash(b)
    with pytest.raises(AttributeError):
        a.start = T1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("cal", "start"),
    [
        ("not-hex", T0),
        ("AB" * 32, T0),
        ("ab" * 31, T0),
        ("../" + "a" * 61, T0),
        ("ab" * 32, T0 + timedelta(minutes=2)),
        ("ab" * 32, T0 + timedelta(seconds=30)),
        ("ab" * 32, datetime(2026, 10, 5, 14, 0)),
        ("ab" * 32, datetime(2026, 10, 5, 10, 0, tzinfo=timezone(timedelta(hours=-4)))),
    ],
    ids=["not-hex", "uppercase", "short", "path", "unaligned", "seconds", "naive", "non-utc"],
)
def test_cell_key_rejects_bad_parts(cal, start):
    with pytest.raises(ValueError):
        CellKey(cal, start)


def test_cell_key_repr_hides_nothing_sensitive_but_is_readable():
    # The calendar_key is an HMAC, not the calendar ID, so it is safe to show.
    assert "20261005T1400Z" in repr(CellKey(_cal_key(), T0))


def test_range_ending_past_datetime_max_is_value_error() -> None:
    """UC04a cso r2 follow-up (UC04b): never an OverflowError."""
    start = datetime(9999, 12, 31, 23, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        cell_range(start, start + timedelta(minutes=30), 240 - 30)
