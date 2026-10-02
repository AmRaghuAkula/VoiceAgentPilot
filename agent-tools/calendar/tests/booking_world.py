"""Shared fixtures for the booking tests (plan UC04b): a binding with the
fictional demo shape (America/Toronto, 14:00-19:00 Monday to Saturday, one day
of notice, a 7-day horizon), the fake provider, the fake claim store on the
same fake clock, `FakeKeyRing`, and helpers that issue real slot tokens.

The fake clock starts on Monday 2026-10-05 at 08:00 Toronto, so Tuesday
2026-10-06 14:00 Toronto (18:00Z) is the first bookable start.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from calendar_tools.core import tokens
from calendar_tools.core.bindings import Binding, load_bindings
from calendar_tools.core.booking import BookingService
from calendar_tools.core.claims import ClaimRecord, FakeClaimStore
from calendar_tools.core.deadline import Deadline
from calendar_tools.core.identity import CalendarIdentityCache, CellKey, calendar_key, floor5
from calendar_tools.http.app import RequestContext
from calendar_tools.providers.fake import FakeCalendarProvider
from tests.fakes import jwt_tokens as jt
from tests.fakes.claim_hooks import BarrierHooks
from tests.fakes.clock import FakeClock, FakeNonceSource
from tests.fakes.keyring import FINGERPRINT_KEY, FakeKeyRing
from tests.test_check_availability import binding_doc

SLOT = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)  # Tuesday 14:00 Toronto
PHONE = "+16135550123"
OTHER_PHONE = "+16135550145"
NAME = "Jordan Example"


class YieldingClock(FakeClock):
    """A fake clock whose `sleep` also yields to the event loop, so a polling
    task lets a concurrent one run (race tests)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_sleep: list[Any] = []

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        for callback in list(self.on_sleep):
            callback()
        await asyncio.sleep(0)


def make_bindings(docs: dict[str, dict[str, Any]], providers: frozenset[str] = frozenset({"fake"})) -> dict[str, Binding]:
    return dict(load_bindings(json.dumps({"bindings": docs}), providers))


class World:
    def __init__(self, *, providers: dict[str, FakeCalendarProvider] | None = None, **binding_changes: Any) -> None:
        self.clock = YieldingClock()
        self.providers = providers or {"fake": FakeCalendarProvider()}
        self.provider = next(iter(self.providers.values()))
        self.keyring = FakeKeyRing()
        self.hooks = BarrierHooks()
        self.store = FakeClaimStore(self.clock, self.hooks)
        self.nonce = FakeNonceSource()
        self.identity = CalendarIdentityCache()
        self.bindings: dict[str, Binding] = make_bindings(
            {"test-alpha": binding_doc(**binding_changes)}, frozenset(self.providers)
        )
        self.service = BookingService(
            provider_for=lambda b: self.providers[b.provider],
            claim_store=self.store,
            keyring=self.keyring,
            identity_cache=self.identity,
            clock=self.clock,
            nonce=self.nonce,
        )

    @property
    def binding(self) -> Binding:
        return self.bindings["test-alpha"]

    def add_binding(self, binding_id: str, **changes: Any) -> Binding:
        docs = {"test-alpha": binding_doc()}
        docs[binding_id] = binding_doc(**changes)
        self.bindings[binding_id] = make_bindings(docs, frozenset(self.providers))[binding_id]
        return self.bindings[binding_id]

    def replace_binding(self, binding_id: str = "test-alpha", **changes: Any) -> Binding:
        self.bindings[binding_id] = make_bindings(
            {binding_id: binding_doc(**changes)}, frozenset(self.providers)
        )[binding_id]
        return self.bindings[binding_id]

    def ctx(self) -> RequestContext:
        return RequestContext(
            request_id="0" * 31 + "1",
            principal=jt.PRINCIPAL_A,
            deadline=Deadline(self.clock),
            binding_id="test-alpha",
        )

    def token(self, start: datetime = SLOT, duration: int = 30, binding_id: str = "test-alpha",
              ttl_minutes: int = 30, keys: Any = None) -> str:
        expiry = floor5(self.clock.now()) + timedelta(minutes=ttl_minutes)
        return tokens.issue(keys or self.keyring.keys, binding_id, start, duration, expiry)

    def body(self, start: datetime = SLOT, duration: int = 30, *, phone: str | None = PHONE,
             name: str | None = NAME, email: str | None = None, type_id: str = "phone_call",
             binding_id: str = "test-alpha", notes: Any = None, token: str | None = None) -> dict[str, Any]:
        contact: dict[str, Any] = {}
        if name is not None:
            contact["name"] = name
        if phone is not None:
            contact["phone"] = phone
        if email is not None:
            contact["email"] = email
        body: dict[str, Any] = {
            "slot_id": token or self.token(start, duration, binding_id),
            "start": start.astimezone(self.bindings[binding_id].timezone).isoformat(),
            "appointment_type": type_id,
            "contact": contact,
        }
        if notes is not None:
            body["notes"] = notes
        return body

    async def book(self, body: dict[str, Any] | None = None, binding_id: str = "test-alpha",
                   ctx: RequestContext | None = None) -> dict[str, Any]:
        self.last_ctx = ctx or self.ctx()
        return await self.service.book(self.bindings[binding_id], body or self.body(), self.last_ctx)

    # --- views ------------------------------------------------------------------

    def cal_key(self, binding_id: str = "test-alpha") -> str:
        provider = self.providers[self.bindings[binding_id].provider]
        canonical = provider.canonical_ids.get(self.bindings[binding_id].calendar_id, self.bindings[binding_id].calendar_id)
        return calendar_key(FINGERPRINT_KEY, self.bindings[binding_id].provider, canonical)

    def cell(self, at: datetime, binding_id: str = "test-alpha") -> CellKey:
        return CellKey(self.cal_key(binding_id), at)

    def records(self) -> dict[CellKey, ClaimRecord]:
        return self.store.records()

    def events(self, binding_id: str = "test-alpha") -> list[Any]:
        provider = self.providers[self.bindings[binding_id].provider]
        return [e for e in provider._store(self.bindings[binding_id].calendar_ref).values() if e.active]
