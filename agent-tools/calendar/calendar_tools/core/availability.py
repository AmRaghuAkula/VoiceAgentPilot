"""The `check_availability` use case (spec sections 5.2, 7.1, 7.5, 10; plan
UC03, P18).

Order: validate the body -> resolve the date range -> a first pass of the slot
engine without busy time (a range with no bookable time, or past the horizon,
answers `no_availability` with no key or calendar call) -> load the keys once
(P18) -> `get_busy` over the bookable span widened by the buffer -> the slot
engine -> one signed token per offered slot.

Every outcome sets the context's `diagnostic` and `reason` for the request's
one log line (plan section 1, "Reason codes"): a business refusal uses its
status name and the field names; a `calendar_unavailable` copies the mapped
exception's own diagnostic and reason. Nothing from the request body is ever
echoed into a response, a reason or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from calendar_tools.core import tokens
from calendar_tools.core.bindings import Binding
from calendar_tools.core.clock import Clock
from calendar_tools.core.deadline import PROVIDER_TIMEOUT, SECRET_TIMEOUT, Deadline, DeadlineExceeded
from calendar_tools.core.display import format_slot
from calendar_tools.core.keys import KeyRing, KeySet
from calendar_tools.core.ports import (
    CalendarProvider,
    Interval,
    ProviderError,
    ProviderTimeout,
    SecretStoreUnavailable,
)
from calendar_tools.core.slots import SlotResult, compute_slots, resolve_local

__all__ = ["OperationContext", "check_availability", "make_check_availability"]

logger = logging.getLogger("calendar_tools.core.availability")

REQUEST_FIELDS = ("from_date", "to_date", "earliest_time", "latest_time", "duration_minutes")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class OperationContext(Protocol):
    """What the dispatcher's `RequestContext` provides (core does not import
    the HTTP layer)."""

    deadline: Deadline
    diagnostic: str | None
    reason: str | None
    provider_ms: int | None


class _Unavailable(Exception):
    """Internal: answer `calendar_unavailable` with this diagnostic and reason."""

    def __init__(self, diagnostic: str, reason: str | None = None) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic
        self.reason = reason


# --- request parsing ------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    match = _HHMM.fullmatch(value)
    return time(int(match.group(1)), int(match.group(2))) if match else None


def _parse_duration(value: Any, binding: Binding) -> int | None:
    """A JSON integer (JSON Schema's `integer` also admits an integral number
    such as 30.0) that is one of the binding's allowed durations."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    return value if value in binding.allowed_durations_minutes else None


