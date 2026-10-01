"""Managed-identity access tokens for Blob and Key Vault REST calls (plan P4)."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

STORAGE_SCOPE = "https://storage.azure.com/.default"
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"
_REFRESH_MARGIN_SECONDS = 300


class TokenUnavailable(Exception):
    """No token could be obtained in time."""


class TokenProvider(Protocol):
    async def get_token(self, scope: str, *, timeout: float) -> str: ...


class ManagedIdentityTokenProvider:
    """Wraps azure-identity's synchronous ``ManagedIdentityCredential`` (run in a thread),
    with a per-scope cache that refreshes five minutes before expiry."""

    def __init__(self, credential=None) -> None:
        if credential is None:
            from azure.identity import ManagedIdentityCredential

            credential = ManagedIdentityCredential()
        self._credential = credential
        self._cache: dict[str, tuple[str, float]] = {}

    async def get_token(self, scope: str, *, timeout: float) -> str:
        cached = self._cache.get(scope)
        if cached and cached[1] - _REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
        if timeout <= 0:
            raise TokenUnavailable("no time left")
        try:
            async with asyncio.timeout(timeout):
                access = await asyncio.to_thread(self._credential.get_token, scope)
        except Exception as err:  # noqa: BLE001 - any identity failure fails closed
            raise TokenUnavailable(type(err).__name__) from None
        self._cache[scope] = (access.token, float(access.expires_on))
        return access.token
