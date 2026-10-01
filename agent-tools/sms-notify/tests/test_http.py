"""Auth and dispatcher (spec K4, K6, section 5 step 1, section 9 "auth and dispatcher"; G3 logging)."""

from __future__ import annotations

import json
import logging
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sms_notify.core.errors import ConfigError
from sms_notify.core.messages import REASONS
from sms_notify.core.service import CoreDeps
from sms_notify.http.auth import JwksCache
from sms_notify.http.dispatcher import ROUTE, Dispatcher, Request, Runtime
from tests.fakes import (
    ALLOWED_OID,
    APP_ID,
    OTHER_OID,
    RECIPIENT_1,
    TENANT_ID,
    TWILIO_SECRET,
    FakeClock,
    FakeNotifier,
    FakeSecretSource,
    FakeStateStore,
    make_settings,
)

KEY_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS_V1 = f"https://sts.windows.net/{TENANT_ID}/"
ISS_V2 = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
MESSAGE = "Alpha: one\nBeta: two"


def _jwk(key, kid):
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    data.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return data


def mint(key=KEY_A, kid="kid-a", **overrides):
    now = int(time.time())
    claims = {
        "aud": f"api://{APP_ID}",
        "iss": ISS_V1,
        "tid": TENANT_ID,
        "oid": ALLOWED_OID,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class JwksServer:
    def __init__(self, keys):
        self.keys = keys
        self.fetches = 0
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "login.microsoftonline.com"
        assert request.url.path == f"/{TENANT_ID}/discovery/v2.0/keys"
        self.fetches += 1
        if self.down:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"keys": self.keys})


class App:
    def __init__(self, env=None, jwks_keys=None, config_error=False):
        self.clock = FakeClock()
        self.jwks_server = JwksServer(jwks_keys if jwks_keys is not None else [_jwk(KEY_A, "kid-a")])
        self.settings = make_settings(**(env or {}))
        self.jwks = JwksCache(
            httpx.AsyncClient(transport=httpx.MockTransport(self.jwks_server)), TENANT_ID, self.clock
        )
        self.state = FakeStateStore()
        self.secrets = FakeSecretSource()
        self.notifier = FakeNotifier(self.clock)
        deps = CoreDeps(self.clock, self.secrets, self.state, self.notifier)
        self.config_error = config_error

        def factory():
            if self.config_error:
                raise ConfigError("SMS_FROM_NUMBER is missing")
            return Runtime(self.settings, self.jwks, deps)

        self.dispatcher = Dispatcher(factory, self.clock)

    async def call(self, body=None, token="default", method="POST", path=ROUTE, raw=None):
        headers = {}
        if token == "default":
            token = mint()
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        payload = raw if raw is not None else json.dumps(body if body is not None else {"message": MESSAGE}).encode()
        return await self.dispatcher.handle(Request(method, path, headers, payload))


def envelope(response):
    assert response.status == 200
    data = json.loads(response.body)
    expected_keys = {"ok", "code", "retry"} | ({"reason"} if data.get("code") == "invalid_request" else set())
    assert set(data) == expected_keys and data["retry"] is False
    if "reason" in data:
        assert data["reason"] in REASONS
    return data


# --- good tokens --------------------------------------------------------------------------------


@pytest.mark.parametrize("aud", [f"api://{APP_ID}", APP_ID])
@pytest.mark.parametrize("iss", [ISS_V1, ISS_V2])
async def test_good_token_sends(aud, iss):
    app = App()
    data = envelope(await app.call(token=mint(aud=aud, iss=iss)))
    assert data == {"ok": True, "code": "sent", "retry": False}
    assert app.notifier.sent == [(RECIPIENT_1, MESSAGE)]


async def test_leeway_accepts_just_expired_and_just_early_tokens():
    app = App()
    now = int(time.time())
    assert envelope(await app.call(token=mint(exp=now - 30)))["code"] == "sent"
    assert envelope(await app.call(token=mint(nbf=now + 30), body={"message": "Note: other"}))["code"] in (
        "sent",
        "rate_limited",
    )


# --- 401 cases ----------------------------------------------------------------------------------


def _bad_tokens():
    now = int(time.time())
    return {
        "missing": None,
        "garbage": "not.a.jwt",
        "bad-signature": mint(key=KEY_B, kid="kid-a"),
        "bad-aud": mint(aud="api://00000000-0000-0000-0000-0000000000ff"),
        "bad-iss": mint(iss="https://sts.windows.net/00000000-0000-0000-0000-0000000000ee/"),
        "bad-tid": mint(tid="00000000-0000-0000-0000-0000000000ee"),
        "expired": mint(exp=now - 120),
        "not-yet-valid": mint(nbf=now + 120),
        "no-oid": mint(oid=None),
        "hs256": jwt.encode({"oid": ALLOWED_OID}, "k" * 32, algorithm="HS256", headers={"kid": "kid-a"}),
    }


