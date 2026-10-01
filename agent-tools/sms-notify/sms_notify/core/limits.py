"""Dedupe with a recorded outcome, cooldown and hourly slots (spec section 5 steps 4 to 6, plan P2).

All writes are conditional (compare-and-swap) through the ``StateStore`` port, so concurrent
requests can't both win a claim. Claims are never released after a failure (fail closed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sms_notify.core.deadline import STORAGE_BUDGET, Deadline
from sms_notify.ports import StateStore

COOLDOWN_KEY = "cooldown"
OUTCOME_PENDING = "pending"
OUTCOME_SENT = "sent"


def dedupe_key(text_hash: str) -> str:
    return f"dedupe/{text_hash}"


def slot_key(now: datetime, n: int) -> str:
    return f"rate/{now:%Y%m%d%H}/{n}"


def _encode(**fields: str) -> bytes:
    return json.dumps(fields, separators=(",", ":")).encode("utf-8")


def _stamp(now: datetime) -> str:
    return now.isoformat()


def _read_at(body: bytes) -> tuple[datetime | None, str | None]:
    """Parse ``{"at", "outcome"?}``. A corrupt body reads as ``(None, None)``."""
    try:
        data = json.loads(body)
        at = datetime.fromisoformat(data["at"])
        if at.tzinfo is None:
            return None, None
        outcome = data.get("outcome")
        return at, outcome if isinstance(outcome, str) else None
    except (ValueError, KeyError, TypeError):
        return None, None


@dataclass(frozen=True)
class DedupeClaim:
    key: str
    etag: str


@dataclass(frozen=True)
class Duplicate:
    outcome: str | None


async def claim_dedupe(
    store: StateStore, text_hash: str, now: datetime, window: timedelta, deadline: Deadline
) -> DedupeClaim | Duplicate | None:
    """Claim the dedupe blob for this text.

    Returns a ``DedupeClaim`` when this request owns the text, a ``Duplicate`` (with the
    recorded outcome) when the same text was claimed inside the window, or ``None`` when a
    concurrent request won the compare-and-swap (treated as ``rate_limited``).
    """
    key = dedupe_key(text_hash)
    body = _encode(at=_stamp(now), outcome=OUTCOME_PENDING)
    etag = await store.create(key, body, timeout=deadline.budget(STORAGE_BUDGET))
    if etag is not None:
        return DedupeClaim(key, etag)
    existing = await store.get(key, timeout=deadline.budget(STORAGE_BUDGET))
    if existing is None:
        # Deleted between the two calls (lifecycle rule): try the create once more.
        etag = await store.create(key, body, timeout=deadline.budget(STORAGE_BUDGET))
        return DedupeClaim(key, etag) if etag is not None else None
    at, outcome = _read_at(existing.body)
    if at is not None and now - at < window:
        return Duplicate(outcome)
    # Older than the window (or unreadable): take it over with compare-and-swap.
    etag = await store.replace(key, body, existing.etag, timeout=deadline.budget(STORAGE_BUDGET))
    return DedupeClaim(key, etag) if etag is not None else None


async def record_outcome(
    store: StateStore, claim: DedupeClaim, now: datetime, outcome: str, deadline: Deadline
) -> bool:
    """Best-effort write-back of the final outcome. On any failure the blob stays ``pending``."""
    try:
        etag = await store.replace(
            claim.key,
            _encode(at=_stamp(now), outcome=outcome),
            claim.etag,
            timeout=max(deadline.budget(STORAGE_BUDGET), 0.05),
        )
    except Exception:  # noqa: BLE001 - best effort by design (plan P2)
        return False
    return etag is not None


async def claim_cooldown(
    store: StateStore, now: datetime, min_interval: timedelta, deadline: Deadline
) -> bool:
    """Stamp the cooldown blob at claim time. False if inside the interval or on a lost race."""
    body = _encode(at=_stamp(now))
    existing = await store.get(COOLDOWN_KEY, timeout=deadline.budget(STORAGE_BUDGET))
    if existing is None:
        etag = await store.create(COOLDOWN_KEY, body, timeout=deadline.budget(STORAGE_BUDGET))
        return etag is not None
    at, _ = _read_at(existing.body)
    if at is not None and now - at < min_interval:
        return False
    etag = await store.replace(
        COOLDOWN_KEY, body, existing.etag, timeout=deadline.budget(STORAGE_BUDGET)
    )
    return etag is not None


async def claim_hourly_slot(
    store: StateStore, now: datetime, max_per_hour: int, deadline: Deadline
) -> bool:
    """Claim the first free slot ``rate/<UTC yyyymmddHH>/<n>``, n = 1..max. False if none is free."""
    body = _encode(at=_stamp(now))
    for n in range(1, max_per_hour + 1):
        etag = await store.create(slot_key(now, n), body, timeout=deadline.budget(STORAGE_BUDGET))
        if etag is not None:
            return True
    return False
