import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handler.voicelive_media_handler as vmh
from app.routing import AgentRoute

ROUTE = AgentRoute("proj", "agent-a", "10")


def handler_config(**overrides):
    cfg = {
        "AZURE_VOICE_LIVE_ENDPOINT": "https://vl.example",
        "VOICE_LIVE_MODEL": "gpt-4o-mini",
        "AZURE_VOICE_LIVE_API_KEY": "key",
        "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID": "cid",
        "AMBIENT_PRESET": "none",
    }
    cfg.update(overrides)
    return cfg


class FakeConn:
    def __init__(self, events=None, block=True):
        self.session = SimpleNamespace(update=AsyncMock())
        self.response = SimpleNamespace(create=AsyncMock())
        self._events = list(events or [])
        self._block = block

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        if self._block:
            await asyncio.sleep(3600)
        raise StopAsyncIteration


class FakeCtx:
    def __init__(self, conn):
        self.conn = conn
        self.exited = False

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        self.exited = True


class FakeCredential:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def close(self):
        pass


@pytest.fixture
def fake_sdk(monkeypatch):
    captured = {}
    conn = FakeConn()

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeCtx(captured.setdefault("_conn", conn))

    monkeypatch.setattr(vmh, "voicelive_connect", fake_connect)
    monkeypatch.setattr(vmh, "DefaultAzureCredential", FakeCredential)
    return captured


async def test_agent_mode_connects_with_pinned_route(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=ROUTE)
    await handler.connect_voicelive()
    assert fake_sdk["agent_name"] == "agent-a"
    assert fake_sdk["project_name"] == "proj"
    assert fake_sdk["agent_version"] == "10"
    assert "model" not in fake_sdk
    assert isinstance(fake_sdk["credential"], FakeCredential)
    assert fake_sdk["credential"].kwargs == {"managed_identity_client_id": "cid"}
    await handler.cleanup()


async def test_agent_mode_session_update_has_no_behavior_fields(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=ROUTE)
    await handler.connect_voicelive()
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    for forbidden in ("instructions", "voice", "turn_detection", "input_audio_noise_reduction",
                      "input_audio_echo_cancellation", "interim_response"):
        assert forbidden not in sent
    assert sent["input_audio_format"] == "pcm16"
    assert sent["output_audio_format"] == "pcm16"
    fake_sdk["_conn"].response.create.assert_awaited_once()
    await handler.cleanup()


async def test_interim_response_only_when_configured(fake_sdk):
    interim = {"type": "llm_interim_response"}
    handler = vmh.VoiceLiveMediaHandler(handler_config(INTERIM_RESPONSE=interim), route=ROUTE)
    await handler.connect_voicelive()
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    assert sent["interim_response"] == interim
    await handler.cleanup()


async def test_api_version_passed_when_set(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(VOICE_LIVE_API_VERSION="2026-07-15"), route=ROUTE)
    await handler.connect_voicelive()
    assert fake_sdk["api_version"] == "2026-07-15"
    await handler.cleanup()


async def test_model_mode_unchanged_without_route(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID=""))
    await handler.connect_voicelive()
    assert fake_sdk["model"] == "gpt-4o-mini"
    assert "agent_name" not in fake_sdk
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    assert "instructions" in sent
    await handler.cleanup()


async def test_stop_forwarding_drops_agent_audio():
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    handler._send_audio_to_client = AsyncMock()
    handler.stop_forwarding_agent_audio()
    await handler.on_audio_delta(b"\x00\x00")
    handler._send_audio_to_client.assert_not_called()


class RecordingHandler(vmh.VoiceLiveMediaHandler):
    ended = 0

    async def on_voicelive_ended(self):
        self.ended += 1


async def test_ended_hook_not_called_on_intentional_cleanup():
    handler = RecordingHandler(handler_config())
    handler.conn = FakeConn(block=True)
    handler._receiver_task = asyncio.create_task(handler._receiver_loop())
    await asyncio.sleep(0)
    await handler.cleanup()
    assert handler.ended == 0


async def test_ended_hook_called_when_voicelive_drops():
    handler = RecordingHandler(handler_config())
    handler.conn = FakeConn(block=False)
    await handler._receiver_loop()
    assert handler.ended == 1


async def test_session_and_conversation_ids_recorded():
    events = [
        SimpleNamespace(type=vmh.ServerEventType.SESSION_CREATED, session=SimpleNamespace(id="sess-1")),
        SimpleNamespace(type=vmh.ServerEventType.RESPONSE_DONE,
                        response=SimpleNamespace(id="r1", conversation_id="conv-9")),
    ]
    handler = RecordingHandler(handler_config())
    handler.conn = FakeConn(events=events, block=False)
    await handler._receiver_loop()
    assert handler.session_id == "sess-1"
    assert handler.conversation_id == "conv-9"


async def test_cleanup_and_force_close_safe_when_never_connected():
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    handler.force_close()
    await handler.cleanup()


def test_force_close_closes_underlying_response():
    closed = []
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    handler.conn = SimpleNamespace(_connection=SimpleNamespace(_response=SimpleNamespace(close=lambda: closed.append(1))))
    handler.force_close()
    assert closed == [1]
