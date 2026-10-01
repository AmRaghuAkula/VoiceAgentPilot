"""``notify()``: the framework-free core (spec section 5 steps 2 to 7, plan P1, P2 and P6)."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

from sms_notify.core.config import (
    RECIPIENTS_SECRET_NAME,
    TWILIO_SECRET_NAME,
    Settings,
    parse_recipients,
)
from sms_notify.core.deadline import KEY_VAULT_BUDGET, Deadline, current_deadline
from sms_notify.core.errors import (
    ConfigError,
    NotifierAuthError,
    NotifierConfigError,
    NotifierRejected,
    NotifierUnavailable,
    SecretStoreUnavailable,
    StateStoreUnavailable,
)
from sms_notify.core.limits import (
    OUTCOME_SENT,
    DedupeClaim,
    Duplicate,
    claim_cooldown,
    claim_dedupe,
    claim_hourly_slot,
    record_outcome,
)
from sms_notify.core.messages import MessageRejected, PrefixInvalid, build_body, segment_info
from sms_notify.core.phone import mask
from sms_notify.ports import Clock, Notifier, NotifierConfig, OutboundMessage, SecretSource, StateStore

SENT = "sent"
ALREADY_SENT = "already_sent"
INVALID_REQUEST = "invalid_request"
CONTENT_REJECTED = "content_rejected"
RATE_LIMITED = "rate_limited"
FORBIDDEN = "forbidden"
SEND_FAILED = "send_failed"
SEND_UNCONFIRMED = "send_unconfirmed"
UNAVAILABLE = "unavailable"

OK_CODES = frozenset({SENT, ALREADY_SENT})
ALL_CODES = frozenset(
    {
        SENT,
        ALREADY_SENT,
        INVALID_REQUEST,
        CONTENT_REJECTED,
        RATE_LIMITED,
        FORBIDDEN,
        SEND_FAILED,
        SEND_UNCONFIRMED,
        UNAVAILABLE,
    }
)

# Worst-code order for per-recipient failures (spec section 4, plan P6).
_SEVERITY = {SEND_UNCONFIRMED: 3, UNAVAILABLE: 2, SEND_FAILED: 1}

# Backstop over the adapter's own timeouts: never wait past the deadline by more than this.
SEND_GRACE_SECONDS = 0.25


@dataclass(frozen=True)
class CoreDeps:
    clock: Clock
    secrets: SecretSource
    state: StateStore
    notifier: Notifier


@dataclass
class RequestRecord:
    """What the single log line reports (spec section 8). Never the body or a full number."""

    code: str = ""
    chars: int | None = None
    encoding: str | None = None
    segments: int | None = None
    recipients: list[dict[str, object]] = field(default_factory=list)


def worst_code(codes: list[str]) -> str:
    """``sent`` only if every recipient was sent; otherwise the worst failure code."""
    failures = [c for c in codes if c != SENT]
    if not codes:
        return SEND_FAILED
    if not failures:
        return SENT
    return max(failures, key=lambda c: _SEVERITY.get(c, _SEVERITY[SEND_UNCONFIRMED]))


async def notify(
    raw_message: str,
    *,
    settings: Settings,
    deps: CoreDeps,
    deadline: Deadline,
    record: RequestRecord,
) -> str:
    token = current_deadline.set(deadline)
    try:
        code = await _notify(raw_message, settings, deps, deadline, record)
    finally:
        current_deadline.reset(token)
    record.code = code
    return code


async def _notify(
    raw_message: str,
    settings: Settings,
    deps: CoreDeps,
    deadline: Deadline,
    record: RequestRecord,
) -> str:
    # Step 2: normalize, validate the prefix, check the limits, reject links.
    try:
        body = build_body(
            raw_message,
            prefix=settings.prefix,
            max_chars=settings.max_chars,
            max_lines=settings.max_lines,
        )
    except MessageRejected as rejected:
        return rejected.code
    except PrefixInvalid:
        return UNAVAILABLE
    info = segment_info(body)
    record.chars, record.encoding, record.segments = len(body), info.encoding, info.segments
    normalized_text = body[len(settings.prefix) :]
    text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

    # Step 3: config and secrets (fail closed before any claim is made).
    try:
        recipients_raw = await deps.secrets.get_secret(
            RECIPIENTS_SECRET_NAME, timeout=deadline.budget(KEY_VAULT_BUDGET)
        )
        recipients = parse_recipients(recipients_raw, settings.allowed_countries)
        await deps.secrets.get_secret(TWILIO_SECRET_NAME, timeout=deadline.budget(KEY_VAULT_BUDGET))
        configs = [
            NotifierConfig(
                notifier=deps.notifier.name,
                channel_config={"from_number": settings.from_number},
                credential_secret_name=TWILIO_SECRET_NAME,
                recipients=(recipient,),
            )
            for recipient in recipients
        ]
        for cfg in configs:
            deps.notifier.validate_config(cfg)
            if len(body) > deps.notifier.capabilities(cfg).max_body_chars:
                raise NotifierConfigError("body limit exceeds the channel's capability")
    except (SecretStoreUnavailable, ConfigError, NotifierConfigError):
        return UNAVAILABLE

    # Steps 4 to 6: dedupe, cooldown, hourly cap.
    now = deps.clock.now()
    try:
        claim = await claim_dedupe(
            deps.state, text_hash, now, timedelta(minutes=settings.dedupe_minutes), deadline
        )
    except StateStoreUnavailable:
        return UNAVAILABLE
    if claim is None:
        return RATE_LIMITED
    if isinstance(claim, Duplicate):
        return ALREADY_SENT if claim.outcome == OUTCOME_SENT else RATE_LIMITED

    code = await _claim_and_send(
        deps, settings, deadline, record, configs, body, text_hash, now, claim
    )
    await record_outcome(deps.state, claim, deps.clock.now(), code, deadline)
    return code


async def _claim_and_send(
    deps: CoreDeps,
    settings: Settings,
    deadline: Deadline,
    record: RequestRecord,
    configs: list[NotifierConfig],
    body: str,
    text_hash: str,
    now,
    claim: DedupeClaim,
) -> str:
    try:
        if not await claim_cooldown(
            deps.state, now, timedelta(seconds=settings.min_interval_seconds), deadline
        ):
            return RATE_LIMITED
        if not await claim_hourly_slot(deps.state, now, settings.max_per_hour, deadline):
            return RATE_LIMITED
    except StateStoreUnavailable:
        return UNAVAILABLE

    # Step 7: one send per recipient, one after another, under the deadline (K5, P6).
    message = OutboundMessage(subject=None, text_body=body, idempotency_key=text_hash)
    codes = []
    for cfg in configs:
        code, provider_id, provider_error = await _send_one(deps.notifier, cfg, message, deadline)
        codes.append(code)
        record.recipients.append(
            {
                "to": mask(cfg.recipients[0]),
                "outcome": code,
                "provider_message_id": provider_id,
                "provider_error": provider_error,
            }
        )
    return worst_code(codes)


async def _send_one(
    notifier: Notifier, cfg: NotifierConfig, message: OutboundMessage, deadline: Deadline
) -> tuple[str, str | None, int | None]:
    if deadline.expired:
        return SEND_FAILED, None, None  # nothing went out
    try:
        async with asyncio.timeout(deadline.remaining() + SEND_GRACE_SECONDS):
            result = await notifier.send(cfg, message)
    except NotifierUnavailable as err:
        return (SEND_UNCONFIRMED if err.maybe_sent else SEND_FAILED), None, None
    except (NotifierAuthError, NotifierRejected) as err:
        return SEND_FAILED, None, err.error_code
    except (NotifierConfigError, SecretStoreUnavailable):
        return UNAVAILABLE, None, None
    except Exception:  # noqa: BLE001 - includes the backstop timeout: it may have been sent
        return SEND_UNCONFIRMED, None, None
    return SENT, result.provider_message_id, None
