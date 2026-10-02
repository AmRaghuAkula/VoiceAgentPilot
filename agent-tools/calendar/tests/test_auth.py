"""In-code Entra authentication (spec section 9.1, plan UC02b "JWT checks").

Every check has its own failing test and its own 401 `reason` (spec rev 3.2
section 10). All keys are generated locally; the JWKS endpoint is a stub.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from calendar_tools.core.deadline import Deadline
from calendar_tools.http.auth import (
    JWKS_MAX_AGE,
    JWKS_RETRY_AFTER_FAILURE,
    UNKNOWN_KID_REFRESH_INTERVAL,
    UNAUTHORIZED_REASONS,
    Authenticator,
    JwksCache,
    JwksUnreachable,
    Principal,
    Unauthorized,
)
from tests.fakes import jwt_tokens as jt
from tests.fakes.clock import FakeClock


def _auth(clock: FakeClock, stub: jt.JwksStub, **config: str) -> Authenticator:
    cache = JwksCache(jt.TENANT_ID, jt.jwks_client(stub), clock)
    return Authenticator(jt.make_config(**config), cache, clock)


async def _run(auth: Authenticator, clock: FakeClock, header: str | None) -> Principal:
    return await auth.authenticate(header, Deadline(clock))


async def _reason(auth: Authenticator, clock: FakeClock, header: str | None) -> str | None:
    with pytest.raises(Unauthorized) as info:
        await _run(auth, clock, header)
    err = info.value
    assert err.diagnostic == "unauthorized"
    assert err.reason in UNAUTHORIZED_REASONS
    return err.reason


def _bearer(token: str) -> str:
    return f"Bearer {token}"


# --- accepted tokens ---------------------------------------------------------


async def test_valid_v1_token(clock, jwks_stub, make_token) -> None:
    principal = await _run(_auth(clock, jwks_stub), clock, _bearer(make_token()))
    assert principal == Principal(jt.PRINCIPAL_A)


async def test_valid_v2_issuer(clock, jwks_stub, make_token) -> None:
    token = make_token(iss=jt.ISS_V2)
    assert (await _run(_auth(clock, jwks_stub), clock, _bearer(token))).value == jt.PRINCIPAL_A


async def test_bare_app_id_audience(clock, jwks_stub, make_token) -> None:
    token = make_token(aud=jt.APP_ID)
    assert (await _run(_auth(clock, jwks_stub), clock, _bearer(token))).value == jt.PRINCIPAL_A


async def test_scheme_is_case_insensitive(clock, jwks_stub, make_token) -> None:
    principal = await _run(_auth(clock, jwks_stub), clock, "bearer " + make_token())
    assert principal.value == jt.PRINCIPAL_A


async def test_principal_from_configured_claim(clock, jwks_stub, make_token) -> None:
    token = make_token(appid=jt.PRINCIPAL_B)
    auth = _auth(clock, jwks_stub, principal_claim="appid")
    assert (await _run(auth, clock, _bearer(token))).value == jt.PRINCIPAL_B


async def test_principal_is_lowercased(clock, jwks_stub, make_token) -> None:
    token = make_token(oid=jt.PRINCIPAL_A.upper())
    assert (await _run(_auth(clock, jwks_stub), clock, _bearer(token))).value == jt.PRINCIPAL_A


async def test_extra_roles_are_fine(clock, jwks_stub, make_token) -> None:
    token = make_token(roles=["Other.Role", jt.ROLE])
    assert (await _run(_auth(clock, jwks_stub), clock, _bearer(token))).value == jt.PRINCIPAL_A


# --- token_missing / token_malformed ------------------------------------------


@pytest.mark.parametrize("header", [None, "", "   ", "Bearer", "Bearer ", "Basic abc", "Token abc.def.ghi"])
async def test_token_missing(clock, jwks_stub, header) -> None:
    assert await _reason(_auth(clock, jwks_stub), clock, header) == "token_missing"
    assert jwks_stub.calls == 0


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "a.b",
        "a.b.c.d",
        "%%%.b.c",
        "Zm9v.YmFy.YmF6",  # three base64 parts, but the header is not JSON
        "x" * 20_000,
    ],
)
async def test_token_malformed_shape(clock, jwks_stub, token) -> None:
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "token_malformed"
    assert jwks_stub.calls == 0


async def test_two_tokens_in_header_is_malformed(clock, jwks_stub, make_token) -> None:
    header = f"Bearer {make_token()} {make_token()}"
    assert await _reason(_auth(clock, jwks_stub), clock, header) == "token_malformed"


async def test_kid_missing_is_malformed(clock, jwks_stub, make_token) -> None:
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(make_token(kid=None))) == "token_malformed"


@pytest.mark.parametrize("exp", ["soon", None, True, [1]])
async def test_exp_missing_or_not_numeric_is_malformed(clock, jwks_stub, make_token, exp) -> None:
    token = make_token(drop=("exp",)) if exp is None else make_token(exp=exp)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "token_malformed"


async def test_nbf_not_numeric_is_malformed(clock, jwks_stub, make_token) -> None:
    token = make_token(nbf="now")
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "token_malformed"


async def test_nbf_absent_is_fine(clock, jwks_stub, make_token) -> None:
    token = make_token(drop=("nbf",))
    assert (await _run(_auth(clock, jwks_stub), clock, _bearer(token))).value == jt.PRINCIPAL_A


# --- alg_rejected --------------------------------------------------------------


async def test_alg_none_rejected(clock, jwks_stub) -> None:
    token = jt.unsigned_token(clock.now().timestamp())
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "alg_rejected"
    assert jwks_stub.calls == 0


async def test_hs256_with_public_key_rejected(clock, jwks_stub) -> None:
    token = jt.hs256_with_public_key(clock.now().timestamp())
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "alg_rejected"
    assert jwks_stub.calls == 0


@pytest.mark.parametrize("alg", ["RS512", "PS256", "ES256", "rs256"])
async def test_other_algorithms_rejected(clock, jwks_stub, alg) -> None:
    token = jt.unsigned_token(clock.now().timestamp(), alg=alg)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "alg_rejected"


# --- signature -----------------------------------------------------------------


async def test_signature_from_another_key(clock, jwks_stub, make_token) -> None:
    token = make_token(key="b")  # signed by key b, but claims kid-a
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "signature_invalid"


async def test_tampered_payload(clock, jwks_stub, make_token) -> None:
    header, _payload, sig = make_token().split(".")
    _h, other_payload, _s = make_token(oid=jt.PRINCIPAL_B).split(".")
    token = f"{header}.{other_payload}.{sig}"
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "signature_invalid"


# --- kid_unknown and the JWKS refresh ------------------------------------------


async def test_unknown_kid_one_refresh_then_reject(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    await _run(auth, clock, _bearer(make_token()))
    assert jwks_stub.calls == 1
    token = make_token(key="b", kid=jt.KID_B)
    assert await _reason(auth, clock, _bearer(token)) == "kid_unknown"
    assert jwks_stub.calls == 2  # exactly one refresh
    # A second unknown kid inside the refresh interval: no further fetch.
    clock.advance(UNKNOWN_KID_REFRESH_INTERVAL - 1)
    assert await _reason(auth, clock, _bearer(make_token(key="b", kid="kid-c"))) == "kid_unknown"
    assert jwks_stub.calls == 2
    # After the interval, one more refresh is allowed.
    clock.advance(1)
    assert await _reason(auth, clock, _bearer(make_token(key="b", kid="kid-c"))) == "kid_unknown"
    assert jwks_stub.calls == 3


async def test_unknown_kid_on_first_load_does_not_fetch_twice(clock, jwks_stub, make_token) -> None:
    token = make_token(key="b", kid=jt.KID_B)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "kid_unknown"
    assert jwks_stub.calls == 1


async def test_rotated_key_found_after_refresh(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    await _run(auth, clock, _bearer(make_token()))
    jwks_stub.keys = [jt.jwk("a", jt.KID_A), jt.jwk("b", jt.KID_B)]
    principal = await _run(auth, clock, _bearer(make_token(key="b", kid=jt.KID_B)))
    assert principal.value == jt.PRINCIPAL_A
    assert jwks_stub.calls == 2


async def test_cache_is_reused_then_refreshed_after_max_age(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    await _run(auth, clock, _bearer(make_token()))
    clock.advance(JWKS_MAX_AGE - 1)
    await _run(auth, clock, _bearer(make_token()))
    assert jwks_stub.calls == 1
    clock.advance(1)
    await _run(auth, clock, _bearer(make_token()))
    assert jwks_stub.calls == 2


async def test_removed_key_is_dropped_on_refresh(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    await _run(auth, clock, _bearer(make_token()))
    jwks_stub.keys = [jt.jwk("b", jt.KID_B)]
    clock.advance(JWKS_MAX_AGE)
    assert await _reason(auth, clock, _bearer(make_token())) == "kid_unknown"


async def test_stale_cache_survives_an_outage_for_known_kid(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    await _run(auth, clock, _bearer(make_token()))
    clock.advance(JWKS_MAX_AGE)
    jwks_stub.mode = "down"
    assert (await _run(auth, clock, _bearer(make_token()))).value == jt.PRINCIPAL_A


async def test_concurrent_first_requests_fetch_once(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    token = _bearer(make_token())
    results = await asyncio.gather(*(_run(auth, clock, token) for _ in range(5)))
    assert all(p.value == jt.PRINCIPAL_A for p in results)
    assert jwks_stub.calls == 1


@pytest.mark.parametrize(
    "bad_key",
    [
        {"kty": "EC", "kid": jt.KID_A},
        {"kty": "RSA", "kid": jt.KID_A, "n": "AQAB"},  # no exponent
    ],
)
async def test_unusable_jwks_entries_are_skipped(clock, make_token, bad_key) -> None:
    stub = jt.JwksStub([bad_key, jt.jwk("b", jt.KID_B)])
    assert await _reason(_auth(clock, stub), clock, _bearer(make_token())) == "kid_unknown"


async def test_encryption_key_is_not_used_for_signatures(clock, make_token) -> None:
    stub = jt.JwksStub([jt.jwk("a", jt.KID_A, use="enc")])
    with pytest.raises(JwksUnreachable):
        await _run(_auth(clock, stub), clock, _bearer(make_token()))


# --- jwks_unreachable ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["down", "timeout", "status_500", "garbage", "no_keys", "redirect"])
async def test_jwks_failure_fails_closed(clock, jwks_stub, make_token, mode) -> None:
    jwks_stub.mode = mode
    with pytest.raises(JwksUnreachable) as info:
        await _run(_auth(clock, jwks_stub), clock, _bearer(make_token()))
    assert isinstance(info.value, Unauthorized)  # still a 401
    assert info.value.diagnostic == "jwks_unreachable"
    assert info.value.reason is None


async def test_jwks_failure_backs_off(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    jwks_stub.mode = "down"
    for _ in range(3):
        with pytest.raises(JwksUnreachable):
            await _run(auth, clock, _bearer(make_token()))
    assert jwks_stub.calls == 1
    jwks_stub.mode = "ok"
    clock.advance(JWKS_RETRY_AFTER_FAILURE)
    assert (await _run(auth, clock, _bearer(make_token()))).value == jt.PRINCIPAL_A
    assert jwks_stub.calls == 2


async def test_expired_deadline_does_not_fetch(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    deadline = Deadline(clock)
    clock.advance(9)
    token = make_token()
    with pytest.raises(JwksUnreachable):
        await auth.authenticate(_bearer(token), deadline)
    assert jwks_stub.calls == 0


# --- claim checks ------------------------------------------------------------------


@pytest.mark.parametrize(
    "iss",
    [
        f"https://sts.windows.net/{jt.OTHER_TENANT_ID}/",
        f"https://login.microsoftonline.com/{jt.OTHER_TENANT_ID}/v2.0",
        f"https://sts.windows.net/{jt.TENANT_ID}",
        f"http://sts.windows.net/{jt.TENANT_ID}/",
        f"https://login.microsoftonline.com/{jt.TENANT_ID}/v2.0/",
        f"https://sts.windows.net/{jt.TENANT_ID.upper()}/",
        "",
        None,
        ["https://sts.windows.net/"],
    ],
)
async def test_issuer_mismatch(clock, jwks_stub, make_token, iss) -> None:
    token = make_token(drop=("iss",)) if iss is None else make_token(iss=iss)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "issuer_mismatch"


@pytest.mark.parametrize(
    "aud",
    [
        "api://00000000-0000-0000-0000-0000000000c3",
        "00000000-0000-0000-0000-0000000000c3",
        f"api://{jt.APP_ID}/",
        f"https://{jt.APP_ID}",
        [f"api://{jt.APP_ID}"],
        None,
    ],
)
async def test_audience_mismatch(clock, jwks_stub, make_token, aud) -> None:
    token = make_token(drop=("aud",)) if aud is None else make_token(aud=aud)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "audience_mismatch"


@pytest.mark.parametrize("tid", [jt.OTHER_TENANT_ID, "", None, 7])
async def test_tenant_mismatch(clock, jwks_stub, make_token, tid) -> None:
    token = make_token(drop=("tid",)) if tid is None else make_token(tid=tid)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "tenant_mismatch"


async def test_exp_leeway_edges(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    now = int(clock.now().timestamp())
    assert (await _run(auth, clock, _bearer(make_token(exp=now - 59)))).value == jt.PRINCIPAL_A
    assert await _reason(auth, clock, _bearer(make_token(exp=now - 60))) == "token_expired"
    assert await _reason(auth, clock, _bearer(make_token(exp=now - 3600))) == "token_expired"


async def test_nbf_leeway_edges(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    now = int(clock.now().timestamp())
    assert (await _run(auth, clock, _bearer(make_token(nbf=now + 60)))).value == jt.PRINCIPAL_A
    assert await _reason(auth, clock, _bearer(make_token(nbf=now + 61))) == "token_not_yet_valid"


async def test_token_expires_with_the_fake_clock(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub)
    token = _bearer(make_token())
    await _run(auth, clock, token)
    clock.advance(3600 + 60)
    assert await _reason(auth, clock, token) == "token_expired"


@pytest.mark.parametrize("roles", [None, [], ["Other.Role"], "Calendar.Invoke", [["Calendar.Invoke"]], ["calendar.invoke"]])
async def test_role_missing(clock, jwks_stub, make_token, roles) -> None:
    token = make_token(drop=("roles",)) if roles is None else make_token(roles=roles)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "role_missing"


async def test_configured_role_is_used(clock, jwks_stub, make_token) -> None:
    auth = _auth(clock, jwks_stub, required_role="Other.Role")
    assert await _reason(auth, clock, _bearer(make_token())) == "role_missing"
    assert (await _run(auth, clock, _bearer(make_token(roles=["Other.Role"])))).value == jt.PRINCIPAL_A


@pytest.mark.parametrize("oid", [None, "", 12, ["x"], "not-a-guid", "0" * 34])
async def test_principal_claim_missing(clock, jwks_stub, make_token, oid) -> None:
    token = make_token(drop=("oid",)) if oid is None else make_token(oid=oid)
    assert await _reason(_auth(clock, jwks_stub), clock, _bearer(token)) == "principal_claim_missing"


async def test_azp_configured_but_absent(clock, jwks_stub, make_token) -> None:
    # D-059 (b): Foundry's v1 tokens carry no azp; configuring it fails closed.
    auth = _auth(clock, jwks_stub, principal_claim="azp")
    assert await _reason(auth, clock, _bearer(make_token())) == "principal_claim_missing"


async def test_principal_known_after_signature_check(clock, jwks_stub, make_token) -> None:
    with pytest.raises(Unauthorized) as info:
        await _run(_auth(clock, jwks_stub), clock, _bearer(make_token(drop=("roles",))))
    assert info.value.reason == "role_missing"
    assert info.value.principal == jt.PRINCIPAL_A


async def test_principal_not_reported_before_signature_check(clock, jwks_stub, make_token) -> None:
    with pytest.raises(Unauthorized) as info:
        await _run(_auth(clock, jwks_stub), clock, _bearer(make_token(key="b")))
    assert info.value.reason == "signature_invalid"
    assert info.value.principal is None


# --- the raw token never leaks ---------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        {"key": "b"},
        {"iss": "https://example.invalid/"},
        {"exp": 1},
        {"roles": []},
        {"drop": ("oid",)},
    ],
)
async def test_token_never_in_exception_or_logs(
    clock, jwks_stub, make_token, caplog: pytest.LogCaptureFixture, variant
) -> None:
    caplog.set_level(logging.DEBUG)
    token = make_token(**variant)
    with pytest.raises(Unauthorized) as info:
        await _run(_auth(clock, jwks_stub), clock, _bearer(token))
    err = info.value
    for part in token.split("."):
        assert part not in str(err)
        assert part not in repr(err)
        assert part not in repr(err.args)
        assert part not in caplog.text


def test_reason_list_is_closed() -> None:
    assert UNAUTHORIZED_REASONS == frozenset(
        {
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
        }
    )


def test_unauthorized_rejects_unknown_reasons() -> None:
    with pytest.raises(ValueError):
        Unauthorized("something_else")

