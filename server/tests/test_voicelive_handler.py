import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handler.voicelive_media_handler as vmh
from app.bridge_config import _deep_freeze
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
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=AsyncMock()))
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


async def test_interim_response_with_nested_frozen_value_is_json_serializable(fake_sdk):
    # BridgeConfig freezes nested dicts/lists too (_deep_freeze), so INTERIM_RESPONSE
    # as loaded by the real config path carries MappingProxyType/tuple nested inside
    # it. A shallow dict(...) copy leaves those nested frozen values in place and
    # json.dumps() on session.as_dict() then raises TypeError — this must not happen.
    interim = _deep_freeze({"type": "llm_interim_response", "nested": {"a": [1, 2, {"b": 3}]}})
    handler = vmh.VoiceLiveMediaHandler(handler_config(INTERIM_RESPONSE=interim), route=ROUTE)
    await handler.connect_voicelive()
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    json.dumps(sent)  # must not raise TypeError: mappingproxy is not JSON serializable
    assert sent["interim_response"] == {"type": "llm_interim_response", "nested": {"a": [1, 2, {"b": 3}]}}
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


# ----------------------------------------------------------------------
# Q-036 / D-038: a failed Voice Live response must never leave the caller in silence
# ----------------------------------------------------------------------

from azure.ai.voicelive.models import ServerEventResponseDone, UserMessageItem

# The exact failure shape seen on the live call (Q-036), parsed by the real SDK model.
_FAILED_ERROR = {
    "type": "invalid_request_error",
    "code": "agent_missing_required_parameter",
    "message": "Foundry agent service response error: One of 'input' or 'prompt' must be provided "
               "and yield at least one input item.",
}


def response_done(status, rid="r1", details=None):
    response = {"id": rid, "object": "realtime.response", "status": status, "output": [],
                "conversation_id": "conv-1"}
    if details is not None:
        response["status_details"] = details
    return ServerEventResponseDone({"type": "response.done", "event_id": f"e-{rid}", "response": response})


def failed(rid="r1"):
    return response_done("failed", rid, {"type": "failed", "error": _FAILED_ERROR})


class UnrecoverableRecorder(vmh.VoiceLiveMediaHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unrecoverable = 0
        self.ended = 0

    async def on_response_unrecoverable(self):
        self.unrecoverable += 1

    async def on_voicelive_ended(self):
        self.ended += 1


async def run_events(events, conn=None):
    handler = UnrecoverableRecorder(handler_config(), route=ROUTE)
    handler.conn = conn or FakeConn(events=events, block=False)
    await handler._receiver_loop()
    return handler


async def test_failed_response_injects_marker_and_retries(caplog):
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await run_events([speech_started(), failed()])
    create_item = handler.conn.conversation.item.create
    create_item.assert_awaited_once()
    item = create_item.await_args.kwargs["item"]
    assert isinstance(item, UserMessageItem)
    assert item.as_dict()["content"] == [{"type": "input_text", "text": vmh.FAILED_RESPONSE_MARKER}]
    handler.conn.response.create.assert_awaited_once_with()  # plain retry: no instructions, no overrides
    assert handler.unrecoverable == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("response_failed" in m and "agent_missing_required_parameter" in m and "attempt=1" in m
               for m in warnings)


async def test_marker_is_neutral_not_a_scripted_line():
    # D-038: a factual marker only; the agent authors what is actually said (D-004).
    marker = vmh.FAILED_RESPONSE_MARKER
    assert marker.startswith("[") and marker.endswith("]")
    assert "say" not in marker.lower() and "sorry" not in marker.lower()


async def test_completed_response_unchanged():
    handler = await run_events([response_done("completed")])
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_not_awaited()
    assert handler.unrecoverable == 0
    assert handler.conversation_id == "conv-1"


@pytest.mark.parametrize("status, details", [
    ("cancelled", {"type": "cancelled", "reason": "turn_detected"}),
    ("incomplete", {"type": "incomplete", "reason": "max_output_tokens"}),
])
async def test_cancelled_and_incomplete_take_no_action(status, details):
    handler = await run_events([response_done(status, details=details)])
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_not_awaited()
    assert handler.unrecoverable == 0


async def test_second_consecutive_failure_ends_call_without_second_retry(caplog):
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await run_events([speech_started(), failed("r1"), failed("r2")])
    handler.conn.response.create.assert_awaited_once()  # bounded: exactly one retry
    handler.conn.conversation.item.create.assert_awaited_once()
    assert handler.unrecoverable == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("response_failed" in m and "attempt=2" in m for m in warnings)


async def test_cancelled_between_failures_does_not_reset_counter():
    handler = await run_events([
        speech_started(),
        failed("r1"),
        response_done("cancelled", "r2", {"type": "cancelled", "reason": "turn_detected"}),
        failed("r3"),
    ])
    handler.conn.response.create.assert_awaited_once()
    assert handler.unrecoverable == 1


async def test_completed_resets_failure_counter():
    handler = await run_events([speech_started(), failed("r1"), response_done("completed", "r2"), failed("r3")])
    assert handler.conn.response.create.await_count == 2
    assert handler.conn.conversation.item.create.await_count == 2
    assert handler.unrecoverable == 0


async def test_retry_send_error_ends_call_instead_of_silence():
    conn = FakeConn(events=[speech_started(), failed()], block=False)
    conn.response.create.side_effect = RuntimeError("socket closed")
    handler = await run_events(None, conn=conn)
    assert handler.unrecoverable == 1


async def test_unrecoverable_hook_error_does_not_kill_receiver_loop():
    class Boom(UnrecoverableRecorder):
        async def on_response_unrecoverable(self):
            raise RuntimeError("boom")

    handler = Boom(handler_config(), route=ROUTE)
    handler.conn = FakeConn(events=[failed("r1"), failed("r2"), response_done("completed", "r3")], block=False)
    await handler._receiver_loop()
    assert handler.conversation_id == "conv-1"  # r3 still processed after the hook raised


async def test_base_unrecoverable_hook_is_safe_noop():
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    await handler.on_response_unrecoverable()


def speech_started():
    return SimpleNamespace(type=vmh.ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED, audio_start_ms=0)


def transcription_completed(text=""):
    return SimpleNamespace(type=vmh.ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
                           transcript=text)


async def test_greeting_failure_before_any_caller_turn_retries_without_marker():
    # The connect-time greeting can fail before the caller has said anything: the
    # "caller audio was not understood" marker would be false, so retry plainly.
    handler = await run_events([failed()])  # no speech/transcription events first
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_awaited_once_with()
    assert handler.unrecoverable == 0


async def test_greeting_failure_twice_still_ends_call():
    handler = await run_events([failed("r1"), failed("r2")])
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_awaited_once()
    assert handler.unrecoverable == 1


@pytest.mark.parametrize("caller_event", [speech_started, transcription_completed])
async def test_failure_after_caller_turn_injects_marker(caller_event):
    handler = await run_events([caller_event(), failed()])
    handler.conn.conversation.item.create.assert_awaited_once()
    handler.conn.response.create.assert_awaited_once_with()
