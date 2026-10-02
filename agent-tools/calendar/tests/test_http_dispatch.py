"""The framework-free HTTP dispatcher (plan UC02b, P4; spec sections 4.2, 5.1, 9.3, 10).

One test (or parametrized group) per dispatcher rule, one for the order of the
rules, and the rev 1.6 reason-code tests: every refusal logs its own
`diagnostic` and `reason`, read back from captured logs.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from calendar_tools import obs
from calendar_tools.core.bindings import Binding, load_bindings
from calendar_tools.core.ports import ProviderUnavailable
from calendar_tools.http.app import (
    MAX_BODY_BYTES,
    CalendarService,
    Request,
    RequestContext,
    load_contract_operations,
)
from calendar_tools.http.auth import Authenticator, JwksCache
from tests.fakes import jwt_tokens as jt
from tests.fakes.clock import FakeClock, FakeNonceSource

ROOT = Path(__file__).resolve().parents[1]
LOGGER_NAME = "calendar_tools.http"
SENTINEL = "SENTINEL-VALUE-9"
CHECK = "/api/v1/bindings/test-alpha/check-availability"
BOOK = "/api/v1/bindings/test-alpha/book-appointment"


def _bindings_doc() -> str:
    doc = json.loads((ROOT / "bindings.sample.json").read_text(encoding="utf-8"))
    template = next(iter(doc["bindings"].values()))
    template["provider"] = "fake"

    def variant(**changes: Any) -> dict[str, Any]:
        b = json.loads(json.dumps(template))
        b.update(changes)
        return b

    return json.dumps(
        {
            "bindings": {
                "test-alpha": variant(allowed_principals=[jt.PRINCIPAL_A]),
                "test-beta": variant(allowed_principals=[jt.PRINCIPAL_B]),
                "test-off": variant(enabled=False, allowed_principals=[jt.PRINCIPAL_A]),
                "test-upper": variant(allowed_principals=[jt.PRINCIPAL_A.upper()]),
            }
        }
    )


class StubOperation:
    """Returns `result` (default `{"status": "available"}`), or raises `raises`,
    after optionally setting `diagnostic`/`reason` on the context."""

    def __init__(self, result: Any = None) -> None:
        self.result = {"status": "available"} if result is None else result
        self.raises: BaseException | None = None
        self.diagnostic: str | None = None
        self.reason: str | None = None
        self.calls: list[tuple[Binding, dict[str, Any], RequestContext]] = []

    async def __call__(self, binding: Binding, body: dict[str, Any], ctx: RequestContext) -> Any:
        self.calls.append((binding, body, ctx))
        ctx.diagnostic = self.diagnostic
        ctx.reason = self.reason
        if self.raises is not None:
            raise self.raises
        return self.result


class Harness:
    def __init__(self, register_book: bool = True) -> None:
        self.clock = FakeClock()
        self.jwks = jt.JwksStub()
        config = jt.make_config()
        authenticator = Authenticator(config, JwksCache(jt.TENANT_ID, jt.jwks_client(self.jwks), self.clock), self.clock)
        self.check = StubOperation()
        self.book = StubOperation({"status": "booked"})
        operations: dict[str, Any] = {"check_availability": self.check}
        if register_book:
            operations["book_appointment"] = self.book
        self.service = CalendarService(
            config=config,
            bindings=load_bindings(_bindings_doc(), frozenset({"fake"})),
            authenticator=authenticator,
            operations=operations,
            clock=self.clock,
            nonce=FakeNonceSource(),
        )

    def token(self, **claims: Any) -> str:
        return jt.make_token(self.clock.now().timestamp(), **claims)

    async def call(
        self,
        path: str = CHECK,
        body: bytes = b"{}",
        *,
        method: str = "POST",
        token: str | None = "default",
        headers: dict[str, str] | None = None,
    ):
        h: dict[str, str] = {}
        if token == "default":
            token = self.token()
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        return await self.service.handle(Request(method=method, path=path, headers=h, body=body))


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def _lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.INFO
    ]


def _one_line(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    lines = _lines(caplog)
    assert len(lines) == 1
    return lines[0]


def _json(resp) -> Any:
    return json.loads(resp.body_bytes())


# --- rule 1: health -------------------------------------------------------------


async def test_health(harness, logs) -> None:
    resp = await harness.call("/api/health", b"", method="GET", token=None)
    assert resp.status == 200
    assert resp.body_bytes() == b'{"status":"ok"}'
    assert harness.jwks.calls == 0
    line = _one_line(logs)
    assert (line["operation"], line["status"], line["diagnostic"], line["binding_id"]) == ("health", "ok", "ok", None)


@pytest.mark.parametrize("method", ["POST", "PUT", "HEAD", "get"])
async def test_health_other_methods(harness, logs, method) -> None:
    resp = await harness.call("/api/health", b"", method=method, token=None)
    assert resp.status == 405
    assert resp.headers["Allow"] == "GET"
    assert resp.body_bytes() == b""
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"]) == (405, "method_not_allowed")


# --- rule 2: paths ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "",
        "/api",
        "/api/health/",
        "/api/v1/bindings/test-alpha",
        "/api/v1/bindings/test-alpha/",
        "/api/v1/bindings/test-alpha/unknown-op",
        "/api/v1/bindings//check-availability",
        "/api/v1/bindings/test-alpha/check-availability/",
        "/api/v1/bindings/a/b/check-availability",
        "/API/v1/bindings/test-alpha/check-availability",
        "/api/v2/bindings/test-alpha/check-availability",
        "/api/v1/bindings/test-alpha/check-availability?x=1",
        "/api/v1/bindings/test-alpha/check_availability",
    ],
)
async def test_unknown_paths_404_empty_body(harness, logs, path) -> None:
    resp = await harness.call(path)
    assert resp.status == 404
    assert resp.body is None
    assert resp.body_bytes() == b""
    assert harness.jwks.calls == 0
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (404, "not_found", None)


async def test_contract_operation_not_registered_is_404(logs) -> None:
    h = Harness(register_book=False)
    resp = await h.call(BOOK)
    assert resp.status == 404
    assert _one_line(logs)["diagnostic"] == "not_found"


async def test_404_logs_binding_only_when_configured(harness, logs) -> None:
    await harness.call("/api/v1/bindings/test-alpha/unknown-op")
    await harness.call(f"/api/v1/bindings/{SENTINEL}/unknown-op")
    await harness.call("/api/v1/bindings/sam-sample-example/unknown-op")
    first, second, third = _lines(logs)
    assert first["binding_id"] == "test-alpha"
    assert second["binding_id"] is None
    assert third["binding_id"] is None
    assert SENTINEL not in logs.text
    assert "sam-sample-example" not in logs.text


async def test_unknown_well_formed_binding_not_logged(harness, logs) -> None:
    # cso r1: a caller must not be able to write ID-shaped text into the logs.
    for token in (None, "default"):
        await harness.call("/api/v1/bindings/sam-sample-example/check-availability", token=token)
    lines = _lines(logs)
    assert [(line["status"], line["reason"]) for line in lines] == [(401, "token_missing"), (403, "binding_unknown")]
    assert all(line["binding_id"] is None for line in lines)
    assert "sam-sample-example" not in logs.text


@pytest.mark.parametrize(
    "segment",
    ["UPPER", "zq", "-lead", "x" * 41, SENTINEL, "a-6135550123", "test%2Dalpha", "test-alpha.."],
)
async def test_invalid_binding_segment_is_403_not_404(harness, logs, segment) -> None:
    resp = await harness.call(f"/api/v1/bindings/{segment}/check-availability")
    assert resp.status == 403
    assert resp.body_bytes() == b'{"error":"forbidden"}'
    line = _one_line(logs)
    assert (line["diagnostic"], line["reason"], line["binding_id"]) == ("forbidden", "binding_unknown", None)
    assert segment not in logs.text
    assert harness.check.calls == []


# --- rule 3: methods ---------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS", "post"])
async def test_operation_requires_post(harness, logs, method) -> None:
    resp = await harness.call(CHECK, method=method)
    assert resp.status == 405
    assert resp.headers["Allow"] == "POST"
    assert resp.body_bytes() == b""
    assert harness.jwks.calls == 0
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["operation"]) == (405, "method_not_allowed", "check_availability")


# --- rule 4: body size --------------------------------------------------------------


async def test_body_over_limit_413(harness, logs) -> None:
    resp = await harness.call(CHECK, b" " * (MAX_BODY_BYTES + 1), token=None)
    assert resp.status == 413
    assert resp.body_bytes() == b'{"error":"payload_too_large"}'
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"]) == (413, "payload_too_large")


async def test_content_length_over_limit_413(harness, logs) -> None:
    resp = await harness.call(CHECK, b"{}", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
    assert resp.status == 413


async def test_body_at_limit_accepted(harness) -> None:
    body = b"{}" + b" " * (MAX_BODY_BYTES - 2)
    resp = await harness.call(CHECK, body)
    assert resp.status == 200
    assert _json(resp)["status"] == "available"


@pytest.mark.parametrize("value", ["abc", "-1", "1e9", ""])
async def test_unparseable_content_length_uses_actual_length(harness, value) -> None:
    resp = await harness.call(CHECK, b"{}", headers={"Content-Length": value})
    assert resp.status == 200


# --- rule 5: authentication ----------------------------------------------------------


async def test_missing_token_401(harness, logs) -> None:
    resp = await harness.call(CHECK, token=None)
    assert resp.status == 401
    assert resp.body_bytes() == b'{"error":"unauthorized"}'
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (401, "unauthorized", "token_missing")
    assert line["binding_id"] == "test-alpha"
    assert harness.check.calls == []


def _token_for_reason(h: Harness, reason: str) -> str | None:
    now = h.clock.now().timestamp()
    return {
        "token_missing": None,
        "token_malformed": "not-a-jwt",
        "alg_rejected": jt.unsigned_token(now),
        "kid_unknown": h.token(kid=jt.KID_B, key="b"),
        "signature_invalid": h.token(key="b"),
        "issuer_mismatch": h.token(iss=f"https://sts.windows.net/{jt.OTHER_TENANT_ID}/"),
        "audience_mismatch": h.token(aud="api://00000000-0000-0000-0000-0000000000c3"),
        "tenant_mismatch": h.token(tid=jt.OTHER_TENANT_ID),
        "token_expired": h.token(exp=int(now) - 61),
        "token_not_yet_valid": h.token(nbf=int(now) + 61),
        "role_missing": h.token(roles=[]),
        "principal_claim_missing": h.token(drop=("oid",)),
    }[reason]


@pytest.mark.parametrize(
    "reason",
    [
        "token_missing",
        "token_malformed",
        "alg_rejected",
        "kid_unknown",
        "signature_invalid",
        "issuer_mismatch",
        "audience_mismatch",
        "tenant_mismatch",
        "token_expired",
        "token_not_yet_valid",
        "role_missing",
        "principal_claim_missing",
    ],
)
async def test_each_401_reason_logged(harness, logs, reason) -> None:
    token = _token_for_reason(harness, reason)
    resp = await harness.call(CHECK, token=token)
    assert resp.status == 401
    assert resp.body_bytes() == b'{"error":"unauthorized"}'
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (401, "unauthorized", reason)
    if token:
        for part in token.split("."):
            if part:
                assert part not in logs.text


async def test_role_missing_logs_principal(harness, logs) -> None:
    await harness.call(CHECK, token=harness.token(roles=[]))
    assert _one_line(logs)["principal"] == jt.PRINCIPAL_A


async def test_jwks_unreachable_401(harness, logs) -> None:
    harness.jwks.mode = "down"
    resp = await harness.call(CHECK)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (401, "jwks_unreachable", None)


async def test_header_names_are_case_insensitive(harness) -> None:
    resp = await harness.call(CHECK, token=None, headers={"AUTHORIZATION": f"Bearer {harness.token()}"})
    assert resp.status == 200


# --- rule 6: identical 403 -------------------------------------------------------------


FORBIDDEN_CASES = [
    ("/api/v1/bindings/test-nope/check-availability", "binding_unknown"),
    ("/api/v1/bindings/NOT_VALID/check-availability", "binding_unknown"),
    ("/api/v1/bindings/test-off/check-availability", "binding_disabled"),
    ("/api/v1/bindings/test-beta/check-availability", "principal_not_allowed"),
]


@pytest.mark.parametrize(("path", "reason"), FORBIDDEN_CASES)
async def test_403_reasons(harness, logs, path, reason) -> None:
    resp = await harness.call(path)
    assert resp.status == 403
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (403, "forbidden", reason)
    assert line["principal"] == jt.PRINCIPAL_A
    assert harness.check.calls == []


async def test_403_bodies_and_headers_byte_identical(harness) -> None:
    responses = [await harness.call(path) for path, _ in FORBIDDEN_CASES]
    bodies = {r.body_bytes() for r in responses}
    assert bodies == {b'{"error":"forbidden"}'}
    assert len({tuple(sorted(r.headers.items())) for r in responses}) == 1
    assert {r.status for r in responses} == {403}


async def test_disabled_binding_refused_even_for_allowed_principal(harness) -> None:
    resp = await harness.call("/api/v1/bindings/test-off/check-availability")
    assert resp.status == 403


async def test_allowlist_is_case_insensitive(harness) -> None:
    resp = await harness.call("/api/v1/bindings/test-upper/check-availability")
    assert resp.status == 200


async def test_principal_x_rejected_on_binding_not_listing_it(harness) -> None:
    # G4's auth half: principal B may use test-beta and not test-alpha.
    token = harness.token(oid=jt.PRINCIPAL_B)
    assert (await harness.call("/api/v1/bindings/test-beta/check-availability", token=token)).status == 200
    assert (await harness.call(CHECK, token=token)).status == 403


# --- rule 7: body not a JSON object ------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        b'"a string"',
        b"42",
        b"null",
        b"\xff\xfe",
        b'{"from_date": "2026-10-05", "from_date": "2026-10-06"}',
        b'{"duration_minutes": NaN}',
        b'{"duration_minutes": Infinity}',
        b"[" * 4000 + b"]" * 4000,
        b'{"a": 1',
    ],
)
async def test_body_not_a_json_object(harness, logs, body) -> None:
    resp = await harness.call(CHECK, body)
    assert resp.status == 200
    payload = _json(resp)
    assert payload == {
        "status": "invalid_request",
        "request_id": payload["request_id"],
        "detail": {"fields": ["body"]},
    }
    assert re.fullmatch(r"[0-9a-f]{32}", payload["request_id"])
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == ("invalid_request", "invalid_request", "body")
    assert line["request_id"] == payload["request_id"]
    assert harness.check.calls == []


@pytest.mark.parametrize("body", [b"", b"   ", b"\r\n"])
async def test_empty_body_is_an_empty_object_when_optional(harness, body) -> None:
    # check_availability's request body is optional in the contract.
    resp = await harness.call(CHECK, body)
    assert resp.status == 200
    assert harness.check.calls[0][1] == {}


@pytest.mark.parametrize("body", [b"", b"   "])
async def test_empty_body_is_invalid_when_required(harness, logs, body) -> None:
    # book_appointment's request body is required in the contract (rule 7).
    resp = await harness.call(BOOK, body)
    assert resp.status == 200
    payload = _json(resp)
    assert (payload["status"], payload["detail"]) == ("invalid_request", {"fields": ["body"]})
    line = _one_line(logs)
    assert (line["diagnostic"], line["reason"]) == ("invalid_request", "body")
    assert harness.book.calls == []


# --- rule 8: unknown fields dropped ---------------------------------------------------------


async def test_unknown_fields_dropped_names_logged_at_debug(harness, logs) -> None:
    body = json.dumps(
        {"from_date": "2026-10-06", "extra_field": SENTINEL, "binding_id": "test-beta"}
    ).encode()
    resp = await harness.call(CHECK, body)
    assert resp.status == 200
    assert harness.check.calls[0][1] == {"from_date": "2026-10-06"}
    debug = [r.getMessage() for r in logs.records if r.levelno == logging.DEBUG and r.name == LOGGER_NAME]
    assert len(debug) == 1
    assert "binding_id" in debug[0] and "extra_field" in debug[0]
    assert SENTINEL not in logs.text


async def test_unknown_field_names_that_look_like_data_are_not_logged(harness, logs) -> None:
    odd = {SENTINEL: 1, "+1 613 555 0123": 2, "a b": 3, "x" * 100: 4}
    resp = await harness.call(CHECK, json.dumps(odd).encode())
    assert resp.status == 200
    assert harness.check.calls[0][1] == {}
    assert SENTINEL not in logs.text
    assert "555 0123" not in logs.text
    assert "x" * 100 not in logs.text


async def test_book_fields_come_from_the_contract(harness) -> None:
    body = {"slot_id": "s", "start": "t", "appointment_type": "phone_call", "contact": {"name": "Sam Sample"}, "notes": None, "x": 1}
    resp = await harness.call(BOOK, json.dumps(body).encode())
    assert resp.status == 200
    seen = harness.book.calls[0][1]
    assert set(seen) == {"slot_id", "start", "appointment_type", "contact", "notes"}


# --- rule 9: request_id and the one log line ------------------------------------------------


async def test_ok_response_carries_request_id_and_context(harness, logs) -> None:
    resp = await harness.call(CHECK)
    payload = _json(resp)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/json"
    assert resp.headers["Cache-Control"] == "no-store"
    assert payload["status"] == "available"
    assert re.fullmatch(r"[0-9a-f]{32}", payload["request_id"])
    binding, _body, ctx = harness.check.calls[0]
    assert binding.binding_id == "test-alpha"
    assert ctx.request_id == payload["request_id"]
    assert ctx.principal == jt.PRINCIPAL_A
    assert ctx.deadline.remaining() > 0
    line = _one_line(logs)
    assert line == {
        "request_id": payload["request_id"],
        "binding_id": "test-alpha",
        "operation": "check_availability",
        "status": "available",
        "diagnostic": "ok",
        "reason": None,
        "principal": jt.PRINCIPAL_A,
        "provider_ms": None,
        "total_ms": 0,
        "replayed": False,
    }


async def test_request_ids_differ(harness) -> None:
    ids = {_json(await harness.call(CHECK))["request_id"] for _ in range(3)}
    assert len(ids) == 3


async def test_operation_cannot_set_request_id(harness) -> None:
    harness.check.result = {"status": "available", "request_id": "forged"}
    payload = _json(await harness.call(CHECK))
    assert payload["request_id"] != "forged"


async def test_operation_diagnostic_and_reason_copied(harness, logs) -> None:
    harness.check.result = {"status": "calendar_unavailable"}
    harness.check.diagnostic = "secret_invalid"
    harness.check.reason = "slot-token-key.empty"
    await harness.call(CHECK)
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (
        "calendar_unavailable",
        "secret_invalid",
        "slot-token-key.empty",
    )


async def test_informational_diagnostic_on_ok_outcome(harness, logs) -> None:
    harness.book.result = {"status": "booked"}
    harness.book.diagnostic = "claim_finalize_failed"
    await harness.call(BOOK)
    assert _one_line(logs)["diagnostic"] == "claim_finalize_failed"


async def test_provider_ms_and_replayed_copied(harness, logs) -> None:
    async def op(binding, body, ctx):
        ctx.provider_ms = 12
        ctx.replayed = True
        return {"status": "booked", "replayed": True}

    harness.service = CalendarService(
        config=jt.make_config(),
        bindings=harness.service._bindings,
        authenticator=harness.service._authenticator,
        operations={"book_appointment": op},
        clock=harness.clock,
        nonce=FakeNonceSource(),
    )
    await harness.call(BOOK)
    line = _one_line(logs)
    assert (line["provider_ms"], line["replayed"]) == (12, True)


async def test_non_ok_without_diagnostic_fails_the_guard(harness) -> None:
    harness.check.result = {"status": "calendar_unavailable"}
    with pytest.raises(ValueError):
        await harness.call(CHECK)


async def test_total_ms_measured_with_the_clock(harness, logs) -> None:
    async def slow(binding, body, ctx):
        harness.clock.advance(1.25)
        return {"status": "available"}

    harness.service._routes["check-availability"] = harness.service._routes["check-availability"]._replace(handler=slow)
    await harness.call(CHECK)
    assert _one_line(logs)["total_ms"] == 1250


# --- the catch-all: no fault escapes as a 500 ------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [(CHECK, "calendar_unavailable"), (BOOK, "booking_unconfirmed")],
)
async def test_unexpected_exception_answers_200(harness, logs, monkeypatch, path, expected) -> None:
    monkeypatch.setattr(obs, "STRICT", False)
    op = harness.check if path == CHECK else harness.book
    op.raises = RuntimeError(SENTINEL)
    resp = await harness.call(path)
    assert resp.status == 200
    payload = _json(resp)
    assert payload["status"] == expected
    line = _one_line(logs)
    assert (line["diagnostic"], line["reason"]) == ("unclassified", "unexpected_exception")
    assert SENTINEL not in logs.text


async def test_unexpected_exception_before_authorization_is_401(harness, logs, monkeypatch) -> None:
    monkeypatch.setattr(obs, "STRICT", False)

    class Broken:
        async def authenticate(self, header, deadline):
            raise RuntimeError(SENTINEL)

    harness.service._authenticator = Broken()
    resp = await harness.call(CHECK)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.body_bytes() == b'{"error":"unauthorized"}'
    line = _one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == (401, "unclassified", "unexpected_exception")
    assert SENTINEL not in logs.text
    assert harness.check.calls == []


async def test_unexpected_exception_fails_the_suite_under_strict(harness) -> None:
    harness.check.raises = RuntimeError("boom")
    with pytest.raises(ValueError):
        await harness.call(CHECK)


async def test_escaped_core_exception_keeps_its_diagnostic(harness, logs) -> None:
    harness.check.raises = ProviderUnavailable("http_503")
    resp = await harness.call(CHECK)
    assert _json(resp)["status"] == "calendar_unavailable"
    line = _one_line(logs)
    assert (line["diagnostic"], line["reason"]) == ("provider_unavailable", "http_503")


@pytest.mark.parametrize(
    "result",
    [
        None,
        ["available"],
        {"no_status": True},
        {"status": "ok"},  # not a contract status for this operation
        {"status": "booked"},  # a status of another operation
        {"status": "available", "x": object()},  # not JSON-serializable
        {"status": "available", "x": float("nan")},
    ],
)
async def test_bad_operation_result_is_unexpected(harness, logs, monkeypatch, result) -> None:
    monkeypatch.setattr(obs, "STRICT", False)
    harness.check.result = result
    if result is None:
        harness.check.result = 0
    resp = await harness.call(CHECK)
    assert resp.status == 200
    assert _json(resp)["status"] == "calendar_unavailable"
    assert _one_line(logs)["diagnostic"] == "unclassified"


# --- order of the rules ------------------------------------------------------------------


async def test_rule_order(harness, logs) -> None:
    big = b"x" * (MAX_BODY_BYTES + 1)
    # 404 before 405
    assert (await harness.call("/api/v1/bindings/test-alpha/nope", method="GET", token=None)).status == 404
    # 405 before 413 and 401
    assert (await harness.call(CHECK, big, method="GET", token=None)).status == 405
    # 413 before 401
    assert (await harness.call(CHECK, big, token=None)).status == 413
    # 401 before 403
    assert (await harness.call("/api/v1/bindings/test-nope/check-availability", b"bad", token=None)).status == 401
    # 403 before the body is parsed
    assert (await harness.call("/api/v1/bindings/test-nope/check-availability", b"bad")).status == 403
    # body parsed only after authorization
    resp = await harness.call(CHECK, b"bad")
    assert (resp.status, _json(resp)["status"]) == (200, "invalid_request")
    assert [line["diagnostic"] for line in _lines(logs)] == [
        "not_found",
        "method_not_allowed",
        "payload_too_large",
        "unauthorized",
        "forbidden",
        "invalid_request",
    ]


# --- no sentinel ever reaches a log line ----------------------------------------------------


async def test_no_planted_value_in_any_log_line(harness, logs) -> None:
    body = json.dumps({"from_date": SENTINEL, SENTINEL: SENTINEL}).encode()
    await harness.call(CHECK, body)
    await harness.call(f"/api/v1/bindings/{SENTINEL}/check-availability", body)
    await harness.call(CHECK, body, headers={"X-Planted": SENTINEL})
    await harness.call(CHECK, token=SENTINEL)
    await harness.call(CHECK, SENTINEL.encode())
    for line in _lines(logs):
        if line["reason"] is not None:
            assert SENTINEL.lower() not in line["reason"].lower()
    assert SENTINEL not in logs.text


# --- construction ------------------------------------------------------------------------


def test_operations_must_be_in_the_contract() -> None:
    h = Harness()
    with pytest.raises(ValueError):
        CalendarService(
            config=jt.make_config(),
            bindings=h.service._bindings,
            authenticator=h.service._authenticator,
            operations={"cancel_appointment": StubOperation()},
            clock=FakeClock(),
            nonce=FakeNonceSource(),
        )


def test_contract_operations_loaded_from_the_document() -> None:
    ops = load_contract_operations()
    assert set(ops) == {"check-availability", "book-appointment"}
    check = ops["check-availability"]
    assert check.operation_id == "check_availability"
    assert check.request_fields == frozenset(
        {"from_date", "to_date", "earliest_time", "latest_time", "duration_minutes"}
    )
    assert "invalid_request" in check.statuses and "available" in check.statuses
    book = ops["book-appointment"]
    assert book.operation_id == "book_appointment"
    assert book.request_fields == frozenset({"slot_id", "start", "appointment_type", "contact", "notes"})
    assert "booking_unconfirmed" in book.statuses
    assert (check.body_required, book.body_required) == (False, True)
