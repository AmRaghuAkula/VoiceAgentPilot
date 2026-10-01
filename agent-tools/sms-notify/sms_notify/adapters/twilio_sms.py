"""TwilioSmsNotifier: the ``Notifier`` shape (plan P1) over raw HTTPS (spec K7, K8).

One hard-coded host, no SDK, no status callback, no retry. Basic auth with a Standard API key.
"""

from __future__ import annotations

import asyncio
import re
from typing import ClassVar

import httpx

from sms_notify.core.config import parse_twilio_credentials
from sms_notify.core.deadline import TWILIO_POST_BUDGET, budget_for
from sms_notify.core.errors import (
    ConfigError,
    NotifierAuthError,
    NotifierConfigError,
    NotifierRejected,
    NotifierUnavailable,
)
from sms_notify.core.phone import is_e164
from sms_notify.ports import (
    NotifierCapabilities,
    NotifierConfig,
    OutboundMessage,
    SecretSource,
    SendResult,
)

TWILIO_HOST = "api.twilio.com"
MAX_BODY_CHARS = 480
SECRET_TIMEOUT_SECONDS = 1.5
# Backstop beyond httpx's per-phase timeouts, so a connect timeout surfaces as ConnectTimeout.
_OUTER_GRACE_SECONDS = 0.05

# Failures where the request can't have reached Twilio.
_NOT_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


class TwilioSmsNotifier:
    name: ClassVar[str] = "twilio_sms"

    def __init__(self, client: httpx.AsyncClient, secrets: SecretSource) -> None:
        self._client = client
        self._secrets = secrets

    def capabilities(self, cfg: NotifierConfig) -> NotifierCapabilities:
        return NotifierCapabilities(
            formats=frozenset({"text_short"}),
            max_body_chars=MAX_BODY_CHARS,
            native_idempotency=False,
            leaves_boundary=True,
            residency="global",
        )

    def validate_config(self, cfg: NotifierConfig) -> None:
        if cfg.notifier != self.name:
            raise NotifierConfigError("config is for another notifier")
        if len(cfg.recipients) != 1:
            raise NotifierConfigError("exactly one recipient per config (plan P6)")
        if not all(is_e164(r) for r in cfg.recipients):
            raise NotifierConfigError("recipient is not E.164")
        if not is_e164(cfg.channel_config.get("from_number")):
            raise NotifierConfigError("from_number is not E.164")
        if not cfg.credential_secret_name:
            raise NotifierConfigError("credential secret name is required")

    async def _credentials(self, cfg: NotifierConfig) -> tuple[str, str, str]:
        raw = await self._secrets.get_secret(
            cfg.credential_secret_name or "", timeout=budget_for(SECRET_TIMEOUT_SECONDS)
        )
        try:
            return parse_twilio_credentials(raw)
        except ConfigError:
            raise NotifierConfigError("credential secret is invalid") from None

    async def send(self, cfg: NotifierConfig, message: OutboundMessage) -> SendResult:
        self.validate_config(cfg)
        if len(message.text_body) > MAX_BODY_CHARS or not message.text_body:
            raise NotifierRejected(status=None, error_code=None)
        account_sid, key_sid, key_secret = await self._credentials(cfg)

        budget = budget_for(TWILIO_POST_BUDGET)
        if budget <= 0:
            raise NotifierUnavailable(maybe_sent=False, reason="no time left")
        url = f"https://{TWILIO_HOST}/2010-04-01/Accounts/{account_sid}/Messages.json"
        form = {
            "To": cfg.recipients[0],
            "From": str(cfg.channel_config["from_number"]),
            "Body": message.text_body,
        }
        try:
            async with asyncio.timeout(budget + _OUTER_GRACE_SECONDS):
                response = await self._client.post(
                    url,
                    data=form,
                    auth=(key_sid, key_secret),
                    timeout=httpx.Timeout(budget, connect=min(budget, 1.0), pool=min(budget, 0.5)),
                    follow_redirects=False,
                )
        except _NOT_SENT:
            raise NotifierUnavailable(maybe_sent=False, reason="connection failed") from None
        except (httpx.HTTPError, TimeoutError):
            # Timeout or dropped connection after the request may have gone out. Never retried.
            raise NotifierUnavailable(maybe_sent=True, reason="no confirmation") from None

        status = response.status_code
        if 200 <= status < 300:
            return SendResult(provider_message_id=_sid(response))
        error_code = _error_code(response)
        if status in (401, 403):
            raise NotifierAuthError(error_code=error_code)
        raise NotifierRejected(status=status, error_code=error_code)


def _sid(response: httpx.Response) -> str | None:
    try:
        sid = response.json().get("sid")
    except (ValueError, AttributeError):
        return None
    return sid if isinstance(sid, str) and re.fullmatch(r"[A-Z]{2}[0-9a-fA-F]{32}", sid) else None


def _error_code(response: httpx.Response) -> int | None:
    """Only Twilio's numeric error code is kept; the body is never logged or returned."""
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, int) and not isinstance(code, bool) else None
