"""In-code Entra caller authentication (spec section 9.1, plan UC02b).

RS256 only, against the tenant's JWKS (cached 24 h; an unknown `kid` triggers
at most one refresh per 5 minutes; a failed fetch is not retried for 30 s).
Then, in this order: `iss` (the v1 or v2 form for our tenant, exactly), `aud`
(the app ID URI or the bare app ID), `tid`, `exp`/`nbf` with 60 s leeway
against the injected clock, the required app role, and the configured
principal claim. Each failure raises `Unauthorized` with its own `reason` from
a closed list (spec rev 3.2 section 10). The raw token never reaches an
exception, a log line or a `repr`.

The per-binding `allowed_principals` check is the dispatcher's (a 403, not a
401), because it needs the binding.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
import jwt

from calendar_tools.config import Config
from calendar_tools.core.clock import Clock
from calendar_tools.core.deadline import Deadline, DeadlineExceeded

JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
JWKS_TIMEOUT = 3.0  # seconds (plan section 1)
JWKS_MAX_AGE = 24 * 3600.0
UNKNOWN_KID_REFRESH_INTERVAL = 5 * 60.0
JWKS_RETRY_AFTER_FAILURE = 30.0
MAX_JWKS_BYTES = 256 * 1024
LEEWAY_SECONDS = 60
MAX_TOKEN_CHARS = 16_384
MAX_KID_CHARS = 128

UNAUTHORIZED_REASONS: frozenset[str] = frozenset(
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

_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def as_guid(value: object) -> str | None:
    """The lowercased GUID, or None. Every principal claim Entra issues (`oid`,
    `azp`, `appid`) is a GUID, so only a GUID is accepted, and only a GUID
    ever reaches the log line."""
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    return lowered if _GUID.fullmatch(lowered) else None


@dataclass(frozen=True)
class Principal:
    """The caller, as the configured claim's value (a lowercased GUID)."""

    value: str


class Unauthorized(Exception):
    """A 401. Carries only the diagnostic, a reason from `UNAUTHORIZED_REASONS`,
    and (once the signature has been verified) the principal GUID."""

    diagnostic: ClassVar[str] = "unauthorized"

    def __init__(self, reason: str | None, principal: str | None = None) -> None:
        if reason is not None and reason not in UNAUTHORIZED_REASONS:
            raise ValueError("reason must be one of UNAUTHORIZED_REASONS")
        self.reason = reason
        self.principal = as_guid(principal)
        super().__init__(self.diagnostic, reason)

    def __str__(self) -> str:
        return self.diagnostic if self.reason is None else f"{self.diagnostic}: {self.reason}"


class JwksUnreachable(Unauthorized):
    """The JWKS could not be fetched: still a 401 (fail closed), with its own
    diagnostic and no reason."""

    diagnostic: ClassVar[str] = "jwks_unreachable"

    def __init__(self) -> None:
        super().__init__(None)


class JwksCache:
    """The tenant's signing keys. All fetches are serialized by one lock, so a
    burst of first requests makes one fetch."""

    def __init__(self, tenant_id: str, http: httpx.AsyncClient, clock: Clock) -> None:
        self._url = JWKS_URL_TEMPLATE.format(tenant_id=tenant_id)
        self._http = http
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._failed_at: float | None = None
        self._kid_refresh_at: float | None = None
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return self._clock.now().timestamp()

    async def get_key(self, kid: str, deadline: Deadline) -> Any | None:
        """The verification key for `kid`, or None when the `kid` is unknown.
        Raises `JwksUnreachable` when the keys cannot be had."""
        async with self._lock:
            fetched = False
            if self._fetched_at is None:
                await self._fetch(deadline)
                fetched = True
            elif self._now() - self._fetched_at >= JWKS_MAX_AGE:
                try:
                    await self._fetch(deadline)
                    fetched = True
                except JwksUnreachable:
                    # Keep serving a known key through an outage; an unknown
                    # kid still fails closed.
                    if kid not in self._keys:
                        raise
            if kid in self._keys:
                return self._keys[kid]
            if fetched:
                return None  # the keys were fetched for this very request
            now = self._now()
            if self._kid_refresh_at is not None and now - self._kid_refresh_at < UNKNOWN_KID_REFRESH_INTERVAL:
                return None
            self._kid_refresh_at = now
            await self._fetch(deadline)
            return self._keys.get(kid)

    async def _fetch(self, deadline: Deadline) -> None:
        now = self._now()
        if self._failed_at is not None and now - self._failed_at < JWKS_RETRY_AFTER_FAILURE:
            raise JwksUnreachable()
        try:
            timeout = deadline.timeout_for(JWKS_TIMEOUT)
        except DeadlineExceeded:
            raise JwksUnreachable() from None
        try:
            async with asyncio.timeout(timeout):
                response = await self._http.get(self._url, timeout=timeout, follow_redirects=False)
            keys = self._parse(response)
        except Exception:  # noqa: BLE001 - any failed or malformed fetch fails closed
            self._failed_at = now
            raise JwksUnreachable() from None
        self._keys = keys
        self._fetched_at = now
        self._failed_at = None

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200 or len(response.content) > MAX_JWKS_BYTES:
            raise ValueError("unusable JWKS response")
        doc = json.loads(response.content)
        entries = doc.get("keys") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            raise ValueError("unusable JWKS document")
        keys: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("kty") != "RSA":
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_CHARS:
                continue
            if entry.get("use", "sig") != "sig" or entry.get("alg", "RS256") != "RS256":
                continue
            try:
                keys[kid] = jwt.PyJWK(entry, algorithm="RS256").key
            except Exception:  # noqa: BLE001 - one bad entry is skipped, not fatal
                continue
        if not keys:
            raise ValueError("no usable signing keys")
        return keys


