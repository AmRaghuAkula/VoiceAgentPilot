"""The claim store (spec 7.4 "The claim store"; plan UC04a).

Time is divided into 5-minute UTC cells per physical calendar (`CellKey`). A
booking holds every cell of its range, and a cell has exactly one holder; that
property carries all of the booking algorithm's correctness.

`ClaimStore` is the core interface. Each operation is atomic on one cell and
read-your-writes consistent:

| Operation | Behavior |
| --- | --- |
| `try_claim(key, record)` | Create-if-absent. `Claimed(etag)` or `Held(record, etag, age)` |
| `replace(key, record, etag)` | Compare-and-swap; returns the new etag |
| `release(key, etag)` | Conditional delete |
| `read(key)` | `Held(record, etag, age)`, or `None` for an empty cell |

`replace` raises `PreconditionFailed` when the etag is not the cell's current
one, including when the cell is empty (the caller lost the race and re-reads;
the Blob store must map a 404 on its conditional `PUT` the same way).
`release` raises `PreconditionFailed` on an etag mismatch, but releasing an
**empty** cell succeeds as a no-op (plan UC06: "404 counts as released"); that is
safe, because a cell claimed again since carries a new etag and so still fails
the precondition. Every failure to reach the store, including a
stored record that cannot be parsed, is `ClaimStoreUnavailable`; it never
passes for an empty cell. A record handed to `try_claim`/`replace` must belong
to the cell (same calendar key, cell inside its range), else `ValueError`
before any I/O.

`age` is storage-server time only (round-3 SF-1): the store's own clock, never
the caller's, so instance clock skew can't make a live claim look stale.

One store instance serves every calendar. `FakeClaimStore` (below) is the
in-memory implementation; the Azure Blob one lives outside core (UC06).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from calendar_tools.core.bindings import BINDING_ID_PATTERN, MAX_DURATION_PLUS_BUFFER
from calendar_tools.core.clock import Clock
from calendar_tools.core.encoding import encode_time
from calendar_tools.core.identity import CellKey, floor5, last_cell
from calendar_tools.core.ports import INVALID_REASON, is_reason_code

__all__ = [
    "CLAIM_RECORD_VERSION",
    "OPERATIONS",
    "ClaimHooks",
    "ClaimRecord",
    "ClaimRecordInvalid",
    "ClaimState",
    "ClaimStore",
    "ClaimStoreUnavailable",
    "Claimed",
    "FakeClaimStore",
    "Held",
    "PreconditionFailed",
]

CLAIM_RECORD_VERSION = 1
OPERATIONS: tuple[str, ...] = ("try_claim", "replace", "release", "read")

ClaimState = Literal["pending", "booked"]

_HEX64 = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{16,64}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z")
_MAX_EVENT_ID = 1024
_FIELDS: tuple[str, ...] = (
    "v",
    "calendar_key",
    "first_cell",
    "last_cell",
    "start",
    "end",
    "buffer_minutes",
    "fingerprint",
    "attempt_id",
    "contact_tag",
    "binding_id",
    "state",
    "event_id",
)


# --- exceptions --------------------------------------------------------------------


class ClaimRecordInvalid(ValueError):
    """A stored record is not exactly a v1 claim record. Names the problem only,
    never stored content."""


class PreconditionFailed(Exception):
    """A compare-and-swap lost: the etag is not the cell's current one (or the
    cell is empty). Control flow for the booking algorithm, never an outcome."""


class ClaimStoreUnavailable(Exception):
    """The claim store could not be reached, timed out, or returned something
    unusable. Maps to `calendar_unavailable` before a create (spec F14).

    Like the provider exceptions, it carries the spec section 10 diagnostic and an
    optional reason code; anything that is not a code is replaced, so vendor
    text can never reach `str()` or the log line.
    """

    diagnostic: ClassVar[str] = "claim_store_unreachable"

    def __init__(self, reason: str | None = None) -> None:
        if reason is not None and not is_reason_code(reason):
            reason = INVALID_REASON
        self.reason = reason
        super().__init__(self._text())

    def _text(self) -> str:
        return self.diagnostic if self.reason is None else f"{self.diagnostic}: {self.reason}"

    def __str__(self) -> str:
        return self._text()


# --- the record ---------------------------------------------------------------------


def _check(ok: bool, problem: str) -> None:
    if not ok:
        raise ClaimRecordInvalid(problem)


def _is_utc_minute(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and value.second == 0
        and value.microsecond == 0
    )


def _valid_event_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_EVENT_ID and value.isprintable()


@dataclass(frozen=True)
class ClaimRecord:
    """One cell's claim (spec 7.4): no contact PII, only HMACs, times, a nonce
    and the binding ID. Every cell of one attempt carries the same record."""

    calendar_key: str
    first_cell: datetime
    last_cell: datetime
    start: datetime
    end: datetime
    buffer_minutes: int
    fingerprint: str
    attempt_id: str
    contact_tag: str
    binding_id: str
    state: ClaimState
    event_id: str | None = None
    v: int = CLAIM_RECORD_VERSION

    def __post_init__(self) -> None:
        _check(self.v == CLAIM_RECORD_VERSION and type(self.v) is int, "version")
        for name in ("calendar_key", "fingerprint", "contact_tag"):
            value = getattr(self, name)
            _check(isinstance(value, str) and bool(_HEX64.fullmatch(value)), name)
        _check(isinstance(self.attempt_id, str) and bool(_ATTEMPT_ID.fullmatch(self.attempt_id)), "attempt_id")
        _check(isinstance(self.binding_id, str) and bool(BINDING_ID_PATTERN.fullmatch(self.binding_id)), "binding_id")
        for name in ("first_cell", "last_cell", "start", "end"):
            _check(_is_utc_minute(getattr(self, name)), name)
        _check(type(self.buffer_minutes) is int, "buffer_minutes")
        _check(self.buffer_minutes >= 0 and self.buffer_minutes % 5 == 0, "buffer_minutes")
        _check(self.end > self.start, "end")
        # A booking never spans more than duration + buffer allows (spec 4.1), so
        # a corrupt or hostile record can't describe a huge range.
        span = self.end - self.start + timedelta(minutes=self.buffer_minutes)
        _check(span <= timedelta(minutes=MAX_DURATION_PLUS_BUFFER), "end")
        _check(self.first_cell == floor5(self.start), "first_cell")
        _check(self.last_cell == last_cell(self.start, self.end, self.buffer_minutes), "last_cell")
        if self.state == "pending":
            _check(self.event_id is None, "event_id")
        elif self.state == "booked":
            _check(_valid_event_id(self.event_id), "event_id")
        else:
            raise ClaimRecordInvalid("state")

    @classmethod
    def pending(
        cls,
        *,
        calendar_key: str,
        start: datetime,
        end: datetime,
        buffer_minutes: int,
        fingerprint: str,
        attempt_id: str,
        contact_tag: str,
        binding_id: str,
    ) -> ClaimRecord:
        """A new `pending` record; the cell range is derived, never passed in."""
        if isinstance(buffer_minutes, bool) or not isinstance(buffer_minutes, int):
            raise TypeError("buffer_minutes must be an int")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise TypeError("start and end must be datetimes")
        return cls(
            calendar_key=calendar_key,
            first_cell=floor5(start),
            last_cell=last_cell(start, end, buffer_minutes),
            start=start,
            end=end,
            buffer_minutes=buffer_minutes,
            fingerprint=fingerprint,
            attempt_id=attempt_id,
            contact_tag=contact_tag,
            binding_id=binding_id,
            state="pending",
        )

    def booked(self, event_id: str) -> ClaimRecord:
        """The same record, finalized with the created event's ID (step 8)."""
        if not isinstance(event_id, str):
            raise TypeError("event_id must be a str")
        return replace(self, state="booked", event_id=event_id)

    def covers(self, key: CellKey) -> bool:
        return key.calendar_key == self.calendar_key and self.first_cell <= key.start <= self.last_cell

    def to_json(self) -> bytes:
        doc: dict[str, Any] = {}
        for name in _FIELDS:
            value = getattr(self, name)
            doc[name] = encode_time(value) if isinstance(value, datetime) else value
        return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> ClaimRecord:
        """Parse a stored record strictly: exactly the v1 fields, nothing else."""
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            doc = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            raise ClaimRecordInvalid("not_json") from None
        _check(isinstance(doc, dict), "not_object")
        _check(set(doc) == set(_FIELDS), "fields")
        values: dict[str, Any] = {}
        for name in _FIELDS:
            value = doc[name]
            if name in ("first_cell", "last_cell", "start", "end"):
                _check(isinstance(value, str) and bool(_TIME.fullmatch(value)), name)
                try:
                    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    raise ClaimRecordInvalid(name) from None
            values[name] = value
        try:
            return cls(**values)
        except ClaimRecordInvalid:
            raise
        except (TypeError, ValueError):
            raise ClaimRecordInvalid("fields") from None


