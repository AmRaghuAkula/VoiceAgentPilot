"""Test doubles: FakeClock, FakeStateStore, FakeSecretSource, FakeNotifier."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from sms_notify.core.config import load_settings
from sms_notify.core.deadline import current_deadline
from sms_notify.core.errors import (
    NotifierConfigError,
    SecretStoreUnavailable,
    StateStoreUnavailable,
)
from sms_notify.core.phone import is_e164
from sms_notify.ports import (
    NotifierCapabilities,
    NotifierConfig,
    OutboundMessage,
    SendResult,
    StoredItem,
)

TENANT_ID = "00000000-0000-0000-0000-0000000000f1"
APP_ID = "00000000-0000-0000-0000-0000000000a1"
ALLOWED_OID = "00000000-0000-0000-0000-0000000000b1"
OTHER_OID = "00000000-0000-0000-0000-0000000000b2"
FROM_NUMBER = "+16135550100"
RECIPIENT_1 = "+16135550199"
RECIPIENT_2 = "+14165550142"

BASE_ENV = {
    "SMS_FROM_NUMBER": FROM_NUMBER,
    "SMS_ALLOWED_COUNTRIES": "CA",
    "SMS_ALLOWED_PRINCIPALS": ALLOWED_OID,
    "SMS_AUTH_AUDIENCE": f"api://{APP_ID}",
    "SMS_AUTH_TENANT_ID": TENANT_ID,
    "SMS_ALLOWED_LABELS": "Alpha,Beta,Gamma,Delta,Note",
    "KEY_VAULT_URI": "https://kv-example.vault.azure.net/",
    "STATE_BLOB_URL": "https://stexample.blob.core.windows.net/sms-state",
}

TWILIO_SECRET = {
    "account_sid": "AC" + "0" * 31 + "1",
    "api_key_sid": "SK" + "0" * 31 + "2",
    "api_key_secret": "fake-api-key-secret-value-xyz",
}


def make_settings(**overrides: str):
    env = dict(BASE_ENV)
    env.update(overrides)
    return load_settings(env)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 2, 14, 30, tzinfo=UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


@dataclass
class FakeStateStore:
    items: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    fail_on: set[str] = field(default_factory=set)  # operation names that raise
    fail_keys: Callable[[str], bool] | None = None
    yield_between: bool = False  # interleave concurrent tasks at every call
    on_call: Callable[[str, str], None] | None = None
    timeouts: list[float] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    _counter: int = 0

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'

    async def _enter(self, op: str, key: str, timeout: float) -> None:
        self.calls.append((op, key))
        self.timeouts.append(timeout)
        if self.on_call:
            self.on_call(op, key)
        if self.yield_between:
            await asyncio.sleep(0)
        if op in self.fail_on or (self.fail_keys and self.fail_keys(key)):
            raise StateStoreUnavailable("fake storage failure")

    async def get(self, key: str, *, timeout: float) -> StoredItem | None:
        await self._enter("get", key, timeout)
        item = self.items.get(key)
        return None if item is None else StoredItem(body=item[0], etag=item[1])

    async def create(self, key: str, body: bytes, *, timeout: float) -> str | None:
        await self._enter("create", key, timeout)
        if key in self.items:
            return None
        etag = self._next_etag()
        self.items[key] = (body, etag)
        return etag

    async def replace(self, key: str, body: bytes, etag: str, *, timeout: float) -> str | None:
        await self._enter("replace", key, timeout)
        current = self.items.get(key)
        if current is None or current[1] != etag:
            return None
        new = self._next_etag()
        self.items[key] = (body, new)
        return new

    def json(self, key: str) -> dict:
        return json.loads(self.items[key][0])


class FakeSecretSource:
    def __init__(self, recipients: list[str] | None = None, *, raw: dict[str, str] | None = None):
        self.values = raw or {
            "sms-recipients": json.dumps({"recipients": recipients or [RECIPIENT_1]}),
            "twilio-api": json.dumps(TWILIO_SECRET),
        }
        self.fail = False
        self.timeouts: list[float] = []

    async def get_secret(self, name: str, *, timeout: float) -> str:
        self.timeouts.append(timeout)
        if self.fail or name not in self.values:
            raise SecretStoreUnavailable("fake secret failure")
        return self.values[name]


class FakeNotifier:
    """Records sends; behavior per recipient is a callable or an exception to raise."""

    name: ClassVar[str] = "twilio_sms"

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.behavior: dict[str, object] = {}
        self.clock = clock
        self.seen_budgets: list[float | None] = []
        self.counter = 0

    def capabilities(self, cfg: NotifierConfig) -> NotifierCapabilities:
        return NotifierCapabilities(frozenset({"text_short"}), 480, False, True, "global")

    def validate_config(self, cfg: NotifierConfig) -> None:
        if len(cfg.recipients) != 1 or not all(is_e164(r) for r in cfg.recipients):
            raise NotifierConfigError("bad recipients")

    async def send(self, cfg: NotifierConfig, message: OutboundMessage) -> SendResult:
        deadline = current_deadline.get()
        self.seen_budgets.append(None if deadline is None else deadline.remaining())
        recipient = cfg.recipients[0]
        action = self.behavior.get(recipient)
        if callable(action):
            action()
        elif isinstance(action, BaseException):
            raise action
        self.sent.append((recipient, message.text_body))
        self.counter += 1
        return SendResult(provider_message_id=f"SM{self.counter:032d}")