@pytest.mark.parametrize("name", list(_bad_tokens()))
async def test_invalid_tokens_get_401_with_empty_body(name):
    app = App()
    response = await app.call(token=_bad_tokens()[name])
    assert response.status == 401 and response.body == b""
    assert app.notifier.sent == []


async def test_non_bearer_scheme_is_401():
    app = App()
    response = await app.dispatcher.handle(
        Request("POST", ROUTE, {"authorization": "Basic abc"}, b'{"message":"x"}')
    )
    assert response.status == 401


# --- JWKS caching and refresh -------------------------------------------------------------------


async def test_jwks_is_cached():
    app = App()
    await app.call()
    await app.call(body={"message": "Note: second"})
    assert app.jwks_server.fetches == 1


async def test_unknown_kid_refresh_is_throttled_to_once_per_60s():
    app = App()
    await app.call()  # initial fetch
    assert app.jwks_server.fetches == 1
    app.clock.advance(61)
    app.jwks_server.keys = [_jwk(KEY_A, "kid-a"), _jwk(KEY_B, "kid-b")]
    response = await app.call(token=mint(key=KEY_B, kid="kid-b"), body={"message": "Note: rotated"})
    assert envelope(response)["code"] in ("sent", "rate_limited")
    assert app.jwks_server.fetches == 2
    # A second unknown kid inside 60 s: 401 with no fetch.
    response = await app.call(token=mint(key=KEY_B, kid="kid-c"))
    assert response.status == 401
    assert app.jwks_server.fetches == 2


async def test_unknown_kid_right_after_first_fetch_is_401_without_second_fetch():
    app = App()
    response = await app.call(token=mint(key=KEY_B, kid="kid-x"))
    assert response.status == 401
    assert app.jwks_server.fetches == 1


async def test_jwks_unreachable_is_200_unavailable():
    app = App()
    app.jwks_server.down = True
    data = envelope(await app.call())
    assert data == {"ok": False, "code": "unavailable", "retry": False}
    assert app.notifier.sent == []


async def test_stale_jwks_still_used_when_refresh_fails():
    app = App()
    await app.call()
    app.clock.advance(25 * 3600)
    app.jwks_server.down = True
    assert envelope(await app.call(body={"message": "Note: later"}))["code"] == "sent"


# --- allowlist and role ----------------------------------------------------------------------


async def test_valid_token_not_allowlisted_is_200_forbidden():
    app = App()
    data = envelope(await app.call(token=mint(oid=OTHER_OID)))
    assert data == {"ok": False, "code": "forbidden", "retry": False}
    assert app.notifier.sent == []


async def test_require_role_off_ignores_roles():
    app = App(env={"SMS_REQUIRE_ROLE": "false"})
    assert envelope(await app.call())["code"] == "sent"


async def test_require_role_on_without_role_is_forbidden():
    app = App(env={"SMS_REQUIRE_ROLE": "true"})
    assert envelope(await app.call(token=mint(roles=["Other.Role"])))["code"] == "forbidden"
    assert envelope(await app.call(token=mint()))["code"] == "forbidden"


async def test_require_role_on_with_role_sends():
    app = App(env={"SMS_REQUIRE_ROLE": "true"})
    assert envelope(await app.call(token=mint(roles=["Sms.Send"])))["code"] == "sent"


# --- body schema ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"message": ""}',
        b'{"message": 5}',
        b"{}",
        json.dumps({"message": "hi", "to": "+16135550100"}).encode(),
        json.dumps({"message": "hi", "recipient": "+16135550100"}).encode(),
        json.dumps({"message": "hi", "extra": 1}).encode(),
        b"\xff\xfe",
    ],
    ids=["json", "array", "empty", "type", "missing", "to", "recipient", "unknown", "utf8"],
)
async def test_bad_bodies_are_bad_request(raw):
    app = App()
    data = envelope(await app.call(raw=raw))
    assert data == {"ok": False, "code": "invalid_request", "retry": False, "reason": "bad_request"}
    assert app.notifier.sent == [] and app.state.calls == []


