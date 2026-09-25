import importlib
import logging
import sys
from types import SimpleNamespace

import pytest

BRIDGE_ENV_KEYS = [
    "ACS_CONNECTION_STRING", "ACS_DEV_TUNNEL", "ENABLE_WEB_CLIENT", "MAX_CALL_SECONDS", "MAX_CALL_DURATION",
    "VOICE_LIVE_ENDPOINT", "AZURE_VOICE_LIVE_ENDPOINT", "AGENT_ROUTING_JSON", "MEDIA_WS_TOKEN",
    "ACS_COGNITIVE_SERVICES_ENDPOINT", "ACS_CALLBACK_JWT_AUDIENCE", "INTERIM_RESPONSE_JSON",
    "VOICE_LIVE_API_VERSION", "TWILIO_AUTH_TOKEN", "INFOBIP_API_KEY", "GENESYS_API_KEY",
    "SINCH_APPLICATION_KEY", "BANDWIDTH_ACCOUNT_ID", "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID",
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


@pytest.fixture
def load_server(monkeypatch):
    def _load(**env):
        import dotenv

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
        for key in BRIDGE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AZURE_VOICE_LIVE_ENDPOINT", "https://vl.example")
        monkeypatch.setenv("AZURE_VOICE_LIVE_API_KEY", "key")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        sys.modules.pop("server", None)
        return importlib.import_module("server")

    return _load


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


@pytest.fixture
def logs():
    handler = _ListHandler()
    app_logger = logging.getLogger("app")
    old_level = app_logger.level
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.DEBUG)
    yield handler
    app_logger.removeHandler(handler)
    app_logger.setLevel(old_level)


class FakeConnection:
    def __init__(self, acs):
        self._acs = acs

    async def play_media(self, play_source, play_to="all", operation_context=None, **kwargs):
        if self._acs.play_error is not None:
            raise self._acs.play_error
        self._acs.log.append(("play", play_source.text))

    async def hang_up(self, is_for_everyone, **kwargs):
        self._acs.log.append(("hang_up", is_for_everyone))


class FakeAcs:
    def __init__(self):
        self.log = []
        self.play_error = None
        self.answer_kwargs = None
        self.answer_error = None
        self.on_answer = None
        self.connection_ids = []

    def get_call_connection(self, call_connection_id):
        self.connection_ids.append(call_connection_id)
        return FakeConnection(self)

    async def answer_call(self, **kwargs):
        self.answer_kwargs = kwargs
        if self.on_answer is not None:
            self.on_answer()
        if self.answer_error is not None:
            raise self.answer_error
        return SimpleNamespace(call_connection_id="conn-1")


@pytest.fixture
def fake_acs():
    return FakeAcs()
