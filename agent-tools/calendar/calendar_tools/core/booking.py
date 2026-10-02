"""The booking algorithm (spec 7.4 steps 1-8, 7.5, 12; plan UC04b, P14, P17, P18).

`BookingService.book()` runs the spec's eight steps, one private method each,
so the code reads against the spec:

1. `_step1_validate`: body fields and contact (all bad fields reported at
   once, before any I/O), then the keys (loaded exactly once, P18), the slot
   token (version, binding, MAC, alignment, `start`; never expiry), and the
   identity HMACs (`calendar_key` needs one cached adapter call).
2. `_step2_replay_claims`: read our first cell. Our fingerprint `booked` ->
   replay (or `cancelled` if the host deleted the event); our live `pending` ->
   poll, then the SF-7 lookup; empty or another fingerprint -> continue.
3. `_step3_replay_calendar_and_limit`: `find_bookings` over
   `[now, max(now + max_days_ahead, token end))` (P14): our fingerprint ->
   replay; otherwise the per-contact limit.
4. `_step4_time_checks`: expiry, bookable hours, duration, type and required
   contact fields, minimum notice. Steps 2 and 3 run first, so a replay never
   depends on the current time or config.
5. `_step5_recheck`: `get_busy` widened by this binding's buffer; when busy,
   re-read the first cell (ours -> back to step 2); then the deadline guard.
6. `_step6_claim`: `try_claim` every cell in ascending order.
7. `_step7_create`: `create_event`.
8. `_step8_finalize`: compare-and-swap every cell to `booked` + `event_id`.

**Conservative interim (plan UC04b scope boundary; UC04c replaces each):**
- a `Held` cell of another fingerprint is always genuinely held (`taken`;
  never recovered, however stale or unverified);
- an own-fingerprint stale `pending` cell (first or later) answers
  `booking_unconfirmed`;
- an own-fingerprint cell on a later cell releases the acquired cells and
  answers `booking_unconfirmed`;
- an uncertain create (`ProviderTimeout`, `ProviderUnavailable`, or any other
  failure that may have reached the vendor) leaves the claims `pending` and
  answers `booking_unconfirmed`, with no lookup or retry.
Each can refuse a bookable slot; none can double-book or report a booking
that does not exist.

Every outcome sets the context's `diagnostic` and `reason` for the request's
one log line (plan section 1, "Reason codes"). Nothing from the request body,
no token, key, HMAC, `calendar_id` or event ID ever reaches a response
`reason`, a log line or an exception message.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

from calendar_tools.core import tokens
from calendar_tools.core.bindings import AppointmentType, Binding
from calendar_tools.core.claims import (
    ClaimRecord,
    ClaimStore,
    ClaimStoreUnavailable,
    Claimed,
    Held,
)
from calendar_tools.core.clock import Clock, NonceSource
from calendar_tools.core.contact import Contact, InvalidFields, normalize
from calendar_tools.core.content import render_event
from calendar_tools.core.deadline import (
    CLAIM_TIMEOUT,
    EXPECTED_CLAIM_LATENCY,
    PROVIDER_TIMEOUT,
    SECRET_TIMEOUT,
    Deadline,
    DeadlineExceeded,
)
from calendar_tools.core.display import format_slot
from calendar_tools.core.identity import (
    CELL_MINUTES,
    CalendarIdentityCache,
    CellKey,
    booking_ref,
    cell_range,
    contact_tag,
    fingerprint,
    floor5,
)
from calendar_tools.core.keys import KeyRing, KeySet
from calendar_tools.core.ports import (
    BookingMeta,
    BookingRecord,
    CalendarProvider,
    CalendarRef,
    NewEvent,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderTimeout,
    SecretStoreUnavailable,
)
from calendar_tools.core.slots import resolve_local

__all__ = [
    "ATTEMPT_ID_BYTES",
    "CLAIM_TTL",
    "MAX_ROUNDS",
    "POLL_INTERVAL",
    "SERVICE_TAG",
    "BookingService",
    "make_book_appointment",
]

CLAIM_TTL = timedelta(seconds=120)
POLL_INTERVAL = 0.25
SERVICE_TAG = "cal-tools-v1"  # stamped on every event this service creates
ATTEMPT_ID_BYTES = 16  # 32 hex characters (the record allows 16 to 64)
# Steps 5 and 6 may send the request back to step 2 when our own fingerprint
# turns up; bounded so two of our own attempts can never loop each other.
MAX_ROUNDS = 3
# A best-effort release still gets this long once the deadline is spent, so a
# failed attempt does not leave its cells pending until `claim_ttl`. A request
# can therefore overrun the 8 s budget by about one grace per best-effort phase
# (the `cancelled` path has two: a read and a release); UC09 measures it.
RELEASE_GRACE = 0.5

# Accepted `start` spellings: ISO 8601 date and time, optional seconds and
# fraction, optional `Z` or +HH:MM offset (a naive time is read in the
# binding's zone, spec 5.3).
_START = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?")
# A booking `start` is bounded when parsed (UC03 carried follow-up), so no
# later arithmetic on it can overflow.
_EARLIEST_START = datetime(2000, 1, 1, tzinfo=UTC)
_LATEST_START = datetime(2100, 1, 1, tzinfo=UTC)
_TYPE_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
_MAX_SLOT_ID = 128

T = TypeVar("T")


class BookingContext(Protocol):
    """What the dispatcher's `RequestContext` provides (core does not import
    the HTTP layer)."""

    deadline: Deadline
    diagnostic: str | None
    reason: str | None
    provider_ms: int | None
    replayed: bool


class _Again:
    """Sentinel: our own fingerprint turned up at step 5 or 6; go back to step 2."""


_AGAIN = _Again()
Outcome = dict[str, Any]


@dataclass
class _Request:
    """One request's validated inputs and derived identity (step 1)."""

    binding: Binding
    provider: CalendarProvider
    cal: CalendarRef
    ctx: BookingContext
    appointment_type: str
    contact: Contact
    notes: Any
    keys: KeySet
    slot: tokens.VerifiedSlot
    calendar_key: str
    contact_tag: str
    fingerprint: str
    booking_ref: str
    # Set once our own fingerprint was seen at step 5 or 6 (an attempt of ours
    # may be in flight): from then on no failure may answer
    # `calendar_unavailable` ("nothing was created"); it is `booking_unconfirmed`.
    seen_own: bool = False

    @property
    def deadline(self) -> Deadline:
        return self.ctx.deadline

    @property
    def first_cell(self) -> CellKey:
        return CellKey(self.calendar_key, floor5(self.slot.start))