def check_record_for(key: CellKey, record: ClaimRecord) -> None:
    """A record may only be written to a cell inside its own range."""
    if not isinstance(key, CellKey):
        raise TypeError("key must be a CellKey")
    if not isinstance(record, ClaimRecord):
        raise TypeError("record must be a ClaimRecord")
    if not record.covers(key):
        raise ValueError("the record does not cover this cell")


# --- results and the interface ------------------------------------------------------


@dataclass(frozen=True)
class Claimed:
    etag: str


@dataclass(frozen=True)
class Held:
    record: ClaimRecord
    etag: str
    age: timedelta  # storage-server time since the cell's last write


@runtime_checkable
class ClaimStore(Protocol):
    async def try_claim(self, key: CellKey, record: ClaimRecord) -> Claimed | Held: ...

    async def replace(self, key: CellKey, record: ClaimRecord, etag: str) -> str: ...

    async def release(self, key: CellKey, etag: str) -> None: ...

    async def read(self, key: CellKey) -> Held | None: ...


class ClaimHooks(Protocol):
    """Test seam: awaited before an operation touches the cell, and after it has
    committed (before it returns). See `tests/fakes/claim_hooks.py`."""

    async def before(self, operation: str, key: CellKey) -> None: ...

    async def after(self, operation: str, key: CellKey) -> None: ...


