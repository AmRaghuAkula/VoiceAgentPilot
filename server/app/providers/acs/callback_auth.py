"""Optional verification of the JWT that ACS Call Automation attaches to callback requests."""

import asyncio
import logging

import jwt

logger = logging.getLogger(__name__)

ACS_CALLBACK_ISSUER = "https://acscallautomation.communication.azure.com"
ACS_CALLBACK_JWKS_URL = "https://acscallautomation.communication.azure.com/calling/keys"


class CallbackJwtVerifier:
    def __init__(self, audience: str, jwks_client=None, issuer: str = ACS_CALLBACK_ISSUER):
        self._audience = audience
        self._issuer = issuer
        self._jwks = jwks_client or jwt.PyJWKClient(ACS_CALLBACK_JWKS_URL, cache_keys=True)

    async def verify(self, authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        token = authorization[len("Bearer "):].strip()
        try:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            jwt.decode(token, signing_key.key, algorithms=["RS256"], audience=self._audience, issuer=self._issuer)
            return True
        except jwt.PyJWTError as exc:
            logger.warning("callback JWT rejected: %s", type(exc).__name__)
            return False
