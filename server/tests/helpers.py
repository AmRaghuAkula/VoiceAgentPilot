"""Shared test constants and helpers, kept out of conftest.py.

Importing this module directly (e.g. `from tests.helpers import acs_env`) is safe under
--import-mode=importlib. Importing tests/conftest.py itself as a plain module is not: pytest
already loads conftest.py as a plugin, and a second import would create a second, divergent
copy of any module-level state it holds.
"""

BRIDGE_ENV_KEYS = [
    "ACS_CONNECTION_STRING", "ACS_DEV_TUNNEL", "ENABLE_WEB_CLIENT", "MAX_CALL_SECONDS", "MAX_CALL_DURATION",
    "VOICE_LIVE_ENDPOINT", "AZURE_VOICE_LIVE_ENDPOINT", "AGENT_ROUTING_JSON", "MEDIA_WS_TOKEN",
    "ACS_COGNITIVE_SERVICES_ENDPOINT", "ACS_CALLBACK_JWT_AUDIENCE", "INTERIM_RESPONSE_JSON",
    "VOICE_LIVE_API_VERSION", "TWILIO_AUTH_TOKEN", "INFOBIP_API_KEY", "GENESYS_API_KEY",
    "SINCH_APPLICATION_KEY", "BANDWIDTH_ACCOUNT_ID", "BANDWIDTH_CLIENT_ID",
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID",
    "MAX_CONCURRENT_CALLS", "CALL_IDLE_TIMEOUT", "DEBUG_MODE", "AMBIENT_PRESET", "VOICE_LIVE_MODEL",
]

VALID_ROUTING = '{"+14165551234": {"project": "proj", "agent": "agent-a", "version": "10"}}'
TOKEN = "t" * 40


def acs_env(**overrides):
    env = {
        "ACS_CONNECTION_STRING": "endpoint=https://fake.communication.azure.com/;accesskey=ZmFrZQ==",
        "AGENT_ROUTING_JSON": VALID_ROUTING,
        "MEDIA_WS_TOKEN": TOKEN,
        "ACS_COGNITIVE_SERVICES_ENDPOINT": "https://cog.example",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}
