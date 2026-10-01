"""Wires settings and adapters into a ``Runtime`` once per process (used by function_app.py).

Settings are re-read on every request (cheap), so a configuration error answers 200
``unavailable`` instead of crashing the host. Adapters are built once per distinct config.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx

from sms_notify.adapters.azure_token import ManagedIdentityTokenProvider, TokenProvider
from sms_notify.adapters.blob_state import BlobStateStore
from sms_notify.adapters.keyvault_secrets import KeyVaultSecretSource
from sms_notify.adapters.twilio_sms import TwilioSmsNotifier
from sms_notify.core.config import load_settings
from sms_notify.core.service import CoreDeps
from sms_notify.http.auth import JwksCache
from sms_notify.http.dispatcher import Runtime
from sms_notify.ports import Clock


class RuntimeFactory:
    def __init__(
        self,
        env: Callable[[], Mapping[str, str]],
        clock: Clock,
        *,
        client: httpx.AsyncClient | None = None,
        tokens: TokenProvider | None = None,
    ) -> None:
        self._env = env
        self._clock = clock
        self._client = client
        self._tokens = tokens
        self._key: tuple[str, str, str] | None = None
        self._parts: tuple[JwksCache, CoreDeps] | None = None

    def __call__(self) -> Runtime:
        settings = load_settings(self._env())  # raises ConfigError
        key = (settings.auth_tenant_id, settings.key_vault_uri, settings.state_blob_url)
        if self._parts is None or key != self._key:
            if self._client is None:
                self._client = httpx.AsyncClient(follow_redirects=False)
            if self._tokens is None:
                self._tokens = ManagedIdentityTokenProvider()
            secrets = KeyVaultSecretSource(self._client, settings.key_vault_uri, self._tokens, self._clock)
            deps = CoreDeps(
                clock=self._clock,
                secrets=secrets,
                state=BlobStateStore(self._client, settings.state_blob_url, self._tokens),
                notifier=TwilioSmsNotifier(self._client, secrets),
            )
            self._parts = (JwksCache(self._client, settings.auth_tenant_id, self._clock), deps)
            self._key = key
        jwks, deps = self._parts
        return Runtime(settings=settings, jwks=jwks, deps=deps)
