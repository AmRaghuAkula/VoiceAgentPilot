"""Observability (spec section 10, rev 3.2): phone masking and the one
structured log line per request. Nothing else logs contact data.

Never logged: contact name, email, notes, event title or description,
`calendar_id`, tokens, or the slot token's value. Phone numbers appear only
masked. `principal` is the configured claim's value (an `oid`), never a token.
"""

from __future__ import annotations

import json
import logging
import re

from calendar_tools.core.ports import INVALID_REASON, is_reason_code

# Set True by tests/conftest.py for the whole suite. In production it stays
# False: the guard then never raises, because it runs after the operation may
# already have created a booking.
STRICT = False

# Spec rev 3.2 section 10: the OK outcomes. Every other status (including every
# non-2xx HTTP code) is non-OK and must carry a diagnostic other than "ok".
OK_STATUSES: frozenset[str] = frozenset({"ok", "available", "no_availability", "booked"})
UNCLASSIFIED = "unclassified"

_NON_DIGIT = re.compile(r"\D")


def mask_phone(value: object) -> str:
    """Keep the last 4 digits only (`***0123`); the D-021 rule, reimplemented
    locally (no import from the bridge)."""
    if value is None:
        return "***"
    digits = _NON_DIGIT.sub("", str(value))
    if len(digits) < 4:
        return "***"
    return "***" + digits[-4:]


def is_ok_outcome(status: str | int) -> bool:
    return isinstance(status, str) and status in OK_STATUSES


def log_request(
    logger: logging.Logger,
    *,
    request_id: str,
    binding_id: str | None,
    operation: str,
    status: str | int,
    diagnostic: str | None,
    reason: str | None = None,
    principal: str | None = None,
    provider_ms: int | None,
    total_ms: int | None,
    replayed: bool,
) -> None:
    """Emit the request's one INFO line as JSON.

    `status` is the response `status` for a 200, or the HTTP code for a non-2xx.
    A non-OK outcome whose `diagnostic` is empty or `ok` is a programming error:
    under `STRICT` it raises `ValueError`; otherwise the line is logged with
    diagnostic `unclassified` (alerted in UC09) plus one WARNING. Under `STRICT`
    a non-OK outcome passed in as `unclassified` also raises.
    """
    if reason is not None and not is_reason_code(reason):
        # A reason is a code, never a value, token, contact field or vendor text.
        if STRICT:
            raise ValueError(f"reason of {operation} is not a reason code")
        reason = INVALID_REASON
    elif STRICT and reason == INVALID_REASON:
        # An exception replaced a non-code reason: a programming error.
        raise ValueError(f"reason of {operation} was replaced as invalid_reason")
    non_ok = not is_ok_outcome(status)
    if STRICT and non_ok and diagnostic == UNCLASSIFIED:
        # e.g. a bare `ProviderError` copied into the line: a missed path, so the
        # suite fails on it rather than passing with an alerted `unclassified`.
        raise ValueError(f"non-OK outcome {status!r} of {operation} logged as unclassified")
    if non_ok and (not diagnostic or diagnostic == "ok"):
        if STRICT:
            raise ValueError(f"non-OK outcome {status!r} of {operation} logged without a diagnostic")
        logger.warning(
            "unclassified outcome: operation=%s status=%s request_id=%s",
            operation,
            status,
            request_id,
        )
        diagnostic = UNCLASSIFIED

    line = {
        "request_id": request_id,
        "binding_id": binding_id,
        "operation": operation,
        "status": status,
        "diagnostic": diagnostic,
        "reason": reason,
        "principal": principal,
        "provider_ms": provider_ms,
        "total_ms": total_ms,
        "replayed": replayed,
    }
    logger.info(json.dumps(line, separators=(",", ":"), ensure_ascii=True))
