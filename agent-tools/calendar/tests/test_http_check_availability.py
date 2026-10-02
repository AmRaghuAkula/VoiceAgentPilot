"""`check_availability` end to end through `CalendarService.handle` with a real
token (plan UC03): every response validates against the contract, and each
request logs exactly one line with `operation=check_availability`, its own
diagnostic and reason, and no slot token value anywhere in the logs."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import jsonschema
import pytest
import yaml

from calendar_tools.core.availability import make_check_availability
from calendar_tools.core.bindings import load_bindings
from calendar_tools.core.ports import ProviderUnavailable, SecretInvalid, SecretStoreUnavailable
from calendar_tools.http.app import CalendarService, Request
from calendar_tools.http.auth import Authenticator, JwksCache
from calendar_tools.providers.fake import FakeCalendarProvider
from tests.fakes import jwt_tokens as jt
from tests.fakes.clock import FakeClock, FakeNonceSource
from tests.fakes.keyring import FakeKeyRing
from tests.test_check_availability import binding_doc
from tests.test_openapi_doc import DOC_PATH, FORMATS, _response_schema

PATH = "/api/v1/bindings/test-alpha/check-availability"
SENTINEL = "SENTINEL-VALUE-9"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    doc = yaml.safe_load(DOC_PATH.read_text(encoding="utf-8"))
    return _response_schema(doc, "check_availability")


class Service:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.provider = FakeCalendarProvider()
        self.keyring = FakeKeyRing()
        config = jt.make_config()
        authenticator = Authenticator(
            config, JwksCache(jt.TENANT_ID, jt.jwks_client(jt.JwksStub()), self.clock), self.clock
        )
        bindings = load_bindings(json.dumps({"bindings": {"test-alpha": binding_doc()}}), frozenset({"fake"}))
        self.service = CalendarService(
            config=config,
            bindings=bindings,
            authenticator=authenticator,
            operations={"check_availability": make_check_availability(self.provider, self.keyring, self.clock)},
            clock=self.clock,
            nonce=FakeNonceSource(),
        )

    async def post(self, body: bytes = b"{}") -> tuple[int, dict[str, Any] | None]:
        token = jt.make_token(self.clock.now().timestamp())
        resp = await self.service.handle(
            Request(method="POST", path=PATH, headers={"Authorization": f"Bearer {token}"}, body=body)
        )
        raw = resp.body_bytes()
        return resp.status, (json.loads(raw) if raw else None)


@pytest.fixture
def svc() -> Service:
    return Service()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def one_line(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    lines = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "calendar_tools.http" and r.levelno == logging.INFO
    ]
    assert len(lines) == 1
    return lines[0]


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def check_contract(body: dict[str, Any], schema: dict[str, Any]) -> None:
    jsonschema.validate(body, schema, format_checker=FORMATS)
    # The schema allows extra keys; the service must not send any.
    assert set(body) <= set(schema["properties"])
    for slot in body.get("slots", []):
        assert set(slot) == set(schema["properties"]["slots"]["items"]["properties"])


async def test_available_end_to_end(svc, logs, schema) -> None:
    status, body = await svc.post(b'{"from_date": "2026-10-06", "to_date": "2026-10-07"}')
    assert status == 200
    assert body["status"] == "available"
    assert len(body["request_id"]) == 32
    check_contract(body, schema)
    line = one_line(logs)
    assert line["operation"] == "check_availability"
    assert (line["status"], line["diagnostic"], line["reason"]) == ("available", "ok", None)
    assert line["binding_id"] == "test-alpha"
    assert line["principal"] == jt.PRINCIPAL_A
    assert isinstance(line["provider_ms"], int)
    text = all_log_text(logs)
    for slot in body["slots"]:
        assert slot["slot_id"] not in text
        assert slot["slot_id"][3:] not in text


async def test_empty_body_is_allowed(svc, logs, schema) -> None:
    status, body = await svc.post(b"")
    assert status == 200 and body["status"] == "available"
    check_contract(body, schema)


async def test_unknown_fields_are_dropped(svc, logs, schema) -> None:
    status, body = await svc.post(json.dumps({"calendar": SENTINEL, "from_date": "2026-10-06"}).encode())
    assert body["status"] == "available"
    assert SENTINEL not in all_log_text(logs)


@pytest.mark.parametrize(
    ("request_body", "reason"),
    [
        ({"from_date": "2026-10-11", "to_date": "2026-10-11"}, "outside_bookable_hours"),
        ({"from_date": "2026-10-20"}, "beyond_booking_horizon"),
    ],
)
async def test_no_availability_end_to_end(svc, logs, schema, request_body, reason) -> None:
    status, body = await svc.post(json.dumps(request_body).encode())
    assert body["status"] == "no_availability"
    assert body["detail"] == {"reason": reason}
    check_contract(body, schema)
    line = one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == ("no_availability", "ok", reason)


async def test_fully_booked_end_to_end(svc, logs, schema) -> None:
    cal = load_bindings(json.dumps({"bindings": {"test-alpha": binding_doc()}}), frozenset({"fake"}))["test-alpha"].calendar_ref
    svc.provider.add_external_event(cal, datetime(2026, 10, 6, 17, tzinfo=UTC), datetime(2026, 10, 7, 0, tzinfo=UTC))
    status, body = await svc.post(b'{"from_date": "2026-10-06", "to_date": "2026-10-06"}')
    assert body["detail"] == {"reason": "fully_booked"}
    check_contract(body, schema)
    assert one_line(logs)["reason"] == "fully_booked"


@pytest.mark.parametrize(
    ("request_body", "fields"),
    [
        ({"duration_minutes": 20}, ["duration_minutes"]),
        ({"from_date": SENTINEL}, ["from_date"]),
        ({"from_date": "2026-10-08", "to_date": "2026-10-07"}, ["to_date"]),
        ({"earliest_time": SENTINEL, "latest_time": "25:00"}, ["earliest_time", "latest_time"]),
    ],
)
async def test_invalid_request_end_to_end(svc, logs, schema, request_body, fields) -> None:
    status, body = await svc.post(json.dumps(request_body).encode())
    assert status == 200
    assert body["status"] == "invalid_request"
    assert body["detail"]["fields"] == fields
    check_contract(body, schema)
    line = one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == ("invalid_request", "invalid_request", ",".join(fields))
    assert SENTINEL not in all_log_text(logs) and SENTINEL not in json.dumps(body)
    assert svc.provider.calls == [] and svc.keyring.load_calls == 0


async def test_body_not_an_object(svc, logs, schema) -> None:
    status, body = await svc.post(b"[1]")
    assert body["status"] == "invalid_request" and body["detail"] == {"fields": ["body"]}
    check_contract(body, schema)


@pytest.mark.parametrize(
    ("fail", "diagnostic", "reason", "provider_called"),
    [
        (("provider", ProviderUnavailable("rate_limited")), "provider_unavailable", "rate_limited", True),
        (("keys", SecretStoreUnavailable()), "secret_store_unreachable", None, False),
        (("keys", SecretInvalid("slot-token-key", "not_base64")), "secret_invalid", "slot-token-key.not_base64", False),
    ],
)
async def test_calendar_unavailable_end_to_end(svc, logs, schema, fail, diagnostic, reason, provider_called) -> None:
    where, exc = fail
    if where == "provider":
        svc.provider.fail_next("get_busy", exc)
    else:
        svc.keyring.fail_next(exc)
    status, body = await svc.post()
    assert status == 200
    assert set(body) == {"status", "request_id", "retryable"}
    assert body["status"] == "calendar_unavailable" and body["retryable"] is True
    check_contract(body, schema)
    line = one_line(logs)
    assert (line["status"], line["diagnostic"], line["reason"]) == ("calendar_unavailable", diagnostic, reason)
    assert (svc.provider.calls == ["get_busy"]) is provider_called


async def test_keys_loaded_once_per_request_through_the_service(svc, logs) -> None:
    await svc.post()
    await svc.post()
    assert svc.keyring.load_calls == 2
