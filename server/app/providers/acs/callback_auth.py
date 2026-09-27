"""Optional verification of the JWT that ACS Call Automation attaches to callback requests."""

import asyncio
import logging

import jwt

logger = logging.getLogger(__name__)

ACS_CALLBACK_ISSUER = "https://acscallautomation.communication.azure.com"
ACS_CALLBACK_JWKS_URL = "https://acscallautomation.communication.azure.com/calling/keys"

# The JWK set is re-fetched after this many seconds, so a rotated/revoked ACS signing key stops being
# trusted; individual keys are not cached beyond the set's lifespan (cache_keys=False).
JWKS_LIFESPAN_SECONDS = 300
# Bounds how long a JWKS fetch (e.g. triggered by an unknown `kid`) can hold a worker thread.
JWKS_FETCH_TIMEOUT_SECONDS = 5
# Allowed clock skew between ACS and this server when checking exp/nbf/iat.
CLOCK_SKEW_LEEWAY_SECONDS = 30


class CallbackJwtVerifier:
    def __init__(self, audience: str, jwks_client=None, issuer: str = ACS_CALLBACK_ISSUER):
        self._audience = audience
        self._issuer = issuer
        self._jwks = jwks_client or jwt.PyJWKClient(
            ACS_CALLBACK_JWKS_URL,
            cache_keys=False,
            lifespan=JWKS_LIFESPAN_SECONDS,
            timeout=JWKS_FETCH_TIMEOUT_SECONDS,
        )

    async def verify(self, authorization: str | None) -> bool:
        """True only for a well-formed, correctly signed, unexpired token; never raises (fails closed)."""
        if not authorization or not authorization.startswith("Bearer "):
            return False
        token = authorization[len("Bearer "):].strip()
        try:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
            return True
        except jwt.PyJWTError as exc:
            logger.warning("callback JWT rejected: %s", type(exc).__name__)
            return False
        except Exception as exc:  # e.g. a JWKS network/parse error: reject, never 500
            logger.warning("callback JWT check failed: %s", type(exc).__name__)
            return False