# --- outcomes ---------------------------------------------------------------------


def _set(ctx: BookingContext, diagnostic: str | None, reason: str | None) -> None:
    ctx.diagnostic, ctx.reason = diagnostic, reason


def _invalid_request(ctx: BookingContext, fields: list[str]) -> Outcome:
    _set(ctx, "invalid_request", ",".join(fields))
    return {"status": "invalid_request", "detail": {"fields": fields}}


def _refusal(ctx: BookingContext, status: str, reason: str) -> Outcome:
    # A business refusal with no more specific code logs its status name and
    # `detail.reason` (spec rev 3.2 section 10).
    _set(ctx, status, reason)
    return {"status": status, "detail": {"reason": reason}}


def _taken(ctx: BookingContext, where: str) -> Outcome:
    """`slot_unavailable/taken`, logged `claim_conflict` (spec F1) with where it
    was found: `busy` (step 5, calendar busy time) or `claim_held` (step 6)."""
    _set(ctx, "claim_conflict", where)
    return {"status": "slot_unavailable", "detail": {"reason": "taken"}}


def _unavailable(ctx: BookingContext, err: BaseException | None = None, *,
                 diagnostic: str | None = None, reason: str | None = None) -> Outcome:
    """`calendar_unavailable`: nothing was created by this request."""
    if err is not None:
        diagnostic, reason = getattr(err, "diagnostic", None), getattr(err, "reason", None)
    _set(ctx, diagnostic, reason)
    return {"status": "calendar_unavailable", "retryable": True}


