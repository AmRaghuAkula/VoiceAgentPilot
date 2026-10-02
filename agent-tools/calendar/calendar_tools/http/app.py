"""The framework-free HTTP dispatcher (plan P4, UC02b).

`CalendarService.handle(Request) -> Response` owns routing, 404/405/413,
authentication, the per-binding authorization, JSON parsing and the request's
one log line. `function_app.py` (UC09) is a thin adapter around it.

The rules run in this order (plan UC02b, "Dispatcher rules"):
1. `GET /api/health` -> 200 `{"status":"ok"}`, no auth.
2. The path must be `/api/v1/bindings/{segment}/{op}` with `op` a registered
   contract operation; otherwise 404 with an empty body. A segment that is not
   a valid binding ID is not a 404: it is an unknown binding (rule 6).
3. POST only (GET only for health); otherwise 405 with `Allow`.
4. A body over 8192 bytes (by `Content-Length` or actual length) -> 413.
5. A missing or invalid token -> 401 + `WWW-Authenticate: Bearer`.
6. An unknown, disabled or not-allowlisted binding -> the same 403 bytes.
7. A body that is not a JSON object -> 200 `invalid_request`, fields `body`.
8. Unknown top-level fields are dropped; their names (never values) are logged
   at DEBUG.
9. Every operation response carries `request_id`, and every request, refusals
   included, logs exactly one line with its own diagnostic and reason.

No fault escapes as a 500: after authorization an unexpected error answers 200
with the operation's fallback status (`booking_unconfirmed` where the operation
may have written, else `calendar_unavailable`), logged `unclassified`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

import yaml

from calendar_tools import obs
from calendar_tools.config import Config
from calendar_tools.core.bindings import Binding, is_valid_binding_id
from calendar_tools.core.clock import Clock, NonceSource
from calendar_tools.core.deadline import Deadline, DeadlineExceeded
from calendar_tools.core.ports import ProviderError
from calendar_tools.http.auth import Authenticator, Unauthorized
from calendar_tools.http.responses import (
    FORBIDDEN,
    HEALTH_OK,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    UNAUTHORIZED,
    Response,
    encode,
    json_response,
    method_not_allowed,
)

__all__ = [
    "MAX_BODY_BYTES",
    "CalendarService",
    "ContractOperation",
    "Operation",
    "Request",
    "RequestContext",
    "Response",
    "load_contract_operations",
]

MAX_BODY_BYTES = 8192
HEALTH_PATH = "/api/health"
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "openapi" / "calendar-tools.openapi.yaml"
REQUEST_ID_BYTES = 16

logger = logging.getLogger("calendar_tools.http")

_OP_PATH = re.compile(r"/api/v1/bindings/([^/]+)/([^/]+)")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")
# A field name is logged only when it looks like a field name (D-006: a key
# with a phone-like digit run is data).
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PHONE_LIKE_RUN = re.compile(r"\d{7,}")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: Mapping[str, str]  # looked up case-insensitively (keys lowercased here)
    body: bytes

    def __post_init__(self) -> None:
        lowered = {str(k).lower(): v for k, v in self.headers.items()}
        object.__setattr__(self, "headers", MappingProxyType(lowered))


@dataclass
class RequestContext:
    """What an operation gets besides the binding and body. The operation sets
    `diagnostic` and `reason` (from the mapped exception's own fields, plan
    section 1 "Reason codes"), `provider_ms` and `replayed`; the dispatcher's
    one log line reads them."""

    request_id: str
    principal: str
    deadline: Deadline
    binding_id: str
    diagnostic: str | None = None
    reason: str | None = None
    provider_ms: int | None = None
    replayed: bool = False


Operation = Callable[[Binding, dict[str, Any], RequestContext], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class ContractOperation:
    operation_id: str
    path_segment: str
    request_fields: frozenset[str]
    statuses: frozenset[str]
    body_required: bool


def _resolve(doc: Mapping[str, Any], node: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = node.get("$ref")
    if ref is None:
        return node
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError("only local $refs are supported")
    target: Any = doc
    for part in ref[2:].split("/"):
        target = target[part]
    return target


@lru_cache(maxsize=4)
def load_contract_operations(path: Path = CONTRACT_PATH) -> Mapping[str, ContractOperation]:
    """The operations of the contract document (the single source, spec 4.3),
    keyed by path segment: request field names and response status enum."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    operations: dict[str, ContractOperation] = {}
    for route, item in doc["paths"].items():
        post = item["post"]
        request_body = post["requestBody"]
        request_schema = _resolve(doc, request_body["content"]["application/json"]["schema"])
        response_schema = _resolve(doc, post["responses"]["200"]["content"]["application/json"]["schema"])
        segment = route.strip("/")
        operations[segment] = ContractOperation(
            operation_id=post["operationId"],
            path_segment=segment,
            request_fields=frozenset(request_schema.get("properties", {})),
            statuses=frozenset(response_schema["properties"]["status"]["enum"]),
            body_required=request_body.get("required", False) is True,
        )
    return MappingProxyType(operations)


