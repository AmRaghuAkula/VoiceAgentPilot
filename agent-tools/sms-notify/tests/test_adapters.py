"""Adapters over httpx.MockTransport: Twilio, Blob state, Key Vault (spec section 9, plan P4)."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest

from sms_notify.adapters.azure_token import ManagedIdentityTokenProvider, TokenUnavailable
from sms_notify.adapters.blob_state import BlobStateStore
from sms_notify.adapters.keyvault_secrets import KeyVaultSecretSource
from sms_notify.adapters.twilio_sms import TwilioSmsNotifier
from sms_notify.core.deadline import Deadline, current_deadline
from sms_notify.core.errors import (
    NotifierAuthError,
    NotifierConfigError,
    NotifierRejected,
    NotifierUnavailable,
    SecretStoreUnavailable,
    StateStoreUnavailable,
)
from sms_notify.ports import NotifierConfig, OutboundMessage
from tests.fakes import FROM_NUMBER, RECIPIENT_1, RECIPIENT_2, TWILIO_SECRET, FakeClock, FakeSecretSource

SID = "SM" + "a" * 32


class StaticTokens:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.scopes: list[str] = []

    async def get_token(self, scope: str, *, timeout: float) -> str:
        self.scopes.append(scope)
        if self.fail:
            raise TokenUnavailable("no token")
        return "fake-access-token"


def _cfg(*recipients: str, **overrides) -> NotifierConfig:
    fields = dict(
        notifier="twilio_sms",
        channel_config={"from_number": FROM_NUMBER},
        credential_secret_name="twilio-api",
        recipients=recipients or (RECIPIENT_1,),
    )
    fields.update(overrides)
    return NotifierConfig(**fields)


MESSAGE = OutboundMessage(subject=None, text_body="Alpha: one\nBeta: two", idempotency_key="h")


def _notifier(handler, secrets=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TwilioSmsNotifier(client, secrets or FakeSecretSource()), client


# --- Twilio ----------------------------------------------------------------------------------


async def test_twilio_exact_request_and_201_is_sent():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": SID, "status": "queued"})

    notifier, _ = _notifier(handler)
    result = await notifier.send(_cfg(), MESSAGE)
    assert result.provider_message_id == SID
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.scheme == "https" and req.url.host == "api.twilio.com"
    assert req.url.path == f"/2010-04-01/Accounts/{TWILIO_SECRET['account_sid']}/Messages.json"
    form = parse_qs(req.content.decode())
    assert form == {"To": [RECIPIENT_1], "From": [FROM_NUMBER], "Body": [MESSAGE.text_body]}
    expected = base64.b64encode(
        f"{TWILIO_SECRET['api_key_sid']}:{TWILIO_SECRET['api_key_secret']}".encode()
    ).decode()
    assert req.headers["authorization"] == f"Basic {expected}"
    assert "StatusCallback" not in req.content.decode()


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_twilio_error_is_rejected_with_only_the_code(status):
    def handler(request):
        return httpx.Response(status, json={"code": 21211, "message": "secret detail body"})

    notifier, _ = _notifier(handler)
    with pytest.raises(NotifierRejected) as err:
        await notifier.send(_cfg(), MESSAGE)
    assert err.value.error_code == 21211 and err.value.status == status
    assert "secret detail body" not in str(err.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_twilio_auth_error(status):
    notifier, _ = _notifier(lambda r: httpx.Response(status, json={"code": 20003}))
    with pytest.raises(NotifierAuthError) as err:
        await notifier.send(_cfg(), MESSAGE)
    assert err.value.error_code == 20003


async def test_twilio_redirect_is_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.invalid/"})

    notifier, _ = _notifier(handler)
    with pytest.raises(NotifierRejected):
        await notifier.send(_cfg(), MESSAGE)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("t"), httpx.RemoteProtocolError("dropped"), httpx.ReadError("reset"), httpx.WriteTimeout("w")],
)
async def test_twilio_timeout_or_drop_after_sending_is_unconfirmed_and_not_retried(exc):
    calls = []

    def handler(request):
        calls.append(request)
        raise exc

    notifier, _ = _notifier(handler)
    with pytest.raises(NotifierUnavailable) as err:
        await notifier.send(_cfg(), MESSAGE)
    assert err.value.maybe_sent is True
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow")])
async def test_twilio_failure_before_sending_is_not_sent(exc):
    def handler(request):
        raise exc

    notifier, _ = _notifier(handler)
    with pytest.raises(NotifierUnavailable) as err:
        await notifier.send(_cfg(), MESSAGE)
    assert err.value.maybe_sent is False


async def test_twilio_no_time_left_does_not_post():
    calls = []
    notifier, _ = _notifier(lambda r: calls.append(r) or httpx.Response(201, json={}))
    clock = FakeClock()
    deadline = Deadline(clock, 1.0)
    clock.advance(2.0)
    token = current_deadline.set(deadline)
    try:
        with pytest.raises((NotifierUnavailable, SecretStoreUnavailable)):
            await notifier.send(_cfg(), MESSAGE)
    finally:
        current_deadline.reset(token)
    assert calls == []


@pytest.mark.parametrize(
    "cfg",
    [
        _cfg(RECIPIENT_1, RECIPIENT_2),
        NotifierConfig("twilio_sms", {"from_number": FROM_NUMBER}, "twilio-api", ()),
        _cfg("6135550199"),
        _cfg(channel_config={"from_number": "bad"}),
        _cfg(credential_secret_name=None),
        _cfg(notifier="other"),
    ],
    ids=["two-recipients", "zero-recipients", "not-e164", "bad-from", "no-secret", "other-notifier"],
)
def test_twilio_validate_config_rejects(cfg):
    notifier, _ = _notifier(lambda r: httpx.Response(201))
    with pytest.raises(NotifierConfigError):
        notifier.validate_config(cfg)


def test_twilio_capabilities():
    notifier, _ = _notifier(lambda r: httpx.Response(201))
    caps = notifier.capabilities(_cfg())
    assert caps.formats == frozenset({"text_short"}) and caps.max_body_chars == 480
    assert caps.native_idempotency is False and caps.leaves_boundary is True
    assert caps.residency == "global"


@pytest.mark.parametrize(
    "secret",
    [
        "not json",
        json.dumps({"account_sid": "AC123"}),
        json.dumps({**TWILIO_SECRET, "account_sid": "AC" + "0" * 31 + "1/../x"}),
        json.dumps({**TWILIO_SECRET, "api_key_secret": ""}),
    ],
)
async def test_twilio_bad_credential_secret_is_config_error(secret):
    calls = []
    secrets = FakeSecretSource(raw={"twilio-api": secret})
    notifier, _ = _notifier(lambda r: calls.append(r) or httpx.Response(201), secrets)
    with pytest.raises(NotifierConfigError):
        await notifier.send(_cfg(), MESSAGE)
    assert calls == []


# --- Blob state --------------------------------------------------------------------------------


CONTAINER = "https://stexample.blob.core.windows.net/sms-state"


class BlobServer:
    def __init__(self):
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.n = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path.split("/sms-state/", 1)[1]
        if request.method == "GET":
            if key not in self.blobs:
                return httpx.Response(404)
            body, etag = self.blobs[key]
            return httpx.Response(200, content=body, headers={"ETag": etag})
        inm, im = request.headers.get("if-none-match"), request.headers.get("if-match")
        if inm == "*" and key in self.blobs:
            return httpx.Response(409)
        if im is not None and (key not in self.blobs or self.blobs[key][1] != im):
            return httpx.Response(412 if key in self.blobs else 404)
        self.n += 1
        etag = f'"0x{self.n}"'
        self.blobs[key] = (request.content, etag)
        return httpx.Response(201, headers={"ETag": etag})


def _blob(server, tokens=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return BlobStateStore(client, CONTAINER, tokens or StaticTokens())


async def test_blob_create_get_replace_semantics():
    server = BlobServer()
    store = _blob(server)
    etag = await store.create("dedupe/abc", b"{}", timeout=0.5)
    assert etag
    assert await store.create("dedupe/abc", b"{}", timeout=0.5) is None
    item = await store.get("dedupe/abc", timeout=0.5)
    assert item.body == b"{}" and item.etag == etag
    new = await store.replace("dedupe/abc", b"{1}", etag, timeout=0.5)
    assert new and new != etag
    assert await store.replace("dedupe/abc", b"{2}", etag, timeout=0.5) is None  # stale etag
    assert await store.replace("dedupe/gone", b"{}", etag, timeout=0.5) is None  # deleted
    assert await store.get("missing", timeout=0.5) is None
    put = server.requests[0]
    assert put.headers["x-ms-blob-type"] == "BlockBlob"
    assert put.headers["if-none-match"] == "*"
    assert put.headers["authorization"] == "Bearer fake-access-token"
    assert put.url.host == "stexample.blob.core.windows.net"


async def test_blob_412_on_create_means_exists():
    store = _blob(lambda r: httpx.Response(412))
    assert await store.create("cooldown", b"{}", timeout=0.5) is None


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_blob_errors_are_unavailable(status):
    store = _blob(lambda r: httpx.Response(status))
    with pytest.raises(StateStoreUnavailable):
        await store.create("cooldown", b"{}", timeout=0.5)
    with pytest.raises(StateStoreUnavailable):
        await store.get("cooldown", timeout=0.5)


async def test_blob_transport_failure_and_token_failure_are_unavailable():
    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(StateStoreUnavailable):
        await _blob(boom).get("cooldown", timeout=0.5)
    with pytest.raises(StateStoreUnavailable):
        await _blob(BlobServer(), StaticTokens(fail=True)).get("cooldown", timeout=0.5)


async def test_blob_rejects_bad_keys_and_zero_timeout():
    store = _blob(BlobServer())
    for key in ("../x", "a//b", "a?b", ""):
        with pytest.raises(StateStoreUnavailable):
            await store.get(key, timeout=0.5)
    with pytest.raises(StateStoreUnavailable):
        await store.get("cooldown", timeout=0)


# --- Key Vault -----------------------------------------------------------------------------------


def _kv(handler, clock=None, tokens=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KeyVaultSecretSource(
        client, "https://kv-example.vault.azure.net/", tokens or StaticTokens(), clock or FakeClock()
    )


async def test_keyvault_reads_over_rest_and_caches_for_5_minutes():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"value": "v1"})

    clock = FakeClock()
    tokens = StaticTokens()
    kv = _kv(handler, clock, tokens)
    assert await kv.get_secret("twilio-api", timeout=1.5) == "v1"
    req = seen[0]
    assert str(req.url) == "https://kv-example.vault.azure.net/secrets/twilio-api?api-version=7.4"
    assert req.headers["authorization"] == "Bearer fake-access-token"
    assert tokens.scopes == ["https://vault.azure.net/.default"]
    clock.advance(299)
    await kv.get_secret("twilio-api", timeout=1.5)
    assert len(seen) == 1
    clock.advance(2)
    await kv.get_secret("twilio-api", timeout=1.5)
    assert len(seen) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_keyvault_errors_are_unavailable(status):
    kv = _kv(lambda r: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(SecretStoreUnavailable):
        await kv.get_secret("twilio-api", timeout=1.5)


async def test_keyvault_unreachable_and_bad_name():
    def boom(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(SecretStoreUnavailable):
        await _kv(boom).get_secret("twilio-api", timeout=1.5)
    with pytest.raises(SecretStoreUnavailable):
        await _kv(lambda r: httpx.Response(200, json={"value": 1})).get_secret("twilio-api", timeout=1.5)
    with pytest.raises(SecretStoreUnavailable):
        await _kv(lambda r: httpx.Response(200)).get_secret("../keys", timeout=1.5)


# --- managed identity token provider -------------------------------------------------------------


class FakeAccessToken:
    def __init__(self, token, expires_on):
        self.token, self.expires_on = token, expires_on


async def test_token_provider_caches_and_fails_closed():
    import time

    calls = []

    class Cred:
        def get_token(self, scope):
            calls.append(scope)
            return FakeAccessToken("tok", time.time() + 3600)

    provider = ManagedIdentityTokenProvider(Cred())
    assert await provider.get_token("s", timeout=1.0) == "tok"
    assert await provider.get_token("s", timeout=1.0) == "tok"
    assert calls == ["s"]

    class Broken:
        def get_token(self, scope):
            raise RuntimeError("no identity")

    with pytest.raises(TokenUnavailable):
        await ManagedIdentityTokenProvider(Broken()).get_token("s", timeout=1.0)
