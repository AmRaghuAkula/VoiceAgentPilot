import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.providers.acs.callback_auth import ACS_CALLBACK_ISSUER, CallbackJwtVerifier

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def token(key=KEY, **claims):
    body = {"aud": "acs-id", "iss": ACS_CALLBACK_ISSUER, "exp": int(time.time()) + 60}
    body.update(claims)
    return jwt.encode(body, key, algorithm="RS256")


@pytest.fixture
def verifier():
    return CallbackJwtVerifier("acs-id", jwks_client=FakeJwks())


async def test_valid_token(verifier):
    assert await verifier.verify(f"Bearer {token()}") is True


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic abc", "Bearer not-a-jwt"],
)
async def test_malformed_headers_rejected(verifier, header):
    assert await verifier.verify(header) is False


@pytest.mark.parametrize(
    # exp is well past the clock-skew leeway (CLOCK_SKEW_LEEWAY_SECONDS)
    "claims", [{"aud": "other"}, {"iss": "https://evil.example"}, {"exp": int(time.time()) - 120}]
)
async def test_bad_claims_rejected(verifier, claims):
    assert await verifier.verify(f"Bearer {token(**claims)}") is False


async def test_wrong_signing_key_rejected(verifier):
    assert await verifier.verify(f"Bearer {token(key=OTHER_KEY)}") is False


@pytest.mark.parametrize("claim", ["exp", "iss", "aud"])
async def test_token_missing_required_claim_rejected(claim):
    body = {"aud": "acs-id", "iss": ACS_CALLBACK_ISSUER, "exp": int(time.time()) + 60}
    del body[claim]
    raw = jwt.encode(body, KEY, algorithm="RS256")
    # All three must be present, not just valid if present (a token with no exp would never expire).
    verifier = CallbackJwtVerifier("acs-id", jwks_client=FakeJwks())
    assert await verifier.verify(f"Bearer {raw}") is False


async def test_small_clock_skew_tolerated(verifier):
    assert await verifier.verify(f"Bearer {token(exp=int(time.time()) - 10)}") is True


class RaisingJwks:
    def __init__(self, exc):
        self._exc = exc

    def get_signing_key_from_jwt(self, token):
        raise self._exc


@pytest.mark.parametrize("exc", [ValueError("bad jwk"), ConnectionResetError(), TypeError("x")])
async def test_non_jwt_errors_fail_closed(exc, logs):
    verifier = CallbackJwtVerifier("acs-id", jwks_client=RaisingJwks(exc))
    assert await verifier.verify(f"Bearer {token()}") is False
    assert type(exc).__name__ in logs.text


def test_default_jwks_client_does_not_cache_keys_forever_and_has_short_timeout():
    verifier = CallbackJwtVerifier("acs-id")
    client = verifier._jwks
    assert isinstance(client, jwt.PyJWKClient)
    assert client.timeout == 5
    assert client.jwk_set_cache.lifespan == 300
    assert not hasattr(client.get_signing_key, "cache_info")  # cache_keys=False: no per-kid lru_cache
