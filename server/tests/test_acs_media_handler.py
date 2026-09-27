import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handler.voicelive_media_handler as vmh
from app.providers.acs.call_session import CallSession, CallSessionRegistry, SessionSettings
from app.providers.acs.media_handler import ACSMediaHandler
from app.routing import AgentRoute
from tests.test_voicelive_handler import handler_config

SETTINGS = SessionSettings(fallback_message="fb", goodbye_message="bye", tts_voice="v", voice_live_connect_timeout=0.05)


class FakeSession:
    def __init__(self):
        self.route = AgentRoute("proj", "agent-a", "10")
        self.settings = SETTINGS
        self.terminated_reason = None
        self.log_context = "call_key=abc conn=conn-1"
        self.ends = []
        self.closes = 0

    def request_end(self, reason, message):
        self.ends.append((reason, message))
        self.terminated_reason = self.terminated_reason or reason

    def ensure_voicelive_closed(self):
        self.closes += 1


@pytest.fixture
def session():
    return FakeSession()


def patch_base_connect(monkeypatch, coro):
    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", coro)


async def test_uses_session_route(session):
    handler = ACSMediaHandler(handler_config(), session=session)
    assert handler.route == session.route
    assert handler.log_context == session.log_context


async def test_connect_failure_requests_fallback_and_reraises(monkeypatch, session):
    async def boom(self):
        raise RuntimeError("bad agent version")

    patch_base_connect(monkeypatch, boom)
    handler = ACSMediaHandler(handler_config(), session=session)
    with pytest.raises(RuntimeError):
        await handler.connect_voicelive()
    assert session.ends == [("voicelive_connect_failed", "fb")]


async def test_connect_timeout_requests_fallback(monkeypatch, session):
    async def hang(self):
        await asyncio.sleep(10)

    patch_base_connect(monkeypatch, hang)
    handler = ACSMediaHandler(handler_config(), session=session)
    with pytest.raises(TimeoutError):
        await handler.connect_voicelive()
    assert session.ends == [("voicelive_connect_failed", "fb")]


async def test_connect_completing_after_termination_closes_voicelive(monkeypatch, session):
    async def slow_ok(self):
        session.terminated_reason = "caller_hangup"

    patch_base_connect(monkeypatch, slow_ok)
    handler = ACSMediaHandler(handler_config(), session=session)
    await handler.connect_voicelive()
    assert session.closes == 1


@pytest.mark.parametrize(
    "hook, expected",
    [("on_voicelive_ended", ("voicelive_dropped", "fb")), ("on_call_cap", ("call_cap", "bye")), ("on_idle", ("idle", "fb"))],
)
async def test_hooks_route_to_request_end(session, hook, expected):
    handler = ACSMediaHandler(handler_config(), session=session)
    await getattr(handler, hook)()
    assert session.ends == [expected]


async def test_stop_forwarding_sends_acs_stop_audio(session):
    handler = ACSMediaHandler(handler_config(), session=session)
    ws = SimpleNamespace(send=AsyncMock())
    await handler.init_websocket(ws)
    handler.stop_forwarding_agent_audio()
    await asyncio.sleep(0)
    sent = json.loads(ws.send.call_args.args[0])
    assert sent["Kind"] == "StopAudio"
    assert handler._forward_agent_audio is False


async def test_without_session_behaves_like_upstream(monkeypatch):
    calls = []

    async def ok(self):
        calls.append("connected")

    patch_base_connect(monkeypatch, ok)
    handler = ACSMediaHandler(handler_config())
    await handler.connect_voicelive()
    assert calls == ["connected"]
    assert handler.route is None


class _HangingConnection:
    async def play_media(self, **kwargs):
        await asyncio.sleep(10)

    async def hang_up(self, **kwargs):
        await asyncio.sleep(10)


class _HangingAcs:
    def get_call_connection(self, call_connection_id):
        return _HangingConnection()


@pytest.mark.parametrize("hook", ["on_call_cap", "on_idle"])
async def test_cap_and_idle_hooks_do_not_block_on_acs_io(hook):
    """Q-008: run_call_loop awaits these hooks with no timeout, so they must not await ACS I/O.

    A real CallSession whose ACS play/hang-up never return must still let the hook return at once;
    the goodbye/fallback play runs in a task the session owns.
    """
    registry = CallSessionRegistry()
    real = CallSession(
        call_key="k" * 32, route=AgentRoute("proj", "agent-a", "10"), masked_caller="***9876",
        masked_called="***1234", settings=SETTINGS, acs_client=_HangingAcs(), registry=registry,
    )
    registry.add(real)
    real.set_answered("conn-1")
    real.mark_connected()
    handler = ACSMediaHandler(handler_config(), session=real)
    await asyncio.wait_for(getattr(handler, hook)(), 0.5)
    assert real.terminated_reason == ("call_cap" if hook == "on_call_cap" else "idle")
    for task in list(real._tasks):
        task.cancel()
    await asyncio.gather(*real._tasks, return_exceptions=True)
