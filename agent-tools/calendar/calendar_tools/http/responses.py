"""Response type and the fixed protocol responses (spec section 5.1 rule 4).

Non-2xx responses are fixed values, so the three 403 causes are byte-identical
(spec section 4.2) and no refusal carries request data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_BASE_HEADERS: Mapping[str, str] = MappingProxyType(
    {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
)


def encode(body: Mapping[str, Any]) -> bytes:
    """Compact, ASCII-only JSON. Raises `TypeError`/`ValueError` for anything
    that is not plain JSON (NaN and infinities included)."""
    return json.dumps(dict(body), separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True)
class Response:
    status: int
    body: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def body_bytes(self) -> bytes:
        return b"" if self.body is None else encode(self.body)


def _headers(json_body: bool, **extra: str) -> Mapping[str, str]:
    headers = dict(_BASE_HEADERS)
    if json_body:
        headers["Content-Type"] = "application/json"
    headers.update(extra)
    return MappingProxyType(headers)


def json_response(body: Mapping[str, Any], status: int = 200) -> Response:
    return Response(status, MappingProxyType(dict(body)), _headers(True))


HEALTH_OK = Response(200, MappingProxyType({"status": "ok"}), _headers(True))
NOT_FOUND = Response(404, None, _headers(False))
PAYLOAD_TOO_LARGE = Response(413, MappingProxyType({"error": "payload_too_large"}), _headers(True))
UNAUTHORIZED = Response(
    401, MappingProxyType({"error": "unauthorized"}), _headers(True, **{"WWW-Authenticate": "Bearer"})
)
FORBIDDEN = Response(403, MappingProxyType({"error": "forbidden"}), _headers(True))


def method_not_allowed(allow: str) -> Response:
    return Response(405, None, _headers(False, Allow=allow))
