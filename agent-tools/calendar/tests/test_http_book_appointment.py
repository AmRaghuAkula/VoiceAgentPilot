"""`book_appointment` end to end through `CalendarService.handle` with a real
token (plan UC04b): every response shape validates against the contract,
`booking.host` is omitted without `host_display_name`, `notes` are ignored
unless the binding accepts them, and each request logs exactly one line with
`operation=book_appointment` and its own diagnostic and reason."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import jsonschema
import pytest
import yaml

from calendar_tools.core.availability import make_check_availability
from calendar_tools.core.booking import BookingService, make_book_appointment
from calendar_tools.core.claims import FakeClaimStore
from calendar_tools.core.identity import CalendarIdentityCache
from calendar_tools.core.ports import ProviderTimeout, ProviderUnavailable
from calendar_tools.http.app import CalendarService, Request
from calendar_tools.http.auth import Authenticator, JwksCache
from calendar_tools.providers.fake import FakeCalendarProvider
from tests.booking_world import PHONE, SLOT, YieldingClock, make_bindings
from tests.fakes import jwt_tokens as jt
from tests.fakes.clock import FakeNonceSource
from tests.fakes.keyring import FakeKeyRing
from tests.test_check_availability import binding_doc
from tests.test_openapi_doc import DOC_PATH, FORMATS, _response_schema

BOOK = "/api/v1/bindings/test-alpha/book-appointment"
CHECK = "/api/v1/bindings/test-alpha/check-availability"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    doc = yaml.safe_load(DOC_PATH.read_text(encoding="utf-8"))
    return _response_schema(doc, "book_appointment")


class HttpWorld:
    def __init__(self, **binding_changes: Any) -> None:
        self.clock = YieldingClock()
        self.provider = FakeCalendarProvider()
        self.keyring = FakeKeyRing()
        self.store = FakeClaimStore(self.clock)
        self.bindings = make_bindings({"test-alpha": binding_doc(**binding_changes)})
        self.booking = BookingService(
            provider_for=lambda b: self.provider,
            claim_store=self.store,
            keyring=self.keyring,
            identity_cache=CalendarIdentityCache(),
            clock=self.clock,
            nonce=FakeNonceSource(),
        )
        config = jt.make_config()
        authenticator = Authenticator(
            config, JwksCache(jt.TENANT_ID, jt.jwks_client(jt.JwksStub()), self.clock), self.clock
        )
        self.service = CalendarService(
            config=config,
            bindings=self.bindings,
            authenticator=authenticator,
            operations={
                "check_availability": make_check_availability(self.provider, self.keyring, self.clock),
                "book_appointment": make_book_appointment(self.booking),
            },
            clock=self.clock,
            nonce=FakeNonceSource(),
        )

    async def post(self, path: str, body: Any) -> dict[str, Any]:
        token = jt.make_token(self.clock.now().timestamp())
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        resp = await self.service.handle(
            Request(method="POST", path=path, headers={"Authorization": f"Bearer {token}"}, body=raw)
        )
        assert resp.status == 200
        return json.loads(resp.body_bytes())

    async def offered(self) -> dict[str, Any]:
        resp = await self.post(CHECK, {"from_date": "2026-10-06", "to_date": "2026-10-06"})
        assert resp["status"] == "available"
        return resp["slots"][0]

    async def book_offered(self, **extra: Any) -> dict[str, Any]:
        slot = await self.offered()
        body = {
            "slot_id": slot["slot_id"],
            "start": slot["start"],
            "appointment_type": "phone_call",
            "contact": {"name": "Jordan Example", "phone": PHONE},
            **extra,
        }
        return await self.post(BOOK, body)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "calendar_tools.http" and r.levelno == logging.INFO
    ]


def check_contract(body: dict[str, Any], schema: dict[str, Any]) -> None:
    jsonschema.validate(body, schema, format_checker=FORMATS)
    assert set(body) <= set(schema["properties"])
    if "booking" in body:
        assert set(body["booking"]) <= set(schema["properties"]["booking"]["properties"])
    if "detail" in body:
        assert set(body["detail"]) <= set(schema["properties"]["detail"]["properties"])


async def test_check_then_book_end_to_end(logs, schema) -> None:
    w = HttpWorld()
    body = await w.book_offered()
    assert body["status"] == "booked" and body["replayed"] is False
    assert len(body["request_id"]) == 32
    assert "host" not in body["booking"]
    check_contract(body, schema)
    book_line = lines(logs)[-1]
    assert book_line["operation"] == "book_appointment"
    assert (book_line["status"], book_line["diagnostic"], book_line["reason"]) == ("booked", "ok", None)
    assert book_line["binding_id"] == "test-alpha" and book_line["principal"] == jt.PRINCIPAL_A
    assert book_line["replayed"] is False and isinstance(book_line["provider_ms"], int)


async def test_replay_end_to_end_logs_replayed(logs, schema) -> None:
    w = HttpWorld()
    slot = await w.offered()
    body = {"slot_id": slot["slot_id"], "start": slot["start"], "appointment_type": "phone_call",
            "contact": {"name": "Jordan Example", "phone": PHONE}}
    await w.post(BOOK, body)
    again = await w.post(BOOK, body)
    assert again["status"] == "booked" and again["replayed"] is True
    check_contract(again, schema)
    assert lines(logs)[-1]["replayed"] is True


async def test_host_present_when_configured(schema) -> None:
    w = HttpWorld(host_display_name="Alex")
    body = await w.book_offered()
    assert body["booking"]["host"] == {"display_name": "Alex"}
    check_contract(body, schema)


async def test_notes_ignored_unless_accepted(schema) -> None:
    w = HttpWorld(event_description_template="{notes_block}Ref: {booking_ref}")
    body = await w.book_offered(notes="Alpha: one")
    assert body["status"] == "booked"
    [event] = [e for e in w.provider._store(w.bindings["test-alpha"].calendar_ref).values()]
    assert "Alpha" not in event.description


async def test_notes_used_when_accepted(schema) -> None:
    w = HttpWorld(accept_notes=True, event_description_template="{notes_block}Ref: {booking_ref}")
    body = await w.book_offered(notes="Alpha: one")
    [event] = [e for e in w.provider._store(w.bindings["test-alpha"].calendar_ref).values()]
    assert event.description.startswith("Notes: Alpha: one\n")
    check_contract(body, schema)


def _token_for(w: HttpWorld) -> dict[str, Any]:
    from calendar_tools.core import tokens

    token = tokens.issue(w.keyring.keys, "test-alpha", SLOT, 30, w.clock.now() + timedelta(minutes=30))
    return {"slot_id": token, "start": "2026-10-06T14:00:00-04:00", "appointment_type": "phone_call",
            "contact": {"name": "Jordan Example", "phone": PHONE}}


@pytest.mark.parametrize(
    ("setup", "status", "diagnostic", "reason"),
    [
        (lambda w, b: b.update(slot_id="v1.garbage"), "invalid_slot", "invalid_slot", "malformed"),
        (lambda w, b: b.update(start="2026-10-06T15:00:00-04:00"), "invalid_slot", "invalid_slot", "start_mismatch"),
        (lambda w, b: b.pop("contact"), "invalid_request", "invalid_request", "contact"),
        (lambda w, b: w.clock.advance(31 * 60), "invalid_slot", "invalid_slot", "expired"),
        (lambda w, b: w.clock.set(SLOT - timedelta(hours=1)), "invalid_slot", "invalid_slot", "expired"),
        (
            lambda w, b: w.provider.add_external_event(w.bindings["test-alpha"].calendar_ref, SLOT, SLOT + timedelta(minutes=30)),
            "slot_unavailable", "claim_conflict", "busy",
        ),
        (lambda w, b: w.provider.fail_next("find_bookings", ProviderUnavailable()), "calendar_unavailable", "provider_unavailable", None),
        (lambda w, b: w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True)), "booking_unconfirmed", "create_unconfirmed", "provider_timeout"),
        (lambda w, b: w.store.fail_next("try_claim"), "calendar_unavailable", "claim_store_unreachable", "injected"),
    ],
    ids=["malformed", "start_mismatch", "no_contact", "expired", "expired_late", "taken", "unavailable", "unconfirmed", "claim_store"],
)
async def test_every_non_ok_shape_validates_and_logs_its_reason(logs, schema, setup, status, diagnostic, reason) -> None:
    w = HttpWorld()
    body = _token_for(w)
    setup(w, body)
    resp = await w.post(BOOK, body)
    assert resp["status"] == status
    check_contract(resp, schema)
    line = lines(logs)[-1]
    assert (line["status"], line["diagnostic"], line["reason"]) == (status, diagnostic, reason)
    if status == "calendar_unavailable":
        assert resp["retryable"] is True


async def test_limit_reached_shape(logs, schema) -> None:
    w = HttpWorld(max_active_bookings_per_contact=1)
    assert (await w.book_offered())["status"] == "booked"
    # A different slot by the same contact hits the limit.
    from calendar_tools.core import tokens

    other = tokens.issue(w.keyring.keys, "test-alpha", SLOT + timedelta(days=1), 30, w.clock.now() + timedelta(minutes=30))
    resp = await w.post(BOOK, {"slot_id": other, "start": "2026-10-07T14:00:00-04:00", "appointment_type": "phone_call",
                               "contact": {"name": "Jordan Example", "phone": PHONE}})
    assert resp == {"status": "limit_reached", "request_id": resp["request_id"]}
    check_contract(resp, schema)
    assert (lines(logs)[-1]["diagnostic"], lines(logs)[-1]["reason"]) == ("limit_reached", None)


async def test_body_required(logs, schema) -> None:
    w = HttpWorld()
    resp = await w.post(BOOK, b"")
    assert resp["status"] == "invalid_request" and resp["detail"] == {"fields": ["body"]}
    check_contract(resp, schema)
