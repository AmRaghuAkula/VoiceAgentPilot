import importlib
import logging
import sys
from types import SimpleNamespace

import pytest

from tests.helpers import BRIDGE_ENV_KEYS, TOKEN, VALID_ROUTING, acs_env

__all__ = ["BRIDGE_ENV_KEYS", "TOKEN", "VALID_ROUTING", "acs_env", "load_server", "logs", "fake_acs"]


@pytest.fixture
def load_server(monkeypatch):
    """Import server/server.py fresh, with a controlled environment.

    Note: importing server.py runs upstream's configure_logging(), which clears the root
    logger's handlers — this also removes pytest's built-in caplog handler for the rest of
    the test. Use the `logs` fixture (below) to capture log output after calling this, not
    caplog, since `logs` attaches to named loggers rather than the root logger.
    """

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
    loggers = [logging.getLogger("app"), logging.getLogger("server")]
    old_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    yield handler
    for logger, old_level in zip(loggers, old_levels):
        logger.removeHandler(handler)
        logger.setLevel(old_level)


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
