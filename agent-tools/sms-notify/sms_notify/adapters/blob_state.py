"""The ``StateStore`` port on Azure Blob Storage (REST, managed identity).

``create`` uses ``If-None-Match: *``; ``replace`` uses ``If-Match: <etag>``.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from sms_notify.adapters.azure_token import STORAGE_SCOPE, TokenProvider, TokenUnavailable
from sms_notify.core.errors import StateStoreUnavailable
from sms_notify.ports import StoredItem

STORAGE_API_VERSION = "2023-11-03"
_KEY = re.compile(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)*")


class BlobStateStore:
    def __init__(self, client: httpx.AsyncClient, container_url: str, tokens: TokenProvider) -> None:
        self._client = client
        self._container = container_url.rstrip("/")
        self._tokens = tokens

    async def _request(
        self, method: str, key: str, timeout: float, headers: dict[str, str], body: bytes | None = None
    ) -> httpx.Response:
        if not _KEY.fullmatch(key):
            raise StateStoreUnavailable("invalid key")
        if timeout <= 0:
            raise StateStoreUnavailable("no time left")
        try:
            async with asyncio.timeout(timeout):
                token = await self._tokens.get_token(STORAGE_SCOPE, timeout=timeout)
                return await self._client.request(
                    method,
                    f"{self._container}/{key}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "x-ms-version": STORAGE_API_VERSION,
                        **headers,
                    },
                    content=body,
                    timeout=timeout,
                )
        except (httpx.HTTPError, TokenUnavailable, TimeoutError):
            raise StateStoreUnavailable("storage unreachable") from None

    async def get(self, key: str, *, timeout: float) -> StoredItem | None:
        response = await self._request("GET", key, timeout, {})
        if response.status_code == 404:
            return None
        if response.status_code != 200 or not response.headers.get("etag"):
            raise StateStoreUnavailable(f"storage status {response.status_code}")
        return StoredItem(body=response.content, etag=response.headers["etag"])

    async def _put(
        self, key: str, body: bytes, timeout: float, condition: dict[str, str], lost: tuple[int, ...]
    ) -> str | None:
        response = await self._request(
            "PUT",
            key,
            timeout,
            {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json", **condition},
            body,
        )
        if response.status_code == 201 and response.headers.get("etag"):
            return response.headers["etag"]
        if response.status_code in lost:
            return None
        raise StateStoreUnavailable(f"storage status {response.status_code}")

    async def create(self, key: str, body: bytes, *, timeout: float) -> str | None:
        # 409 BlobAlreadyExists or 412 ConditionNotMet: the blob exists.
        return await self._put(key, body, timeout, {"If-None-Match": "*"}, (409, 412))

    async def replace(self, key: str, body: bytes, etag: str, *, timeout: float) -> str | None:
        # 412: the etag changed (lost race); 404: the blob was deleted meanwhile.
        return await self._put(key, body, timeout, {"If-Match": etag}, (404, 412))