def bearer_token(header: str | None) -> str:
    """The token from an `Authorization` header value. Raises `Unauthorized`."""
    if header is None:
        raise Unauthorized("token_missing")
    parts = header.split()
    if not parts or parts[0].lower() != "bearer" or len(parts) == 1:
        raise Unauthorized("token_missing")
    if len(parts) != 2:
        raise Unauthorized("token_malformed")
    token = parts[1]
    if len(token) > MAX_TOKEN_CHARS or not _TOKEN.fullmatch(token):
        raise Unauthorized("token_malformed")
    return token


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Authenticator:
    def __init__(self, config: Config, jwks: JwksCache, clock: Clock) -> None:
        self._config = config
        self._jwks = jwks
        self._clock = clock

    async def authenticate(self, auth_header: str | None, deadline: Deadline) -> Principal:
        token = bearer_token(auth_header)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Unauthorized("token_malformed") from None
        if header.get("alg") != "RS256":
            raise Unauthorized("alg_rejected")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_CHARS:
            raise Unauthorized("token_malformed")

        key = await self._jwks.get_key(kid, deadline)
        if key is None:
            raise Unauthorized("kid_unknown")
        claims = self._verified_claims(token, key)
        return self._check_claims(claims)

    @staticmethod
    def _verified_claims(token: str, key: Any) -> dict[str, Any]:
        """Signature only; every claim is checked by `_check_claims` against
        the injected clock, each with its own reason."""
        try:
            return jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                options={
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_sub": False,
                    "verify_jti": False,
                    "require": [],
                },
            )
        except jwt.InvalidSignatureError:
            raise Unauthorized("signature_invalid") from None
        except jwt.InvalidAlgorithmError:
            raise Unauthorized("alg_rejected") from None
        except jwt.PyJWTError:
            raise Unauthorized("token_malformed") from None

    def _check_claims(self, claims: dict[str, Any]) -> Principal:
        config = self._config
        # The signature is verified: the principal may now be logged with a refusal.
        principal = as_guid(claims.get(config.principal_claim))

        def fail(reason: str) -> Unauthorized:
            return Unauthorized(reason, principal)

        iss = claims.get("iss")
        if not isinstance(iss, str) or iss not in config.issuers:
            raise fail("issuer_mismatch")
        aud = claims.get("aud")
        if not isinstance(aud, str) or aud not in config.audiences:
            raise fail("audience_mismatch")
        tid = claims.get("tid")
        if not isinstance(tid, str) or tid != config.tenant_id:
            raise fail("tenant_mismatch")

        now = self._clock.now().timestamp()
        exp = claims.get("exp")
        if not _is_number(exp):
            raise fail("token_malformed")
        if exp <= now - LEEWAY_SECONDS:
            raise fail("token_expired")
        if "nbf" in claims:
            nbf = claims["nbf"]
            if not _is_number(nbf):
                raise fail("token_malformed")
            if nbf > now + LEEWAY_SECONDS:
                raise fail("token_not_yet_valid")

        roles = claims.get("roles")
        if not isinstance(roles, list) or config.required_role not in roles:
            raise fail("role_missing")
        if principal is None:
            raise fail("principal_claim_missing")
        return Principal(principal)
