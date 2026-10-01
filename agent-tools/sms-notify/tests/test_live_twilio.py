"""Opt-in live send (``-m live_twilio``): USMS02 only, by the founder's choice.

One real send of a fictional body with no personal data, to the configured recipient(s),
using the Key Vault secrets. Excluded by default (``addopts = -m "not live_twilio"``).
Needs ``SMS_LIVE_KEY_VAULT_URI`` and ``SMS_LIVE_FROM_NUMBER`` in the environment and an
Azure sign-in that can read the vault (``az login``).
"""

from __future__ import annotations

import os

import httpx
import pytest

from sms_notify.adapters.azure_token import ManagedIdentityTokenProvider
from sms_notify.adapters.keyvault_secrets import KeyVaultSecretSource
from sms_notify.adapters.twilio_sms import TwilioSmsNotifier
from sms_notify.core.config import RECIPIENTS_SECRET_NAME, TWILIO_SECRET_NAME, parse_recipients
from sms_notify.ports import NotifierConfig, OutboundMessage, SystemClock

pytestmark = pytest.mark.live_twilio

LIVE_BODY = "Test message from the follow-up tool.\nNo action needed."


async def test_live_send_one_fictional_message():
    vault = os.environ.get("SMS_LIVE_KEY_VAULT_URI")
    sender = os.environ.get("SMS_LIVE_FROM_NUMBER")
    if not vault or not sender:
        pytest.skip("SMS_LIVE_KEY_VAULT_URI and SMS_LIVE_FROM_NUMBER are required")
    from azure.identity import AzureCliCredential

    async with httpx.AsyncClient(follow_redirects=False) as client:
        tokens = ManagedIdentityTokenProvider(AzureCliCredential())
        secrets = KeyVaultSecretSource(client, vault, tokens, SystemClock())
        recipients = parse_recipients(
            await secrets.get_secret(RECIPIENTS_SECRET_NAME, timeout=10.0), frozenset({"CA"})
        )
        notifier = TwilioSmsNotifier(client, secrets)
        cfg = NotifierConfig("twilio_sms", {"from_number": sender}, TWILIO_SECRET_NAME, (recipients[0],))
        result = await notifier.send(cfg, OutboundMessage(None, LIVE_BODY, "live"))
    assert result.provider_message_id is not None
