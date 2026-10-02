"""Locally generated Entra-style tokens and a stub JWKS endpoint (UC02b).

Nothing here touches the network: the JWKS endpoint is a `respx` route served
through `httpx.MockTransport`, and every key is generated in-process.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import httpx
import jwt
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from calendar_tools.config import Config

TENANT_ID = "00000000-0000-0000-0000-0000000000c1"
OTHER_TENANT_ID = "00000000-0000-0000-0000-0000000000c9"
APP_ID = "00000000-0000-0000-0000-0000000000c2"
PRINCIPAL_A = "00000000-0000-0000-0000-0000000000a1"
PRINCIPAL_B = "00000000-0000-0000-0000-0000000000b1"
ROLE = "Calendar.Invoke"
ISS_V1 = f"https://sts.windows.net/{TENANT_ID}/"
ISS_V2 = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
KID_A = "kid-a"
KID_B = "kid-b"

_KEYS: dict[str, rsa.RSAPrivateKey] = {}


def private_key(name: str = "a") -> rsa.RSAPrivateKey:
    """One 2048-bit key per name, generated once per test session."""
    if name not in _KEYS:
        _KEYS[name] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEYS[name]


def public_pem(name: str = "a") -> bytes:
    return private_key(name).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def jwk(name: str = "a", kid: str = KID_A, **extra: Any) -> dict[str, Any]:
    data = jwt.algorithms.RSAAlgorithm.to_jwk(private_key(name).public_key(), as_dict=True)
    data.update({"kid": kid, "use": "sig"})
    data.update(extra)
    return data


def make_config(**overrides: str) -> Config:
    values = {
        "tenant_id": TENANT_ID,
        "app_id": APP_ID,
        "required_role": ROLE,
        "principal_claim": "oid",
    }
    values.update(overrides)
    return Config(**values)


def default_claims(now: float) -> dict[str, Any]:
    return {
        "aud": f"api://{APP_ID}",
        "iss": ISS_V1,
        "tid": TENANT_ID,
        "oid": PRINCIPAL_A,
        "appid": PRINCIPAL_A,
        "roles": [ROLE],
        "iat": int(now),
        "nbf": int(now),
        "exp": int(now) + 3600,
    }


def make_token(
    now: float, *, key: str = "a", kid: str | None = KID_A, drop: tuple[str, ...] = (), **claims: Any
) -> str:
    """An RS256 token valid at `now` (epoch seconds) unless `claims` say otherwise.
    Claims named in `drop` are removed."""
    payload = default_claims(now)
    payload.update(claims)
    for name in drop:
        payload.pop(name, None)
    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    # Signed by hand, not with jwt.encode, so tests can plant claims PyJWT's
    # encoder refuses (for example a non-string `iss`).
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = private_key(key).sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unsigned_token(now: float, alg: str = "none", kid: str = KID_A) -> str:
    header = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(default_claims(now)).encode())
    return f"{header}.{payload}."


def hs256_with_public_key(now: float, kid: str = KID_A) -> str:
    """The classic algorithm-confusion token: HS256 keyed with the RSA public key."""
    header = _b64(json.dumps({"alg": "HS256", "kid": kid, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(default_claims(now)).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(public_pem("a"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


class JwksStub:
    """The tenant's JWKS endpoint. `mode`: `ok`, `down`, `status_500`, `garbage`,
    `no_keys`, `timeout`, `redirect`."""

    def __init__(self, keys: list[dict[str, Any]] | None = None) -> None:
        self.keys = keys if keys is not None else [jwk("a", KID_A)]
        self.mode = "ok"
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "down":
            raise httpx.ConnectError("down", request=request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if self.mode == "status_500":
            return httpx.Response(500, json={"keys": self.keys})
        if self.mode == "garbage":
            return httpx.Response(200, content=b"{not json")
        if self.mode == "no_keys":
            return httpx.Response(200, json={"keys": []})
        if self.mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.invalid/keys"})
        return httpx.Response(200, json={"keys": self.keys})


def jwks_client(stub: JwksStub) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` whose only reachable URL is the stub JWKS route."""
    router = respx.MockRouter(assert_all_called=False, assert_all_mocked=True)
    router.get(JWKS_URL).mock(side_effect=stub)
    return httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler))