def _unconfirmed(ctx: BookingContext, diagnostic: str, reason: str | None = None) -> Outcome:
    """`booking_unconfirmed`: a create was (or may have been) attempted and its
    result is not known."""
    _set(ctx, diagnostic, reason)
    return {"status": "booking_unconfirmed"}


def _iso(instant: datetime, binding: Binding) -> str:
    return instant.astimezone(binding.timezone).isoformat(timespec="seconds")


def _type_ref(binding: Binding, type_id: str) -> dict[str, str]:
    # A replay answers even if the type was since removed from the binding.
    label = next((t.label for t in binding.appointment_types if t.id == type_id), type_id)
    return {"id": type_id, "label": label}


def _booked(req: _Request, start: datetime, end: datetime, *, replayed: bool) -> Outcome:
    binding = req.binding
    booking: dict[str, Any] = {
        "booking_ref": req.booking_ref,
        "start": _iso(start, binding),
        "end": _iso(end, binding),
        "display": format_slot(start, binding.timezone, binding.locale),
        "timezone": binding.timezone.key,
        "appointment_type": _type_ref(binding, req.appointment_type),
    }
    if binding.host_display_name:
        booking["host"] = {"display_name": binding.host_display_name}
    req.ctx.replayed = replayed
    return {"status": "booked", "replayed": replayed, "booking": booking}


# --- parsing ------------------------------------------------------------------------


def _parse_start(value: Any, binding: Binding) -> datetime | None:
    """The request's `start` as a UTC instant, or None. Bounded to
    2000-2099 UTC, so no later arithmetic on it can overflow."""
    if not isinstance(value, str) or not _START.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=binding.timezone)
        instant = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    if not _EARLIEST_START <= instant < _LATEST_START:
        return None
    return instant


def _within_bookable_hours(binding: Binding, start: datetime, end: datetime) -> bool:
    """`[start, end)` lies inside one of the binding's current bookable
    windows on the start's local date (windows never cross midnight)."""
    tz = binding.timezone
    day = start.astimezone(tz).date()
    weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[day.weekday()]
    for wall_start, wall_end in binding.bookable_hours.get(weekday, ()):
        if resolve_local(day, wall_start, tz) <= start and end <= resolve_local(day, wall_end, tz):
            return True
    return False


def _cells_of(record: ClaimRecord) -> list[CellKey]:
    step = timedelta(minutes=CELL_MINUTES)
    count = (record.last_cell - record.first_cell) // step + 1
    return [CellKey(record.calendar_key, record.first_cell + i * step) for i in range(count)]


# --- the service ---------------------------------------------------------------------