# --- the in-memory fake ---------------------------------------------------------------


@dataclass
class _Cell:
    data: bytes  # the record as the store holds it (JSON), parsed on every read
    etag: str
    last_modified: datetime


@dataclass
class _Failure:
    exc: ClaimStoreUnavailable
    after_commit: bool


class FakeClaimStore:
    """In-memory `ClaimStore` (spec 7.4) for tests and the conformance suite.

    - `server_clock`: the store's own clock. `age` = server now - last write,
      never negative; the caller's clock is never consulted.
    - `fail_next(op, exc=None, after_commit=False)`: the next `op` raises
      `exc` (default `ClaimStoreUnavailable("injected")`), one-shot, queued per
      operation. With `after_commit=True` the write is applied first, then the
      call raises: a response lost after the store committed. A lost
      compare-and-swap commits nothing, so it leaves such a failure armed.
    - `plant_raw(key, data)`: store unparseable content (a corrupt blob).
    - `hooks`: an optional `ClaimHooks` (barriers for deterministic races).
    - `calls`, `records()`: what happened and what is held, for assertions.

    Each operation yields to the event loop once before its check-and-set, and
    never awaits inside it, so concurrent callers interleave but each cell
    change is atomic.
    """

    def __init__(self, server_clock: Clock, hooks: ClaimHooks | None = None) -> None:
        self._clock = server_clock
        self._hooks = hooks
        self._cells: dict[CellKey, _Cell] = {}
        self._failures: dict[str, deque[_Failure]] = defaultdict(deque)
        self._etag_counter = 0
        self.calls: list[tuple[str, CellKey]] = []

    # test controls

    def fail_next(
        self, operation: str, exc: ClaimStoreUnavailable | None = None, *, after_commit: bool = False
    ) -> None:
        if operation not in OPERATIONS:
            raise ValueError("unknown claim-store operation")
        if exc is None:
            exc = ClaimStoreUnavailable("injected")
        if not isinstance(exc, ClaimStoreUnavailable):
            raise TypeError("a claim store raises only ClaimStoreUnavailable")
        self._failures[operation].append(_Failure(exc, after_commit))

    def plant_raw(self, key: CellKey, data: bytes) -> None:
        """Store arbitrary bytes in a cell, as a corrupt blob would hold them."""
        self._etag_counter += 1
        self._cells[key] = _Cell(data, f'"fake-{self._etag_counter}"', self._clock.now())

    def records(self) -> dict[CellKey, ClaimRecord]:
        return {k: ClaimRecord.from_json(c.data) for k, c in self._cells.items()}

    # ClaimStore

    async def try_claim(self, key: CellKey, record: ClaimRecord) -> Claimed | Held:
        check_record_for(key, record)
        failure = await self._enter("try_claim", key)
        existing = self._cells.get(key)
        if existing is not None:
            result: Claimed | Held = self._held(existing)
        else:
            etag = self._write(key, record)
            result = Claimed(etag)
        return await self._leave("try_claim", key, failure, result)

    async def replace(self, key: CellKey, record: ClaimRecord, etag: str) -> str:
        check_record_for(key, record)
        failure = await self._enter("replace", key)
        existing = self._cells.get(key)
        if existing is None or existing.etag != etag:
            self._requeue("replace", failure)
            raise PreconditionFailed()
        new_etag = self._write(key, record)
        return await self._leave("replace", key, failure, new_etag)

    async def release(self, key: CellKey, etag: str) -> None:
        if not isinstance(key, CellKey):
            raise TypeError("key must be a CellKey")
        failure = await self._enter("release", key)
        existing = self._cells.get(key)
        if existing is None:
            # Already gone: released (plan UC06, "404 counts as released").
            return await self._leave("release", key, failure, None)
        if existing.etag != etag:
            self._requeue("release", failure)
            raise PreconditionFailed()
        del self._cells[key]
        await self._leave("release", key, failure, None)

    async def read(self, key: CellKey) -> Held | None:
        if not isinstance(key, CellKey):
            raise TypeError("key must be a CellKey")
        failure = await self._enter("read", key)
        existing = self._cells.get(key)
        result = None if existing is None else self._held(existing)
        return await self._leave("read", key, failure, result)

    # internals

    async def _enter(self, operation: str, key: CellKey) -> _Failure | None:
        self.calls.append((operation, key))
        if self._hooks is not None:
            await self._hooks.before(operation, key)
        await asyncio.sleep(0)  # let concurrent callers interleave before the atomic part
        queue = self._failures.get(operation)
        failure = queue.popleft() if queue else None
        if failure is not None and not failure.after_commit:
            raise failure.exc
        return failure

    async def _leave(self, operation: str, key: CellKey, failure: _Failure | None, result: Any) -> Any:
        if self._hooks is not None:
            await self._hooks.after(operation, key)
        if failure is not None:
            raise failure.exc
        return result

    def _requeue(self, operation: str, failure: _Failure | None) -> None:
        # An after-commit failure is for a write that commits; a lost CAS commits
        # nothing, so the failure stays armed for the next call.
        if failure is not None:
            self._failures[operation].appendleft(failure)

    def _write(self, key: CellKey, record: ClaimRecord) -> str:
        self._etag_counter += 1
        etag = f'"fake-{self._etag_counter}"'
        self._cells[key] = _Cell(record.to_json(), etag, self._clock.now())
        return etag

    def _held(self, cell: _Cell) -> Held:
        try:
            record = ClaimRecord.from_json(cell.data)
        except ClaimRecordInvalid:
            # Unusable stored content is a store failure, never an empty cell.
            raise ClaimStoreUnavailable("record_invalid") from None
        age = max(timedelta(0), self._clock.now() - cell.last_modified)
        return Held(record, cell.etag, age)