@pytest.mark.parametrize(
    "raw",
    [json.dumps({"message": "Note: " + "a" * 5000}).encode(), json.dumps({"message": "a" * 9000}).encode()],
    ids=["over-480", "over-byte-cap"],
)
async def test_over_length_is_too_long_not_bad_request(raw):
    app = App()
    data = envelope(await app.call(raw=raw))
    assert data == {"ok": False, "code": "invalid_request", "retry": False, "reason": "too_long"}
    assert app.notifier.sent == [] and app.state.calls == []


# --- T9, T10: template rejections carry only the reason, and claim nothing ------------------------

T9_CASES = [
    ("Website: zzsecretvalue", "unknown_label"),
    ("Alpha: zzsecretvalue" + chr(10) + "alpha: x", "duplicate_label"),
    ("Alpha: zzsecret.value", "bad_character"),
    ("Alpha zzsecretvalue", "bad_line"),
]


@pytest.mark.parametrize(("text", "reason"), T9_CASES)
async def test_t9_t10_rejection_has_no_echo_and_no_claim(text, reason):
    app = App()
    response = await app.call(body={"message": text})
    data = envelope(response)
    assert data == {"ok": False, "code": "invalid_request", "retry": False, "reason": reason}
    for fragment in ("zzsecret", "Alpha", "alpha", "Website", "Beta", "Gamma", "Note"):
        assert fragment.encode() not in response.body
    assert app.state.calls == [] and app.notifier.sent == [] and app.secrets.timeouts == []


async def test_invalid_label_config_is_200_unavailable():
    app = App(env={"SMS_ALLOWED_LABELS": ""})
    assert envelope(await app.call())["code"] == "unavailable"
    assert app.notifier.sent == []


async def test_config_error_is_200_unavailable_without_a_number():
    app = App(config_error=True)
    response = await app.call()
    assert envelope(response)["code"] == "unavailable"
    assert b"555" not in response.body


async def test_wrong_route_and_method():
    app = App()
    assert (await app.call(path="/api/other")).status == 404
    response = await app.call(method="GET")
    assert response.status == 405


# --- the always-200 matrix ------------------------------------------------------------------------


def _setup(case, app):
    if case == "rate_limited":
        app.state.items["cooldown"] = (json.dumps({"at": app.clock.now().isoformat()}).encode(), '"e"')
    elif case == "send_failed":
        from sms_notify.core.errors import NotifierRejected

        app.notifier.behavior[RECIPIENT_1] = NotifierRejected(status=400, error_code=21211)
    elif case == "send_unconfirmed":
        from sms_notify.core.errors import NotifierUnavailable

        app.notifier.behavior[RECIPIENT_1] = NotifierUnavailable(maybe_sent=True)
    elif case == "unavailable":
        app.secrets.fail = True
    elif case == "storage_unavailable":
        app.state.fail_on = {"create"}
    elif case == "unexpected":
        async def boom(*a, **k):
            raise RuntimeError("bug")

        app.secrets.get_secret = boom


MATRIX = [
    ("sent", {"message": MESSAGE}, True, "sent"),
    ("invalid", {"message": "Note: " + "a" * 481}, False, "invalid_request"),
    ("template", {"message": "Note: see example.com"}, False, "invalid_request"),
    ("rate_limited", {"message": MESSAGE}, False, "rate_limited"),
    ("send_failed", {"message": MESSAGE}, False, "send_failed"),
    ("send_unconfirmed", {"message": MESSAGE}, False, "send_unconfirmed"),
    ("unavailable", {"message": MESSAGE}, False, "unavailable"),
    ("storage_unavailable", {"message": MESSAGE}, False, "unavailable"),
    ("unexpected", {"message": MESSAGE}, False, "unavailable"),
]


@pytest.mark.parametrize(("case", "body", "ok", "code"), MATRIX, ids=[m[0] for m in MATRIX])
async def test_every_non_401_path_is_http_200(case, body, ok, code):
    app = App()
    _setup(case, app)
    response = await app.call(body=body)
    assert response.status == 200
    data = envelope(response)
    assert (data["ok"], data["code"], data["retry"]) == (ok, code, False)
    assert response.headers["Content-Type"] == "application/json"


async def test_already_sent_is_200_ok():
    app = App()
    await app.call()
    app.clock.advance(120)
    assert envelope(await app.call()) == {"ok": True, "code": "already_sent", "retry": False}


# --- G3: logging ---------------------------------------------------------------------------------


SECRET_BODY = "Gamma: zzprivatevalue\nDelta: +1 613 555 0177"