def _parse(binding: Binding, body: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Parsed values for the fields present (null means absent), and the names
    of the invalid ones in schema order."""
    values: dict[str, Any] = {}
    bad: list[str] = []
    for name in REQUEST_FIELDS:
        raw = body.get(name)
        if raw is None:
            continue
        if name in ("from_date", "to_date"):
            parsed: Any = _parse_date(raw)
        elif name in ("earliest_time", "latest_time"):
            parsed = _parse_time(raw)
        else:
            parsed = _parse_duration(raw, binding)
        if parsed is None:
            bad.append(name)
        else:
            values[name] = parsed
    return values, bad


# --- response pieces -------------------------------------------------------------


def _iso(instant: datetime, binding: Binding) -> str:
    return instant.astimezone(binding.timezone).isoformat(timespec="seconds")


def _searched(result: SlotResult, first: date, last: date, binding: Binding) -> dict[str, str]:
    if result.searched is not None:
        start, end = result.searched.start, result.searched.end
    else:
        # No bookable window in range: the whole local days (dates within the horizon).
        tz = binding.timezone
        start = resolve_local(first, time(0, 0), tz)
        end = resolve_local(last + timedelta(days=1), time(0, 0), tz)
    return {"from": _iso(start, binding), "to": _iso(end, binding)}


def _common(binding: Binding, now: datetime, today: date, duration: int) -> dict[str, Any]:
    return {
        "timezone": binding.timezone.key,
        "now": _iso(now, binding),
        "today": {"date": today.isoformat(), "weekday": _WEEKDAY_NAMES[today.weekday()]},
        "duration_minutes": duration,
    }


def _binding_options(binding: Binding) -> dict[str, Any]:
    return {
        "appointment_types": [{"id": t.id, "label": t.label} for t in binding.appointment_types],
        "allowed_durations_minutes": list(binding.allowed_durations_minutes),
    }


def _invalid(ctx: OperationContext, binding: Binding, fields: list[str]) -> dict[str, Any]:
    detail: dict[str, Any] = {"fields": fields}
    if "duration_minutes" in fields:
        detail["allowed_durations_minutes"] = list(binding.allowed_durations_minutes)
    ctx.diagnostic, ctx.reason = "invalid_request", ",".join(fields)
    return {"status": "invalid_request", "detail": detail}


def _unavailable(ctx: OperationContext, diagnostic: str, reason: str | None) -> dict[str, Any]:
    ctx.diagnostic, ctx.reason = diagnostic, reason
    return {"status": "calendar_unavailable", "retryable": True}


# --- external calls, bounded by the deadline (spec 7.5) -----------------------------


async def _load_keys(keyring: KeyRing, deadline: Deadline) -> KeySet:
    timeout = deadline.timeout_for(SECRET_TIMEOUT)
    try:
        return await asyncio.wait_for(keyring.load(deadline), timeout)
    except TimeoutError:
        if timeout < SECRET_TIMEOUT:
            raise DeadlineExceeded() from None
        raise SecretStoreUnavailable("timeout") from None


async def _get_busy(
    provider: CalendarProvider, binding: Binding, span: Interval, deadline: Deadline
) -> list[Interval]:
    buffer = timedelta(minutes=binding.buffer_minutes)
    timeout = deadline.timeout_for(PROVIDER_TIMEOUT)
    try:
        return await asyncio.wait_for(
            provider.get_busy(binding.calendar_ref, span.start - buffer, span.end + buffer), timeout
        )
    except TimeoutError:
        if timeout < PROVIDER_TIMEOUT:
            raise DeadlineExceeded() from None
        raise ProviderTimeout(maybe_committed=False, reason="timeout") from None


def _issue_all(keys: KeySet, binding: Binding, result: SlotResult, duration: int, expiry: datetime) -> list[dict[str, str]]:
    slots: list[dict[str, str]] = []
    dropped = 0
    for slot in result.slots:
        try:
            token = tokens.issue(keys, binding.binding_id, slot.start, duration, expiry)
        except tokens.SlotMisaligned:
            dropped += 1  # a server bug: never issued (spec 7.2)
            continue
        slots.append(
            {
                "slot_id": token,
                "start": _iso(slot.start, binding),
                "end": _iso(slot.end, binding),
                "display": format_slot(slot.start, binding.timezone, binding.locale),
            }
        )
    if dropped:
        logger.warning("slot_misaligned: binding_id=%s dropped=%d", binding.binding_id, dropped)
    return slots


# --- the operation -----------------------------------------------------------------


async def check_availability(
    binding: Binding,
    body: Mapping[str, Any],
    ctx: OperationContext,
    *,
    provider: CalendarProvider,
    keyring: KeyRing,
    clock: Clock,
) -> dict[str, Any]:
    values, bad = _parse(binding, body)
    if bad:
        return _invalid(ctx, binding, bad)

    tz = binding.timezone
    now = clock.now()
    today = now.astimezone(tz).date()
    horizon = today + timedelta(days=binding.max_days_ahead)
    first = max(values.get("from_date", today), today)
    to_date: date | None = values.get("to_date")
    if to_date is not None and to_date < first:
        return _invalid(ctx, binding, ["to_date"])
    duration = values.get("duration_minutes", binding.default_duration_minutes)
    common = _common(binding, now, today, duration)

    if first > horizon:
        ctx.reason = "beyond_booking_horizon"
        return {
            "status": "no_availability",
            **common,
            "slots": [],
            "more_available": False,
            **_binding_options(binding),
            "detail": {"reason": "beyond_booking_horizon"},
        }
    if to_date is None:
        to_date = first + timedelta(days=binding.default_search_days - 1)
    last = min(to_date, horizon)

    def engine(busy: list[Interval]) -> SlotResult:
        return compute_slots(
            window_start_local=first,
            window_end_local=last,
            bookable_hours=binding.bookable_hours,
            tz=tz,
            busy=busy,
            duration=duration,
            step=binding.slot_step_minutes,
            buffer=binding.buffer_minutes,
            min_notice=binding.min_notice_minutes,
            now=now,
            horizon_end=horizon,
            earliest_time=values.get("earliest_time"),
            latest_time=values.get("latest_time"),
            selection=binding.slot_selection,
            max_slots=binding.max_slots_returned,
        )

    result = engine([])
    if result.reason == "ok":
        if result.searched is None:  # the engine found candidates, so windows exist
            raise RuntimeError("slot engine returned slots without a searched span")
        try:
            keys = await _load_keys(keyring, ctx.deadline)
            started = clock.now()
            try:
                busy = await _get_busy(provider, binding, result.searched, ctx.deadline)
            finally:
                ctx.provider_ms = round((clock.now() - started).total_seconds() * 1000)
        except (ProviderError, DeadlineExceeded) as err:
            return _unavailable(ctx, err.diagnostic, getattr(err, "reason", None))
        result = engine(busy)

    searched = _searched(result, first, last, binding)
    if result.reason != "ok":
        ctx.reason = result.reason
        return {
            "status": "no_availability",
            **common,
            "searched": searched,
            "slots": [],
            "more_available": False,
            **_binding_options(binding),
            "detail": {"reason": result.reason},
        }

    expiry = now + timedelta(minutes=binding.slot_token_ttl_minutes)
    slots = _issue_all(keys, binding, result, duration, expiry)
    if not slots:
        return _unavailable(ctx, tokens.SlotMisaligned.diagnostic, None)
    return {
        "status": "available",
        **common,
        "searched": searched,
        "slots": slots,
        "more_available": result.more_available,
        **_binding_options(binding),
    }


Operation = Callable[[Binding, dict[str, Any], OperationContext], Awaitable[dict[str, Any]]]


def make_check_availability(provider: CalendarProvider, keyring: KeyRing, clock: Clock) -> Operation:
    """Bind the dependencies, giving UC02b's `Operation` shape (UC09 wiring)."""

    async def operation(binding: Binding, body: dict[str, Any], ctx: OperationContext) -> dict[str, Any]:
        return await check_availability(binding, body, ctx, provider=provider, keyring=keyring, clock=clock)

    return operation