class _Route(NamedTuple):
    operation_id: str
    request_fields: frozenset[str]
    statuses: frozenset[str]
    fallback_status: str
    body_required: bool
    handler: Operation


@dataclass
class _Record:
    """The request's one log line, filled in as the request is handled."""

    request_id: str
    operation: str = "unknown"
    binding_id: str | None = None
    status: str | int = 0
    diagnostic: str | None = None
    reason: str | None = None
    principal: str | None = None
    provider_ms: int | None = None
    replayed: bool = False
    route: _Route | None = None  # set once the caller is authorized


class _BadResult(Exception):
    """An operation returned something that is not a contract response."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_: str) -> Any:
    raise ValueError("NaN and infinities are not JSON")


def _parse_body(body: bytes, required: bool) -> dict[str, Any] | None:
    """The body as a JSON object; `{}` for an empty body when the contract
    marks the request body optional; None when it is not a JSON object."""
    if not body.strip():
        return None if required else {}
    try:
        data = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _loggable_field_name(name: str) -> bool:
    return bool(_FIELD_NAME.fullmatch(name)) and not _PHONE_LIKE_RUN.search(name)


class CalendarService:
    def __init__(
        self,
        config: Config,
        bindings: Mapping[str, Binding],
        authenticator: Authenticator,
        operations: Mapping[str, Operation],
        clock: Clock,
        nonce: NonceSource,
        contract: Mapping[str, ContractOperation] | None = None,
    ) -> None:
        self._config = config
        self._bindings = bindings
        self._authenticator = authenticator
        self._clock = clock
        self._nonce = nonce
        contract = load_contract_operations() if contract is None else contract
        by_id = {op.operation_id: op for op in contract.values()}
        unknown = set(operations) - set(by_id)
        if unknown:
            raise ValueError(f"operations not in the contract: {sorted(unknown)}")
        self._routes: dict[str, _Route] = {}
        for operation_id, handler in operations.items():
            op = by_id[operation_id]
            if "calendar_unavailable" not in op.statuses:
                raise ValueError(f"{operation_id} has no calendar_unavailable status")
            # An operation that may write answers "unknown" rather than "nothing
            # happened" when it fails unexpectedly.
            fallback = "booking_unconfirmed" if "booking_unconfirmed" in op.statuses else "calendar_unavailable"
            self._routes[op.path_segment] = _Route(
                operation_id, op.request_fields, op.statuses, fallback, op.body_required, handler
            )
        # GUID principals compare case-insensitively.
        self._allowed: dict[str, frozenset[str]] = {
            binding_id: frozenset(p.lower() for p in binding.allowed_principals)
            for binding_id, binding in bindings.items()
        }

    async def handle(self, request: Request) -> Response:
        start = self._clock.now()
        record = _Record(request_id=self._nonce.new_hex(REQUEST_ID_BYTES))
        try:
            response = await self._dispatch(request, record)
        except Exception as exc:  # noqa: BLE001 - no fault may escape as a 500
            response = self._unexpected(record, exc)
        total_ms = round((self._clock.now() - start).total_seconds() * 1000)
        # Outside the try: under obs.STRICT a missed diagnostic must fail the test.
        obs.log_request(
            logger,
            request_id=record.request_id,
            binding_id=record.binding_id,
            operation=record.operation,
            status=record.status,
            diagnostic=record.diagnostic,
            reason=record.reason,
            principal=record.principal,
            provider_ms=record.provider_ms,
            total_ms=total_ms,
            replayed=record.replayed,
        )
        return response

    def _refuse(self, record: _Record, status: int, diagnostic: str, response: Response,
                reason: str | None = None) -> Response:
        record.status = status
        record.diagnostic = diagnostic
        record.reason = reason
        return response

    def _too_large(self, request: Request) -> bool:
        if len(request.body) > MAX_BODY_BYTES:
            return True
        declared = request.headers.get("content-length", "").strip()
        return bool(_CONTENT_LENGTH.fullmatch(declared)) and int(declared) > MAX_BODY_BYTES

    async def _dispatch(self, request: Request, record: _Record) -> Response:
        # Rule 1: health.
        if request.path == HEALTH_PATH:
            record.operation = "health"
            if request.method != "GET":
                return self._refuse(record, 405, "method_not_allowed", method_not_allowed("GET"))
            record.status, record.diagnostic = "ok", "ok"
            return HEALTH_OK

        # Rule 2: path.
        match = _OP_PATH.fullmatch(request.path)
        route = self._routes.get(match.group(2)) if match else None
        segment = match.group(1) if match else None
        # Only a configured binding's ID is logged (cso r1): an unknown one,
        # even if well formed, is caller text, and the reason says it was unknown.
        if segment is not None and is_valid_binding_id(segment) and segment in self._bindings:
            record.binding_id = segment
        if route is None or segment is None:
            return self._refuse(record, 404, "not_found", NOT_FOUND)
        record.operation = route.operation_id

        # Rule 3: method. Rule 4: size.
        if request.method != "POST":
            return self._refuse(record, 405, "method_not_allowed", method_not_allowed("POST"))
        if self._too_large(request):
            return self._refuse(record, 413, "payload_too_large", PAYLOAD_TOO_LARGE)

        # Rule 5: authentication.
        deadline = Deadline(self._clock)
        try:
            principal = await self._authenticator.authenticate(request.headers.get("authorization"), deadline)
        except Unauthorized as err:
            record.principal = err.principal
            return self._refuse(record, 401, err.diagnostic, UNAUTHORIZED, err.reason)
        record.principal = principal.value

        # Rule 6: authorization; one response for every cause.
        binding = self._bindings.get(segment) if record.binding_id is not None else None
        if binding is None:
            return self._refuse(record, 403, "forbidden", FORBIDDEN, "binding_unknown")
        if not binding.enabled:
            return self._refuse(record, 403, "forbidden", FORBIDDEN, "binding_disabled")
        if principal.value not in self._allowed[segment]:
            return self._refuse(record, 403, "forbidden", FORBIDDEN, "principal_not_allowed")
        record.route = route

        # Rule 7: the body is a JSON object.
        body = _parse_body(request.body, route.body_required)
        if body is None:
            record.status, record.diagnostic, record.reason = "invalid_request", "invalid_request", "body"
            return json_response(
                {"status": "invalid_request", "request_id": record.request_id, "detail": {"fields": ["body"]}}
            )

        # Rule 8: unknown fields dropped.
        known = {k: v for k, v in body.items() if k in route.request_fields}
        dropped = [k for k in body if k not in route.request_fields]
        if dropped:
            names = sorted(k for k in dropped if _loggable_field_name(k))
            logger.debug(
                "unknown request fields dropped: names=%s unnamed=%d",
                ",".join(names),
                len(dropped) - len(names),
            )

        ctx = RequestContext(
            request_id=record.request_id,
            principal=principal.value,
            deadline=deadline,
            binding_id=binding.binding_id,
        )
        try:
            result = await route.handler(binding, known, ctx)
        except (ProviderError, DeadlineExceeded) as err:
            # An operation should map these itself; this keeps their diagnostic.
            record.provider_ms, record.replayed = ctx.provider_ms, ctx.replayed
            record.diagnostic = err.diagnostic
            record.reason = getattr(err, "reason", None)
            record.status = route.fallback_status
            return json_response({"status": route.fallback_status, "request_id": record.request_id})
        return self._operation_response(record, route, ctx, result)

    def _operation_response(
        self, record: _Record, route: _Route, ctx: RequestContext, result: Any
    ) -> Response:
        if not isinstance(result, Mapping) or result.get("status") not in route.statuses:
            raise _BadResult()
        status = result["status"]
        payload: dict[str, Any] = {"status": status, "request_id": record.request_id}
        payload.update((k, v) for k, v in result.items() if k not in ("status", "request_id"))
        encode(payload)  # raises for anything that is not plain JSON
        record.status = status
        record.diagnostic = ctx.diagnostic or ("ok" if obs.is_ok_outcome(status) else None)
        record.reason = ctx.reason
        record.provider_ms, record.replayed = ctx.provider_ms, ctx.replayed
        return json_response(payload)

    def _unexpected(self, record: _Record, exc: Exception) -> Response:
        # The type name only: an exception's text may hold request values.
        logger.error("unexpected error in %s: %s", record.operation, type(exc).__name__)
        record.diagnostic, record.reason = obs.UNCLASSIFIED, "unexpected_exception"
        if record.route is not None:
            record.status = record.route.fallback_status
            return json_response({"status": record.route.fallback_status, "request_id": record.request_id})
        record.status = 401  # before authorization completed: fail closed
        return UNAUTHORIZED
