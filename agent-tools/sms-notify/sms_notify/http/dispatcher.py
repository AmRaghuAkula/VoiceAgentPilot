"""Framework-free HTTP dispatcher (plan P3): JWT auth and the always-200 envelope (spec K4).

A missing or invalid token gets 401 with an empty body. Every other outcome is HTTP 200
with ``{"ok", "code", "retry": false}``. One log line per request (spec section 8): never
the body, a full number or a secret.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sms_notify.core.config import Settings
from sms_notify.core.deadline import Deadline
from sms_notify.core.errors import ConfigError
from sms_notify.core.service import (
    FORBIDDEN,
    INVALID_REQUEST,
    OK_CODES,
    UNAVAILABLE,
    CoreDeps,
    RequestRecord,
    notify,
)
from sms_notify.http.auth import AuthResult, JwksCache, authenticate
from sms_notify.ports import Clock

ROUTE = "/api/v1/follow-up-sms"
MAX_BODY_BYTES = 8_192
MAX_RAW_MESSAGE_CHARS = 4 * 480  # a loose pre-check; the real limit applies after normalization

logger = logging.getLogger("sms_notify")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: Mapping[str, str]  # keys lower-cased by the caller
    body: bytes


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    jwks: JwksCache
    deps: CoreDeps


def _envelope(code: str) -> Response:
    payload = {"ok": code in OK_CODES, "code": code, "retry": False}
    return Response(
        200,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


_UNAUTHORIZED = Response(401, b"", {"WWW-Authenticate": "Bearer"})


def _parse_message(body: bytes) -> str | None:
    """The ``message`` string, or None for anything the schema doesn't allow."""
    if len(body) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {"message"}:
        return None  # unknown fields, including any "to" or "recipient", are rejected
    message = data["message"]
    if not isinstance(message, str) or not message or len(message) > MAX_RAW_MESSAGE_CHARS:
        return None
    return message


class Dispatcher:
    """``runtime_factory`` builds the settings and dependencies; it raises ``ConfigError``
    when the configuration is missing or invalid (the request then gets 200 ``unavailable``)."""

    def __init__(self, runtime_factory: Callable[[], Runtime], clock: Clock) -> None:
        self._runtime_factory = runtime_factory
        self._clock = clock

    async def handle(self, request: Request) -> Response:
        start = self._clock.monotonic()
        deadline = Deadline(self._clock)
        record = RequestRecord()
        response = _envelope(UNAVAILABLE)
        try:
            response = await self._handle(request, deadline, record)
        except Exception:  # noqa: BLE001 - K4: no unexpected fault may escape as a 500
            logger.error("sms_notify.unexpected_error")
            record.code = UNAVAILABLE
            response = _envelope(UNAVAILABLE)
        finally:
            _log(record, response, (self._clock.monotonic() - start) * 1000.0)
        return response

    async def _handle(self, request: Request, deadline: Deadline, record: RequestRecord) -> Response:
        if request.path != ROUTE:
            record.code = "not_found"
            return Response(404)
        if request.method.upper() != "POST":
            record.code = "method_not_allowed"
            return Response(405, b"", {"Allow": "POST"})

        try:
            runtime = self._runtime_factory()
        except ConfigError:
            record.code = UNAVAILABLE
            return _envelope(UNAVAILABLE)

        auth = await authenticate(dict(request.headers), runtime.settings, runtime.jwks, deadline)
        if auth.result is AuthResult.UNAUTHORIZED:
            record.code = "unauthorized"
            return _UNAUTHORIZED
        if auth.result is AuthResult.UNAVAILABLE:
            record.code = UNAVAILABLE
            return _envelope(UNAVAILABLE)
        if auth.result is AuthResult.FORBIDDEN:
            record.code = FORBIDDEN
            return _envelope(FORBIDDEN)

        message = _parse_message(request.body)
        if message is None:
            record.code = INVALID_REQUEST
            return _envelope(INVALID_REQUEST)
        try:
            code = await notify(
                message, settings=runtime.settings, deps=runtime.deps, deadline=deadline, record=record
            )
        except Exception:  # noqa: BLE001 - an unexpected fault still answers 200, fails closed
            logger.error("sms_notify.unexpected_error")
            code = UNAVAILABLE
            record.code = code
        return _envelope(code)


def _log(record: RequestRecord, response: Response, latency_ms: float) -> None:
    line = {
        "event": "sms_notify.request",
        "request_id": uuid.uuid4().hex,
        "status": response.status,
        "code": record.code,
        "recipients": record.recipients,
        "chars": record.chars,
        "encoding": record.encoding,
        "segments": record.segments,
        "latency_ms": round(latency_ms, 1),
    }
    logger.info(json.dumps(line, separators=(",", ":")))
