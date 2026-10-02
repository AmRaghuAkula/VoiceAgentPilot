"""G4 (spec 13.1; plan UC04b): two bindings with different providers, hours,
types and principals are served by one app instance with no cross-binding
leakage. The two providers are two instances of the fake adapter registered
under different names (`fake-a`, `fake-b`); UC08b makes one the Google
adapter."""

from __future__ import annotations

import json
from typing import Any

import pytest

from calendar_tools.core.availability import check_availability
from calendar_tools.core.booking import BookingService, make_book_appointment
from calendar_tools.core.claims import FakeClaimStore
from calendar_tools.core.identity import CalendarIdentityCache
from calendar_tools.http.app import CalendarService, Request
from calendar_tools.http.auth import Authenticator, JwksCache
from calendar_tools.providers.fake import FakeCalendarProvider
from tests.booking_world import PHONE, YieldingClock, make_bindings
from tests.fakes import jwt_tokens as jt
from tests.fakes.clock import FakeNonceSource
from tests.fakes.keyring import FakeKeyRing
from tests.test_check_availability import binding_doc

ALPHA_DOC = binding_doc(provider="fake-a", allowed_principals=[jt.PRINCIPAL_A])
BETA_DOC = binding_doc(
    provider="fake-b",
    calendar_id="primary",
    credential_secret_name="cal-binding-test-beta-fake",
    bookable_hours={d: [["09:00", "11:00"]] for d in ("mon", "tue", "wed", "thu", "fri")},
    appointment_types=[{"id": "video_call", "label": "Video call"}],
    allowed_principals=[jt.PRINCIPAL_B],
)


class G4:
    def __init__(self) -> None:
        self.clock = YieldingClock()
        self.providers = {"fake-a": FakeCalendarProvider("fake-a"), "fake-b": FakeCalendarProvider("fake-b")}
        self.keyring = FakeKeyRing()
        self.bindings = make_bindings({"test-alpha": ALPHA_DOC, "test-beta": BETA_DOC}, frozenset(self.providers))
        booking = BookingService(
            provider_for=lambda b: self.providers[b.provider],
            claim_store=FakeClaimStore(self.clock),
            keyring=self.keyring,
            identity_cache=CalendarIdentityCache(),
            clock=self.clock,
            nonce=FakeNonceSource(),
        )

        async def check(binding, body, ctx):
            return await check_availability(
                binding, body, ctx, provider=self.providers[binding.provider], keyring=self.keyring, clock=self.clock
            )

        config = jt.make_config()
        self.service = CalendarService(
            config=config,
            bindings=self.bindings,
            authenticator=Authenticator(
                config, JwksCache(jt.TENANT_ID, jt.jwks_client(jt.JwksStub()), self.clock), self.clock
            ),
            operations={"check_availability": check, "book_appointment": make_book_appointment(booking)},
            clock=self.clock,
            nonce=FakeNonceSource(),
        )

    async def post(self, binding_id: str, op: str, body: dict[str, Any], principal: str) -> tuple[int, Any]:
        token = jt.make_token(self.clock.now().timestamp(), oid=principal, appid=principal)
        resp = await self.service.handle(
            Request(
                method="POST",
                path=f"/api/v1/bindings/{binding_id}/{op}",
                headers={"Authorization": f"Bearer {token}"},
                body=json.dumps(body).encode(),
            )
        )
        raw = resp.body_bytes()
        return resp.status, (json.loads(raw) if raw else None)


@pytest.fixture
def g4() -> G4:
    return G4()


async def test_each_binding_answers_with_its_own_types_and_hours(g4) -> None:
    _, a = await g4.post("test-alpha", "check-availability", {"from_date": "2026-10-06", "to_date": "2026-10-06"}, jt.PRINCIPAL_A)
    _, b = await g4.post("test-beta", "check-availability", {"from_date": "2026-10-06", "to_date": "2026-10-06"}, jt.PRINCIPAL_B)
    assert [t["id"] for t in a["appointment_types"]] == ["phone_call", "in_person"]
    assert [t["id"] for t in b["appointment_types"]] == ["video_call"]
    assert {s["start"][11:13] for s in a["slots"]} <= {"14", "15", "16", "17", "18"}
    assert {s["start"][11:13] for s in b["slots"]} <= {"09", "10"}
    assert g4.providers["fake-a"].calls == ["get_busy"] and g4.providers["fake-b"].calls == ["get_busy"]


async def test_slot_token_from_a_is_wrong_binding_on_b(g4) -> None:
    _, a = await g4.post("test-alpha", "check-availability", {"from_date": "2026-10-06", "to_date": "2026-10-06"}, jt.PRINCIPAL_A)
    slot = a["slots"][0]
    body = {"slot_id": slot["slot_id"], "start": slot["start"], "appointment_type": "video_call",
            "contact": {"name": "Jordan Example", "phone": PHONE}}
    status, resp = await g4.post("test-beta", "book-appointment", body, jt.PRINCIPAL_B)
    assert status == 200
    assert resp["status"] == "invalid_slot" and resp["detail"] == {"reason": "wrong_binding"}
    assert "create_event" not in g4.providers["fake-b"].calls


@pytest.mark.parametrize(
    ("binding_id", "principal"),
    [("test-beta", jt.PRINCIPAL_A), ("test-alpha", jt.PRINCIPAL_B)],
)
async def test_principal_not_listed_is_forbidden(g4, binding_id, principal) -> None:
    status, body = await g4.post(binding_id, "book-appointment", {}, principal)
    assert status == 403 and body == {"error": "forbidden"}
    assert all(p.calls == [] for p in g4.providers.values())


async def test_bookings_land_only_on_their_own_provider(g4) -> None:
    for binding_id, principal, type_id in (
        ("test-alpha", jt.PRINCIPAL_A, "phone_call"),
        ("test-beta", jt.PRINCIPAL_B, "video_call"),
    ):
        _, offer = await g4.post(binding_id, "check-availability", {"from_date": "2026-10-06", "to_date": "2026-10-06"}, principal)
        slot = offer["slots"][0]
        body = {"slot_id": slot["slot_id"], "start": slot["start"], "appointment_type": type_id,
                "contact": {"name": "Jordan Example", "phone": PHONE}}
        _, resp = await g4.post(binding_id, "book-appointment", body, principal)
        assert resp["status"] == "booked"
        assert resp["booking"]["appointment_type"]["id"] == type_id
    assert g4.providers["fake-a"].calls.count("create_event") == 1
    assert g4.providers["fake-b"].calls.count("create_event") == 1
    [event_a] = g4.providers["fake-a"]._store(g4.bindings["test-alpha"].calendar_ref).values()
    [event_b] = g4.providers["fake-b"]._store(g4.bindings["test-beta"].calendar_ref).values()
    assert event_a.meta.binding_id == "test-alpha" and event_b.meta.binding_id == "test-beta"