class BookingService:
    def __init__(
        self,
        provider_for: Callable[[Binding], CalendarProvider],
        claim_store: ClaimStore,
        keyring: KeyRing,
        identity_cache: CalendarIdentityCache,
        clock: Clock,
        nonce: NonceSource,
    ) -> None:
        self._provider_for = provider_for
        self._store = claim_store
        self._keyring = keyring
        self._identity = identity_cache
        self._clock = clock
        self._nonce = nonce

    async def book(self, binding: Binding, body: Mapping[str, Any], ctx: BookingContext) -> Outcome:
        req = await self._step1_validate(binding, body, ctx)
        if isinstance(req, dict):
            return req
        for _ in range(MAX_ROUNDS):
            outcome = await self._step2_replay_claims(req)
            if outcome is not None:
                return outcome
            outcome = await self._step3_replay_calendar_and_limit(req)
            if outcome is not None:
                return outcome
            outcome = self._step4_time_checks(req)
            if outcome is not None:
                return outcome
            cells = await self._step5_recheck(req)
            if cells is _AGAIN:
                req.seen_own = True
                continue
            if isinstance(cells, dict):
                return cells
            claimed = await self._step6_claim(req, cells)
            if claimed is _AGAIN:
                req.seen_own = True
                continue
            if isinstance(claimed, dict):
                return claimed
            record, acquired = claimed
            created = await self._step7_create(req, acquired)
            if isinstance(created, dict):
                return created
            return await self._step8_finalize(req, record, acquired, created)
        # Our own attempt kept appearing and disappearing: say "unknown", never "taken".
        return _unconfirmed(ctx, "replay_poll_timeout", "replay_loop")

    # --- bounded external calls (spec 7.5) -----------------------------------------

    async def _provider_call(self, req: _Request, call: Callable[[], Awaitable[T]], *, write: bool = False) -> T:
        """One provider call under `min(PROVIDER_TIMEOUT, remaining)`. A
        timeout on a write may have reached the vendor (`maybe_committed`)."""
        timeout = req.deadline.timeout_for(PROVIDER_TIMEOUT)
        started = self._clock.now()
        try:
            return await asyncio.wait_for(call(), timeout)
        except TimeoutError:
            if write:
                raise ProviderTimeout(maybe_committed=True, reason="timeout") from None
            if timeout < PROVIDER_TIMEOUT:
                raise DeadlineExceeded() from None
            raise ProviderTimeout(maybe_committed=False, reason="timeout") from None
        finally:
            elapsed = round((self._clock.now() - started).total_seconds() * 1000)
            req.ctx.provider_ms = (req.ctx.provider_ms or 0) + elapsed

    async def _claim_call(self, req: _Request, call: Callable[[], Awaitable[T]], reserve: float = 0.0) -> T:
        """One claim-store call under `min(CLAIM_TIMEOUT, remaining - reserve)`."""
        timeout = req.deadline.timeout_for(CLAIM_TIMEOUT, reserve)
        try:
            return await asyncio.wait_for(call(), timeout)
        except TimeoutError:
            if timeout < CLAIM_TIMEOUT:
                raise DeadlineExceeded() from None
            raise ClaimStoreUnavailable("timeout") from None

    async def _load_keys(self, deadline: Deadline) -> KeySet:
        timeout = deadline.timeout_for(SECRET_TIMEOUT)
        try:
            return await asyncio.wait_for(self._keyring.load(deadline), timeout)
        except TimeoutError:
            if timeout < SECRET_TIMEOUT:
                raise DeadlineExceeded() from None
            raise SecretStoreUnavailable("timeout") from None

    def _release_timeout(self, deadline: Deadline) -> float:
        return max(min(CLAIM_TIMEOUT, deadline.remaining()), RELEASE_GRACE)

    async def _release_all(self, req: _Request, acquired: list[tuple[CellKey, str]]) -> None:
        """Best effort, in parallel. A cell whose release fails stays `pending`
        with our `attempt_id` and is recovered after `claim_ttl` (spec 7.4)."""
        if not acquired:
            return
        timeout = self._release_timeout(req.deadline)
        await asyncio.gather(
            *(asyncio.wait_for(self._store.release(key, etag), timeout) for key, etag in acquired),
            return_exceptions=True,
        )

    async def _for_attempt(self, req: _Request, record: ClaimRecord, state: str) -> list[tuple[CellKey, Held]]:
        """The cells of `record`'s range that still carry its attempt in
        `state` (best effort: an unreadable cell is left alone)."""
        timeout = self._release_timeout(req.deadline)
        cells = _cells_of(record)
        results = await asyncio.gather(
            *(asyncio.wait_for(self._store.read(key), timeout) for key in cells), return_exceptions=True
        )
        matched: list[tuple[CellKey, Held]] = []
        for key, held in zip(cells, results, strict=True):
            if (
                isinstance(held, Held)
                and held.record.attempt_id == record.attempt_id
                and held.record.fingerprint == record.fingerprint
                and held.record.state == state
                and held.record.event_id == record.event_id
            ):
                matched.append((key, held))
        return matched

    async def _finalize_cells(self, req: _Request, cells: list[tuple[CellKey, str]],
                              record: ClaimRecord, event_id: str) -> bool:
        """Compare-and-swap each cell to `booked` + `event_id`, in parallel.
        True only if every write succeeded."""
        try:
            booked = record.booked(event_id)
        except (TypeError, ValueError):  # an event ID the record cannot hold
            return False
        try:
            timeout = req.deadline.timeout_for(CLAIM_TIMEOUT)
        except DeadlineExceeded:
            return False
        results = await asyncio.gather(
            *(asyncio.wait_for(self._store.replace(key, booked, etag), timeout) for key, etag in cells),
            return_exceptions=True,
        )
        return bool(cells) and not any(isinstance(r, BaseException) for r in results)

    # --- step 1 ------------------------------------------------------------------------

    async def _step1_validate(self, binding: Binding, body: Mapping[str, Any], ctx: BookingContext) -> _Request | Outcome:
        bad: list[str] = []
        slot_id = body.get("slot_id")
        if not isinstance(slot_id, str) or not 0 < len(slot_id) <= _MAX_SLOT_ID:
            bad.append("slot_id")
        start = _parse_start(body.get("start"), binding)
        if start is None:
            bad.append("start")
        type_id = body.get("appointment_type")
        if not isinstance(type_id, str) or not _TYPE_ID.fullmatch(type_id):
            bad.append("appointment_type")
        raw_contact = body.get("contact")
        contact: Contact | InvalidFields
        if raw_contact is None:
            contact = InvalidFields(("contact",))
        else:
            contact = normalize(binding, raw_contact)
        if isinstance(contact, InvalidFields):
            bad.extend(contact.fields)
        notes = body.get("notes")
        if not binding.accept_notes:
            notes = None  # ignored entirely (spec 5.3)
        elif notes is not None and not isinstance(notes, str):
            bad.append("notes")
        if bad:
            return _invalid_request(ctx, bad)
        assert isinstance(slot_id, str) and start is not None and isinstance(type_id, str)
        assert isinstance(contact, Contact)

        # P18: exactly one key load, after the body checks and before the token.
        try:
            keys = await self._load_keys(ctx.deadline)
        except (SecretStoreUnavailable, DeadlineExceeded) as err:
            return _unavailable(ctx, err)

        verified = tokens.verify(keys, binding.binding_id, slot_id, start)
        if isinstance(verified, tokens.InvalidSlot):
            return _refusal(ctx, "invalid_slot", verified.reason)

        provider = self._provider_for(binding)
        cal = binding.calendar_ref
        try:
            cal_key = await self._identity.calendar_key(provider, cal, keys.fingerprint, ctx.deadline)
        except (ProviderError, DeadlineExceeded) as err:
            return _unavailable(ctx, err)
        tag = contact_tag(keys.fingerprint, contact)
        fp = fingerprint(keys.fingerprint, cal_key, verified.start, verified.end, tag, type_id)
        return _Request(
            binding=binding,
            provider=provider,
            cal=cal,
            ctx=ctx,
            appointment_type=type_id,
            contact=contact,
            notes=notes,
            keys=keys,
            slot=verified,
            calendar_key=cal_key,
            contact_tag=tag,
            fingerprint=fp,
            booking_ref=booking_ref(keys.fingerprint, fp),
        )

    # --- step 2 ------------------------------------------------------------------------

    async def _step2_replay_claims(self, req: _Request) -> Outcome | None:
        try:
            held = await self._claim_call(req, lambda: self._store.read(req.first_cell))
        except (ClaimStoreUnavailable, DeadlineExceeded) as err:
            if req.seen_own:
                return _unconfirmed(req.ctx, err.diagnostic, getattr(err, "reason", None))
            return _unavailable(req.ctx, err)  # nothing claimed or created yet
        if held is None or held.record.fingerprint != req.fingerprint:
            return None  # empty or another fingerprint: decided at step 6
        record = held.record
        if record.state == "booked":
            return await self._replay_booked(req, record)
        if held.age >= CLAIM_TTL:
            # Interim (UC04c recovers it): our own stale attempt, outcome unknown.
            return _unconfirmed(req.ctx, "create_unconfirmed", "stale_own_claim")
        return await self._poll_own_pending(req, record)

    async def _replay_booked(self, req: _Request, record: ClaimRecord) -> Outcome:
        event_id = record.event_id
        assert event_id is not None  # a booked record always has one
        try:
            event = await self._provider_call(req, lambda: req.provider.get_event(req.cal, event_id))
        except (ProviderError, DeadlineExceeded) as err:
            return _unavailable(req.ctx, err)
        if event is not None:
            return _booked(req, event.start, event.end, replayed=True)
        # The host deleted it: free our cells (this attempt's, this event's only).
        cells = await self._for_attempt(req, record, "booked")
        await self._release_all(req, [(key, held.etag) for key, held in cells])
        return _refusal(req.ctx, "slot_unavailable", "cancelled")

    async def _poll_own_pending(self, req: _Request, record: ClaimRecord) -> Outcome | None:
        """Another attempt of ours is in flight (the retry-during-create case).
        Poll until only the SF-7 lookup, one read and one compare-and-swap of
        the attempt's cells still fit (S3).
        Having seen our own pending claim, no failure here may answer
        `calendar_unavailable` ("nothing was created"): it is `booking_unconfirmed`."""
        # The SF-7 path needs one lookup, one parallel read of the attempt's
        # cells and one parallel compare-and-swap.
        reserve = PROVIDER_TIMEOUT + 2 * CLAIM_TIMEOUT
        ctx = req.ctx
        while req.deadline.remaining() - POLL_INTERVAL >= reserve:
            await self._clock.sleep(POLL_INTERVAL)
            try:
                held = await self._claim_call(req, lambda: self._store.read(req.first_cell), reserve)
            except DeadlineExceeded:
                break
            except ClaimStoreUnavailable as err:
                return _unconfirmed(ctx, err.diagnostic, err.reason)
            if held is None or held.record.fingerprint != req.fingerprint:
                return None  # it disappeared: continue at step 3
            if held.record.state == "booked":
                return _booked(req, req.slot.start, req.slot.end, replayed=True)
            if held.age >= CLAIM_TTL:
                return _unconfirmed(ctx, "create_unconfirmed", "stale_own_claim")
            record = held.record
        # SF-7: the original may have created the event without finalizing.
        try:
            found = await self._provider_call(
                req,
                lambda: req.provider.find_bookings(req.cal, req.slot.start, req.slot.end, record.binding_id),
            )
        except (ProviderError, DeadlineExceeded):
            return _unconfirmed(ctx, "replay_poll_timeout", "lookup_failed")
        ours = [r for r in found if r.meta.fingerprint == req.fingerprint]
        if not ours:
            return _unconfirmed(ctx, "replay_poll_timeout")
        event = ours[0]
        cells = await self._for_attempt(req, record, "pending")
        finalized = await self._finalize_cells(req, [(k, h.etag) for k, h in cells], record, event.event_id)
        outcome = _booked(req, event.start, event.end, replayed=True)
        if not finalized:
            _set(ctx, "claim_finalize_failed", None)
        return outcome

    # --- step 3 ------------------------------------------------------------------------

    async def _step3_replay_calendar_and_limit(self, req: _Request) -> Outcome | None:
        now = self._clock.now()
        until = max(now + timedelta(days=req.binding.max_days_ahead), req.slot.end)  # P14
        try:
            records: list[BookingRecord] = await self._provider_call(
                req, lambda: req.provider.find_bookings(req.cal, now, until, req.binding.binding_id)
            )
        except (ProviderError, DeadlineExceeded) as err:
            return _unavailable(req.ctx, err)
        for found in records:
            if found.meta.fingerprint == req.fingerprint:
                return _booked(req, found.start, found.end, replayed=True)
        active = sum(1 for found in records if found.meta.contact_tag == req.contact_tag)
        if active >= req.binding.max_active_bookings_per_contact:
            _set(req.ctx, "limit_reached", None)
            return {"status": "limit_reached"}
        return None

    # --- step 4 ------------------------------------------------------------------------

    def _step4_time_checks(self, req: _Request) -> Outcome | None:
        binding, slot, ctx = req.binding, req.slot, req.ctx
        now = self._clock.now()
        if isinstance(tokens.check_expiry(slot, now), tokens.InvalidSlot):
            return _refusal(ctx, "invalid_slot", "expired")
        if not _within_bookable_hours(binding, slot.start, slot.end):
            return _refusal(ctx, "invalid_slot", "outside_bookable_hours")
        if slot.duration_minutes not in binding.allowed_durations_minutes:
            return _refusal(ctx, "invalid_slot", "duration_not_allowed")
        bad: list[str] = []
        if self._appointment_type(req) is None:
            bad.append("appointment_type")
        for name in ("name", "phone", "email"):
            if name in binding.required_contact_fields and getattr(req.contact, name) is None:
                bad.append(f"contact.{name}")
        if bad:
            return _invalid_request(ctx, bad)
        if slot.start < now + timedelta(minutes=binding.min_notice_minutes):
            return _refusal(ctx, "slot_unavailable", "too_soon")
        return None

    def _appointment_type(self, req: _Request) -> AppointmentType | None:
        return next((t for t in req.binding.appointment_types if t.id == req.appointment_type), None)

    # --- step 5 ------------------------------------------------------------------------

    async def _step5_recheck(self, req: _Request) -> list[CellKey] | Outcome | _Again:
        binding, slot = req.binding, req.slot
        buffer = timedelta(minutes=binding.buffer_minutes)
        try:
            cells = [CellKey(req.calendar_key, t) for t in cell_range(slot.start, slot.end, binding.buffer_minutes)]
        except (ValueError, OverflowError):
            # Unreachable once step 4 passed (spec 4.1 caps duration + buffer).
            return _unavailable(req.ctx, diagnostic="binding_config_error", reason="cell_range")
        lo, hi = slot.start - buffer, slot.end + buffer
        try:
            busy = await self._provider_call(req, lambda: req.provider.get_busy(req.cal, lo, hi))
        except (ProviderError, DeadlineExceeded) as err:
            return _unavailable(req.ctx, err)
        if any(b.start < hi and b.end > lo for b in busy):
            # Before answering `taken`: a concurrent attempt of ours may have
            # claimed and created in the meantime.
            try:
                held = await self._claim_call(req, lambda: self._store.read(req.first_cell))
            except (ClaimStoreUnavailable, DeadlineExceeded) as err:
                # The busy time may be our own concurrent booking: unknown.
                return _unconfirmed(req.ctx, err.diagnostic, getattr(err, "reason", None))
            if held is not None and held.record.fingerprint == req.fingerprint:
                return _AGAIN
            return _taken(req.ctx, "busy")
        # The deadline guard (S3, P17): the claims, one create and one parallel
        # finalize must fit, in integer milliseconds; otherwise nothing is claimed.
        need_ms = len(cells) * round(EXPECTED_CLAIM_LATENCY * 1000) + round(PROVIDER_TIMEOUT * 1000) + round(
            CLAIM_TIMEOUT * 1000
        )
        if round(req.deadline.remaining() * 1000) < need_ms:
            return _unavailable(req.ctx, diagnostic="deadline_exceeded")
        return cells

    # --- step 6 ------------------------------------------------------------------------

    async def _step6_claim(
        self, req: _Request, cells: list[CellKey]
    ) -> tuple[ClaimRecord, list[tuple[CellKey, str]]] | Outcome | _Again:
        slot = req.slot
        record = ClaimRecord.pending(
            calendar_key=req.calendar_key,
            start=slot.start,
            end=slot.end,
            buffer_minutes=req.binding.buffer_minutes,
            fingerprint=req.fingerprint,
            attempt_id=self._nonce.new_hex(ATTEMPT_ID_BYTES),
            contact_tag=req.contact_tag,
            binding_id=req.binding.binding_id,
        )
        acquired: list[tuple[CellKey, str]] = []
        for index, key in enumerate(cells):
            try:
                # Each claim keeps the create's and the finalize's budget in
                # reserve, so a slow store cannot starve the create (the step-5
                # guard's invariant, held at run time).
                result = await self._claim_call(
                    req, lambda key=key: self._store.try_claim(key, record), PROVIDER_TIMEOUT + CLAIM_TIMEOUT
                )
            except (ClaimStoreUnavailable, DeadlineExceeded) as err:
                await self._release_all(req, acquired)
                return _unavailable(req.ctx, err)
            if isinstance(result, Claimed):
                acquired.append((key, result.etag))
                continue
            held = result
            if held.record.fingerprint == req.fingerprint:
                if index == 0:
                    return _AGAIN  # another attempt of ours got there first
                # A leftover of ours on a later cell (SF-3). Interim: UC04c
                # recovers a stale one and waits on a live one.
                await self._release_all(req, acquired)
                stale = held.record.state == "pending" and held.age >= CLAIM_TTL
                return _unconfirmed(req.ctx, "create_unconfirmed", "stale_own_claim" if stale else "own_claim_live")
            # Interim: every other holder counts as genuine (UC04c recovers
            # stale and unverified ones).
            await self._release_all(req, acquired)
            return _taken(req.ctx, "claim_held")
        return record, acquired

    # --- step 7 ------------------------------------------------------------------------

    async def _step7_create(self, req: _Request, acquired: list[tuple[CellKey, str]]) -> BookingRecord | Outcome:
        binding, slot = req.binding, req.slot
        appointment_type = self._appointment_type(req)
        assert appointment_type is not None  # step 4
        title, description = render_event(
            binding, req.contact, appointment_type, slot.duration_minutes, req.booking_ref, req.notes
        )
        event = NewEvent(
            start=slot.start,
            end=slot.end,
            timezone=binding.timezone.key,
            title=title,
            description=description,
            meta=BookingMeta(
                service_tag=SERVICE_TAG,
                binding_id=binding.binding_id,
                fingerprint=req.fingerprint,
                contact_tag=req.contact_tag,
                booking_ref=req.booking_ref,
            ),
            request_key=req.fingerprint,
        )
        try:
            return await self._provider_call(req, lambda: req.provider.create_event(req.cal, event), write=True)
        except DeadlineExceeded as err:
            # Raised before the call was made: nothing reached the vendor.
            await self._release_all(req, acquired)
            return _unavailable(req.ctx, err)
        except (ProviderAuthError, ProviderConfigError, SecretStoreUnavailable) as err:
            # The vendor (or the credential source) definitively refused: no event.
            await self._release_all(req, acquired)
            return _unavailable(req.ctx, err)
        except ProviderError as err:
            # Uncertain (ProviderTimeout, ProviderUnavailable, anything else).
            # Interim: the claims stay pending; UC04c adds the lookup and retry.
            return _unconfirmed(req.ctx, "create_unconfirmed", err.diagnostic)

    # --- step 8 ------------------------------------------------------------------------

    async def _step8_finalize(
        self, req: _Request, record: ClaimRecord, acquired: list[tuple[CellKey, str]], created: BookingRecord
    ) -> Outcome:
        finalized = await self._finalize_cells(req, acquired, record, created.event_id)
        outcome = _booked(req, created.start, created.end, replayed=False)
        if not finalized:
            # Still booked: the event exists and the pending claims keep the slot
            # protected (spec 7.4 step 8, F14).
            _set(req.ctx, "claim_finalize_failed", None)
        return outcome


def make_book_appointment(service: BookingService) -> Callable[[Binding, dict[str, Any], Any], Awaitable[Outcome]]:
    """UC02b's `Operation` shape (UC09 wiring)."""

    async def operation(binding: Binding, body: dict[str, Any], ctx: Any) -> Outcome:
        return await service.book(binding, body, ctx)

    return operation
