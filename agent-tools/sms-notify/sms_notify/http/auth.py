"""Entra token validation in code (spec K6, section 5 step 1).

RS256 against the tenant's JWKS (cached; refreshed on an unknown ``kid`` at most once per
60 s), ``iss`` (v1 and v2 forms for our ``tid``), ``aud`` (app ID URI or bare app ID),
``tid``, ``exp``/``nbf`` with 60 s leeway, the ``oid`` allowlist, and ``roles`` only when
``SMS_REQUIRE_ROLE=true``.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass

import httpx
import jwt

from sms_notify.core.config import Settings
from sms_notify.core.deadline import JWKS_BUDGET, Deadline
from sms_notify.ports import Clock

LEEWAY_SECONDS = 60
UNKNOWN_KID_REFRESH_SECONDS = 60.0
JWKS_MAX_AGE_SECONDS = 24 * 3600.0
REQUIRED_ROLE = "Sms.Send"
MAX_TOKEN_CHARS = 16_384


class AuthResult(enum.Enum):
    OK = "ok"
    UNAUTHORIZED = "unauthorized"  # 401, empty body
    FORBIDDEN = "forbidden"  # 200 forbidden
    UNAVAILABLE = "unavailable"  # 200 unavailable (JWKS unreachable)


@dataclass(frozen=True)
class AuthOutcome:
    result: AuthResult
    oid: str | None = None


class JwksUnavailable(Exception):
    pass


class JwksCache:
    def __init__(self, client: httpx.AsyncClient, tenant_id: str, clock: Clock) -> None:
        self._client = client
        self._url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        self._clock = clock
        self._keys: dict[str, object] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._last_failed = False
        self._lock = asyncio.Lock()

    async def _fetch(self, deadline: Deadline) -> None:
        timeout = deadline.budget(JWKS_BUDGET)
        if timeout <= 0:
            raise JwksUnavailable("no time left")
        self._attempted_at = self._clock.monotonic()
        try:
            async with asyncio.timeout(timeout):
                response = await self._client.get(self._url, timeout=timeout, follow_redirects=False)
            if response.status_code != 200:
                raise JwksUnavailable(f"jwks status {response.status_code}")
            keyset = jwt.PyJWKSet.from_dict(response.json())
        except JwksUnavailable:
            raise
        except Exception as err:  # noqa: BLE001 - any malformed or failed fetch fails closed
            raise JwksUnavailable(type(err).__name__) from None
        self._keys = {k.key_id: k.key for k in keyset.keys if k.key_id and k.key_type == "RSA"}
        self._fetched_at = self._clock.monotonic()

    def _attempted_recently(self) -> bool:
        return (
            self._attempted_at is not None
            and self._clock.monotonic() - self._attempted_at < UNKNOWN_KID_REFRESH_SECONDS
        )

    async def get_key(self, kid: str, deadline: Deadline) -> object | None:
        """The signing key for ``kid``; None if unknown. Raises ``JwksUnavailable``.

        Every outbound fetch after the first is throttled to once per 60 s, measured from
        the last attempt (successful or not), so a JWKS outage can't be amplified.
        """
        async with self._lock:
            if self._fetched_at is None:
                if self._attempted_recently() and self._last_failed:
                    # The first load failed less than 60 s ago: answer unavailable, no fetch.
                    raise JwksUnavailable("recent fetch failed")
                await self._fetch_tracked(deadline)
            elif (
                self._clock.monotonic() - self._fetched_at >= JWKS_MAX_AGE_SECONDS
                and not self._attempted_recently()
            ):
                try:
                    await self._fetch_tracked(deadline)
                except JwksUnavailable:
                    if kid not in self._keys:
                        raise
            if kid in self._keys:
                return self._keys[kid]
            if self._attempted_recently():
                return None  # refreshed too recently: no fetch, the token is rejected
            await self._fetch_tracked(deadline)
            return self._keys.get(kid)

    async def _fetch_tracked(self, deadline: Deadline) -> None:
        self._last_failed = True
        await self._fetch(deadline)
        self._last_failed = False


def bearer_token(headers: dict[str, str]) -> str | None:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    return token


async def authenticate(
    headers: dict[str, str], settings: Settings, jwks: JwksCache, deadline: Deadline
) -> AuthOutcome:
    token = bearer_token(headers)
    if token is None:
        return AuthOutcome(AuthResult.UNAUTHORIZED)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return AuthOutcome(AuthResult.UNAUTHORIZED)
    kid = header.get("kid")
    if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        return AuthOutcome(AuthResult.UNAUTHORIZED)
    try:
        key = await jwks.get_key(kid, deadline)
    except JwksUnavailable:
        return AuthOutcome(AuthResult.UNAVAILABLE)
    if key is None:
        return AuthOutcome(AuthResult.UNAUTHORIZED)

    tid = settings.auth_tenant_id
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=list(settings.audiences),
            issuer=[f"https://sts.windows.net/{tid}/", f"https://login.microsoftonline.com/{tid}/v2.0"],
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "tid", "oid"]},
        )
    except jwt.PyJWTError:
        return AuthOutcome(AuthResult.UNAUTHORIZED)
    if str(claims.get("tid", "")).lower() != tid:
        return AuthOutcome(AuthResult.UNAUTHORIZED)

    oid = str(claims.get("oid", "")).lower()
    if oid not in settings.allowed_principals:
        return AuthOutcome(AuthResult.FORBIDDEN, oid)
    if settings.require_role:
        roles = claims.get("roles")
        if not isinstance(roles, list) or REQUIRED_ROLE not in roles:
            return AuthOutcome(AuthResult.FORBIDDEN, oid)
    return AuthOutcome(AuthResult.OK, oid)