@pytest.mark.parametrize("case", ["sent", "send_failed", "unavailable", "unexpected", "invalid", "template"])
async def test_g3_log_has_no_body_no_full_number_no_secret(case, caplog):
    app = App()
    app.secrets.values["sms-recipients"] = json.dumps({"recipients": [RECIPIENT_1]})
    _setup(case, app)
    token = mint()
    caplog.set_level(logging.DEBUG)
    body = {"message": SECRET_BODY}
    if case == "invalid":
        body = {"message": SECRET_BODY, "to": RECIPIENT_1}
    elif case == "template":
        body = {"message": SECRET_BODY + chr(10) + "Note: zzprivatevalue.x"}
    await app.call(body=body, token=token)
    text = "\n".join(r.getMessage() for r in caplog.records)
    lines = [r for r in caplog.records if "sms_notify.request" in r.getMessage()]
    assert len(lines) == 1
    for forbidden in (
        "zzprivatevalue",
        "6135550177",
        "555 0177",
        RECIPIENT_1,
        RECIPIENT_1[1:],
        TWILIO_SECRET["api_key_secret"],
        token,
    ):
        assert forbidden not in text


async def test_log_line_fields(caplog):
    app = App()
    caplog.set_level(logging.INFO, logger="sms_notify")
    await app.call()
    line = json.loads(next(r.getMessage() for r in caplog.records if "sms_notify.request" in r.getMessage()))
    assert set(line) == {
        "event",
        "request_id",
        "status",
        "code",
        "reason",
        "caller_oid",
        "recipients",
        "chars",
        "encoding",
        "segments",
        "latency_ms",
    }
    assert line["code"] == "sent" and line["encoding"] == "GSM-7" and line["segments"] == 1
    assert line["recipients"][0]["to"] == "***0199"
    assert line["recipients"][0]["provider_message_id"].startswith("SM")


# --- review round 1 regressions ---------------------------------------------------------------


async def test_deeply_nested_json_is_invalid_request_not_500():
    app = App()
    response = await app.call(raw=b"[" * 8000)
    assert envelope(response)["reason"] == "bad_request"


async def test_forbidden_logs_the_caller_oid(caplog):
    app = App()
    caplog.set_level(logging.INFO, logger="sms_notify")
    await app.call(token=mint(oid=OTHER_OID))
    line = json.loads(next(r.getMessage() for r in caplog.records if "sms_notify.request" in r.getMessage()))
    assert line["code"] == "forbidden" and line["caller_oid"] == OTHER_OID
    await app.call()
    lines = [json.loads(r.getMessage()) for r in caplog.records if "sms_notify.request" in r.getMessage()]
    assert lines[-1]["caller_oid"] is None


async def test_unexpected_auth_fault_is_200_unavailable_and_logged_once(caplog):
    app = App()

    async def boom(kid, deadline):
        raise AttributeError("unexpected")

    app.jwks.get_key = boom
    caplog.set_level(logging.INFO, logger="sms_notify")
    response = await app.call()
    assert envelope(response)["code"] == "unavailable"
    assert sum("sms_notify.request" in r.getMessage() for r in caplog.records) == 1


async def test_jwks_non_object_body_is_unavailable():
    app = App()

    def handler(request):
        app.jwks_server.fetches += 1
        return httpx.Response(200, json=[1, 2])

    app.jwks = JwksCache(httpx.AsyncClient(transport=httpx.MockTransport(handler)), TENANT_ID, app.clock)
    from sms_notify.http.auth import JwksUnavailable
    from sms_notify.core.deadline import Deadline

    with pytest.raises(JwksUnavailable):
        await app.jwks.get_key("kid-a", Deadline(app.clock))


async def test_jwks_outage_is_not_amplified():
    app = App()
    app.jwks_server.down = True
    assert envelope(await app.call())["code"] == "unavailable"
    assert envelope(await app.call(token=mint(kid="kid-z")))["code"] == "unavailable"
    assert app.jwks_server.fetches == 1  # second request inside 60 s: no fetch
    app.clock.advance(61)
    app.jwks_server.down = False
    assert envelope(await app.call(body={"message": "Note: after outage"}))["code"] == "sent"
    assert app.jwks_server.fetches == 2


async def test_failed_unknown_kid_refresh_is_throttled():
    app = App()
    await app.call()
    app.clock.advance(61)
    app.jwks_server.down = True
    assert (await app.call(token=mint(key=KEY_B, kid="kid-n1"))).status in (200, 401)
    fetches = app.jwks_server.fetches
    assert (await app.call(token=mint(key=KEY_B, kid="kid-n2"))).status == 401
    assert app.jwks_server.fetches == fetches
