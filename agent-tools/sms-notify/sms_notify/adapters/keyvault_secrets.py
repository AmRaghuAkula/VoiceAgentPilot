"""Key Vault secrets over REST with the managed identity (plan P4), cached for 5 minutes."""

from __future__ import annotations

import asyncio
import re

import httpx

from sms_notify.adapters.azure_token import KEY_VAULT_SCOPE, TokenProvider, TokenUnavailable
from sms_notify.core.errors import SecretStoreUnavailable
from sms_notify.ports import Clock

API_VERSION = "7.4"
CACHE_SECONDS = 300.0
_SECRET_NAME = re.compile(r"[0-9A-Za-z-]{1,127}")


class KeyVaultSecretSource:
    def __init__(
        self,
        client: httpx.AsyncClient,
        vault_uri: str,
        tokens: TokenProvider,
        clock: Clock,
        *,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._client = client
        self._vault = vault_uri.rstrip("/")
        self._tokens = tokens
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    async def get_secret(self, name: str, *, timeout: float) -> str:
        if not _SECRET_NAME.fullmatch(name):
            raise SecretStoreUnavailable("invalid secret name")
        cached = self._cache.get(name)
        if cached and cached[1] > self._clock.monotonic():
            return cached[0]
        if timeout <= 0:
            raise SecretStoreUnavailable("no time left")
        try:
            async with asyncio.timeout(timeout):
                token = await self._tokens.get_token(KEY_VAULT_SCOPE, timeout=timeout)
                response = await self._client.get(
                    f"{self._vault}/secrets/{name}",
                    params={"api-version": API_VERSION},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
        except (httpx.HTTPError, TokenUnavailable, TimeoutError):
            raise SecretStoreUnavailable("key vault unreachable") from None
        if response.status_code != 200:
            raise SecretStoreUnavailable(f"key vault status {response.status_code}")
        try:
            value = response.json()["value"]
        except (ValueError, KeyError, TypeError):
            raise SecretStoreUnavailable("key vault response unreadable") from None
        if not isinstance(value, str):
            raise SecretStoreUnavailable("key vault response unreadable")
        self._cache[name] = (value, self._clock.monotonic() + self._cache_seconds)
        return value
