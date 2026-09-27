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
    "claims", [{"aud": "other"}, {"iss": "https://evil.example"}, {"exp": int(time.time()) - 10}]
)
async def test_bad_claims_rejected(verifier, claims):
    assert await verifier.verify(f"Bearer {token(**claims)}") is False


async def test_wrong_signing_key_rejected(verifier):
    assert await verifier.verify(f"Bearer {token(key=OTHER_KEY)}") is False
