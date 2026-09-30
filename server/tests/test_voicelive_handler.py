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


async def test_agent_mode_unpinned_route_omits_agent_version(fake_sdk):
    # D-049 (U-LATESTVER): unpinned mode must omit agent_version entirely (the SDK then sends
    # no agent-version query param and Voice Live resolves the latest version) — never pass
    # the literal string "latest" through to the service.
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=AgentRoute("proj", "agent-a", "latest"))
    await handler.connect_voicelive()
    assert "agent_version" not in fake_sdk
    assert fake_sdk["agent_name"] == "agent-a"
    assert fake_sdk["project_name"] == "proj"
    assert "model" not in fake_sdk
    assert not any(v == "latest" for v in fake_sdk.values())
    await handler.cleanup()


async def test_agent_mode_pinned_route_passes_exact_version_string(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=AgentRoute("proj", "agent-a", "24"))
    await handler.connect_voicelive()
    assert fake_sdk["agent_version"] == "24"
    assert isinstance(fake_sdk["agent_version"], str)
    await handler.cleanup()


async def test_agent_mode_unpinned_route_logs_latest_mode(fake_sdk, caplog):
    caplog.set_level("INFO", logger=vmh.logger.name)
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=AgentRoute("proj", "agent-a", "latest"))
    await handler.connect_voicelive()
    assert any("version=latest" in r.getMessage() and "unpinned" in r.getMessage() for r in caplog.records)
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

from azure.ai.voicelive.models import ServerEventResponseDone, SystemMessageItem, UserMessageItem

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


def assert_call_connected_item(item):
    assert isinstance(item, SystemMessageItem)
    assert item.as_dict() == {
        "type": "message",
        "role": "system",
        "content": [{"type": "input_text", "text": vmh.CALL_CONNECTED_MARKER}],
    }


async def test_greeting_failure_before_any_caller_turn_retries_with_call_connected_item():
    # Q-063 / D-048: the connect-time greeting can fail before the caller has said anything.
    # The "caller audio was not understood" marker would be false, and a bare retry fails
    # identically (agent mode needs an input item), so the neutral call-connected item is added.
    handler = await run_events([failed()])  # no speech/transcription events first
    create_item = handler.conn.conversation.item.create
    create_item.assert_awaited_once()
    assert_call_connected_item(create_item.await_args.kwargs["item"])
    handler.conn.response.create.assert_awaited_once_with()  # plain retry: no instructions, no overrides
    assert handler.unrecoverable == 0


@pytest.mark.parametrize("events, route, expected", [
    ([failed()], ROUTE, "marker=call_connected"),
    ([speech_started(), failed()], ROUTE, "marker=caller_audio"),
    ([failed()], None, "marker=none"),
])
async def test_retry_log_names_the_item_added(caplog, events, route, expected):
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = UnrecoverableRecorder(handler_config(), route=route)
    handler.conn = FakeConn(events=events, block=False)
    await handler._receiver_loop()
    retries = [r.getMessage() for r in caplog.records if "response_retry " in r.getMessage()]
    assert len(retries) == 1 and expected in retries[0]


async def test_greeting_retry_adds_item_before_response_create():
    order = []
    conn = FakeConn(events=[failed()], block=False)
    conn.conversation.item.create.side_effect = lambda **kw: order.append("item")
    conn.response.create.side_effect = lambda **kw: order.append("response")
    await run_events(None, conn=conn)
    assert order == ["item", "response"]


async def test_greeting_failure_twice_still_ends_call():
    handler = await run_events([failed("r1"), failed("r2")])
    handler.conn.conversation.item.create.assert_awaited_once()  # the single retry's item only
    handler.conn.response.create.assert_awaited_once()  # bounded: exactly one retry
    assert handler.unrecoverable == 1


async def test_greeting_retry_item_error_ends_call_instead_of_silence():
    conn = FakeConn(events=[failed()], block=False)
    conn.conversation.item.create.side_effect = RuntimeError("socket closed")
    handler = await run_events(None, conn=conn)
    conn.response.create.assert_not_awaited()
    assert handler.unrecoverable == 1


@pytest.mark.parametrize("caller_event", [speech_started, transcription_completed])
async def test_failure_after_caller_turn_injects_marker(caller_event):
    # D-038 unchanged: once the caller has spoken, the retry uses the caller-audio marker.
    handler = await run_events([caller_event(), failed()])
    create_item = handler.conn.conversation.item.create
    create_item.assert_awaited_once()
    item = create_item.await_args.kwargs["item"]
    assert isinstance(item, UserMessageItem)
    assert item.as_dict()["content"] == [{"type": "input_text", "text": vmh.FAILED_RESPONSE_MARKER}]
    handler.conn.response.create.assert_awaited_once_with()


async def test_model_mode_greeting_retry_unchanged():
    # No route (the upstream model-mode web client): the retry stays a plain response.create().
    handler = UnrecoverableRecorder(handler_config())
    handler.conn = FakeConn(events=[failed()], block=False)
    await handler._receiver_loop()
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_awaited_once_with()
    assert handler.unrecoverable == 0


# ----------------------------------------------------------------------
# Q-063 / D-048: the connect-time greeting needs one input item in agent mode
# ----------------------------------------------------------------------


async def test_agent_mode_connect_adds_call_connected_item_before_greeting(fake_sdk):
    order = []
    conn = FakeConn()
    conn.session.update.side_effect = lambda **kw: order.append("session")
    conn.conversation.item.create.side_effect = lambda **kw: order.append("item")
    conn.response.create.side_effect = lambda **kw: order.append("response")
    fake_sdk["_conn"] = conn
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=ROUTE)
    await handler.connect_voicelive()
    assert order == ["session", "item", "response"]
    conn.conversation.item.create.assert_awaited_once()
    assert_call_connected_item(conn.conversation.item.create.await_args.kwargs["item"])
    conn.response.create.assert_awaited_once_with()  # no instructions, no overrides (D-004)
    await handler.cleanup()


async def test_agent_mode_connect_item_error_propagates_without_greeting(fake_sdk):
    # The provider wrappers catch a connect_voicelive() failure and end the call (fallback + hangup).
    conn = FakeConn()
    conn.conversation.item.create.side_effect = RuntimeError("socket closed")
    fake_sdk["_conn"] = conn
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=ROUTE)
    with pytest.raises(RuntimeError):
        await handler.connect_voicelive()
    conn.response.create.assert_not_awaited()
    assert handler._voicelive_connected is False
    assert handler._receiver_task is None
    await handler.cleanup()


async def test_model_mode_connect_adds_no_item(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID=""))
    await handler.connect_voicelive()
    fake_sdk["_conn"].conversation.item.create.assert_not_awaited()
    fake_sdk["_conn"].response.create.assert_awaited_once_with()
    await handler.cleanup()


def test_call_connected_marker_is_neutral_not_a_scripted_line():
    # D-048: a factual statement of what happened; the agent authors the greeting (D-004).
    marker = vmh.CALL_CONNECTED_MARKER
    assert marker == "[call connected]"
    for word in ("say", "greet", "welcome", "hello", "introduce", "respond", "you"):
        assert word not in marker.lower()


# ----------------------------------------------------------------------
# Q-070 / D-050: a response.created that never resolves must not leave the caller in silence
# ----------------------------------------------------------------------

from azure.ai.voicelive.models import (
    ServerEventError,
    ServerEventResponseAudioDelta,
    ServerEventResponseCreated,
    ServerEventResponseMcpCallInProgress,
)

FAST_TIMEOUT = 0.05


def created(rid="r1"):
    return ServerEventResponseCreated({"type": "response.created", "event_id": f"c-{rid}", "response": {
        "id": rid, "object": "realtime.response", "status": "in_progress", "output": []}})


def audio_delta(rid="r1"):
    return ServerEventResponseAudioDelta({"type": "response.audio.delta", "event_id": f"d-{rid}",
                                          "response_id": rid, "item_id": "i1", "output_index": 0,
                                          "content_index": 0, "delta": "AAAA"})


class QueueConn(FakeConn):
    """A Voice Live connection the test feeds event by event (None ends the stream)."""

    def __init__(self):
        super().__init__()
        self.response.cancel = AsyncMock()
        self.queue = asyncio.Queue()

    async def __anext__(self):
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event


@pytest.fixture
def fast_watchdog(monkeypatch):
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", FAST_TIMEOUT)
    monkeypatch.setattr(vmh, "CANCEL_ACK_TIMEOUT_SECONDS", FAST_TIMEOUT)


def no_more_timeouts(monkeypatch):
    """From here on, newly armed timers never fire during the test (keeps timing tests deterministic)."""
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", 3600)


def gated(mock):
    """Make an AsyncMock block until the returned event is set (a recovery held mid-flight)."""
    gate = asyncio.Event()

    async def wait(**kwargs):
        await gate.wait()

    mock.side_effect = wait
    return gate


async def eventually(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.005)


async def settle(multiple=5):
    """Let well over one watchdog timeout pass."""
    await asyncio.sleep(FAST_TIMEOUT * multiple)


async def start_handler(route=ROUTE, events=()):
    handler = UnrecoverableRecorder(handler_config(), route=route)
    handler.conn = QueueConn()
    for event in events:
        handler.conn.queue.put_nowait(event)
    handler._receiver_task = asyncio.create_task(handler._receiver_loop())
    return handler


def assert_plain_tagged_retry(create):
    # The timeout path's retry carries only an event_id tag (to match a rejection): no instructions,
    # no response overrides (D-004).
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert set(kwargs) == {"event_id"} and kwargs["event_id"].startswith("retry-")


def all_watchdog_tasks(handler):
    tasks = set(handler._recovery_tasks)
    if handler._watchdog_task is not None:
        tasks.add(handler._watchdog_task)
    return tasks


async def test_response_timeout_constant_has_real_margin():
    # D-050: normal responses resolved in ~2-4 s on the live calls; the timeout must sit well
    # above that (>= 3x the slowest observed) yet recover inside a caller's patience for silence.
    assert 12 <= vmh.RESPONSE_TIMEOUT_SECONDS <= 20


async def test_stuck_response_triggers_marker_retry(fast_watchdog, caplog):
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    handler.conn.response.cancel.assert_awaited_once_with(response_id="r1")
    create_item = handler.conn.conversation.item.create
    create_item.assert_awaited_once()
    item = create_item.await_args.kwargs["item"]
    assert isinstance(item, UserMessageItem)
    assert item.as_dict()["content"] == [{"type": "input_text", "text": vmh.RESPONSE_STALLED_MARKER}]
    assert_plain_tagged_retry(handler.conn.response.create)  # plain retry: no instructions, no overrides
    assert handler.unrecoverable == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("response_stuck id=r1" in m and "attempt=1" in m for m in messages)
    assert not any("response_failed" in m for m in messages)  # distinguishable from a real failure
    assert any("response_retry attempt=1 marker=response_stalled" in m for m in messages)
    await handler.cleanup()


async def test_failed_path_still_uses_the_caller_audio_marker_after_a_timeout(fast_watchdog, monkeypatch, caplog):
    # The FAILED path (D-038) is unchanged by the timeout path's marker, even within one call.
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    cancel_gate = gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)  # timeout recovery started
    no_more_timeouts(monkeypatch)  # before the retry arms its own timer
    cancel_gate.set()
    await eventually(lambda: handler.conn.response.create.await_count == 1)  # timeout retry
    handler.conn.queue.put_nowait(created("r2"))
    handler.conn.queue.put_nowait(response_done("completed", "r2"))  # recovery worked: counter reset
    handler.conn.queue.put_nowait(failed("r3"))  # a later, ordinary failed response
    await eventually(lambda: handler.conn.response.create.await_count == 2)
    texts = [c.kwargs["item"].as_dict()["content"][0]["text"]
             for c in handler.conn.conversation.item.create.await_args_list]
    assert texts == [vmh.RESPONSE_STALLED_MARKER, vmh.FAILED_RESPONSE_MARKER]
    assert handler.conn.response.create.await_args_list[1].kwargs == {}  # FAILED path: bare create()
    retries = [r.getMessage() for r in caplog.records if "response_retry " in r.getMessage()]
    assert "marker=response_stalled" in retries[0] and "marker=caller_audio" in retries[1]
    assert handler.unrecoverable == 0  # the intended path: no timer ended the call in between
    await handler.cleanup()


def test_response_stalled_marker_is_neutral_and_distinct():
    # D-050 / D-004: a factual statement of what happened, not a scripted line, and not the
    # caller-side "not understood" marker.
    marker = vmh.RESPONSE_STALLED_MARKER
    assert marker.startswith("[") and marker.endswith("]")
    assert marker not in (vmh.FAILED_RESPONSE_MARKER, vmh.CALL_CONNECTED_MARKER)
    words = set(marker.strip("[]").lower().split())
    assert not words & {"say", "sorry", "repeat", "understood", "you", "my"}
    assert "apolog" not in marker.lower()


async def test_response_done_in_time_cancels_the_timer(monkeypatch):
    no_more_timeouts(monkeypatch)
    handler = await start_handler(events=[created("r1")])
    await eventually(lambda: handler._watchdog_task is not None)
    timer = handler._watchdog_task
    handler.conn.queue.put_nowait(response_done("completed", "r1"))
    await eventually(lambda: handler.conversation_id == "conv-1")
    await eventually(timer.done)
    assert timer.cancelled()  # genuinely cancelled, not merely ignored
    assert handler._watchdog_task is None and not handler._recovery_tasks
    handler.conn.response.cancel.assert_not_awaited()
    handler.conn.response.create.assert_not_awaited()
    handler.conn.conversation.item.create.assert_not_awaited()
    assert handler._consecutive_failed_responses == 0
    await handler.cleanup()


async def test_audio_progress_keeps_a_long_response_alive(monkeypatch):
    # A response that is still streaming is not stuck: progress events for it push the deadline out.
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", 0.3)
    handler = await start_handler(events=[created("r1")])
    for _ in range(30):  # ~0.9 s of streaming, three times the timeout, a delta every 30 ms
        await asyncio.sleep(0.03)
        handler.conn.queue.put_nowait(audio_delta("r1"))
    handler.conn.queue.put_nowait(response_done("completed", "r1"))
    await eventually(lambda: handler.conversation_id == "conv-1")
    handler.conn.response.create.assert_not_awaited()
    handler.conn.response.cancel.assert_not_awaited()
    await handler.cleanup()


async def test_progress_for_another_response_does_not_keep_the_watched_one_alive(fast_watchdog):
    handler = await start_handler(events=[created("r1")])
    for _ in range(4):
        await asyncio.sleep(FAST_TIMEOUT / 2)
        handler.conn.queue.put_nowait(audio_delta("other"))
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    await handler.cleanup()


async def test_two_stuck_responses_end_the_call_without_a_second_retry(fast_watchdog, caplog):
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    handler.conn.queue.put_nowait(created("r2"))  # the retry's response also never resolves
    await eventually(lambda: handler.unrecoverable == 1)
    await settle()
    handler.conn.response.create.assert_awaited_once()  # bounded: exactly one retry
    handler.conn.response.cancel.assert_awaited_once_with(response_id="r1")  # none on the final attempt
    assert handler.unrecoverable == 1
    assert any("response_stuck id=r2" in r.getMessage() and "attempt=2" in r.getMessage() for r in caplog.records)
    await handler.cleanup()


async def test_retry_that_never_starts_a_response_still_ends_the_call(fast_watchdog):
    # If Voice Live ignores the retry's response.create() entirely (no response.created ever), the
    # retry itself is watched, so the caller still gets a clean hangup instead of silence.
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.unrecoverable == 1)
    handler.conn.response.create.assert_awaited_once()
    assert handler._consecutive_failed_responses == 2
    await handler.cleanup()


async def test_stuck_then_failed_share_one_bounded_counter(fast_watchdog):
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    handler.conn.queue.put_nowait(created("r2"))
    handler.conn.queue.put_nowait(failed("r2"))
    await eventually(lambda: handler.unrecoverable == 1)
    await settle()
    handler.conn.response.create.assert_awaited_once()
    assert handler.unrecoverable == 1
    await handler.cleanup()


async def test_failed_then_stuck_share_one_bounded_counter(fast_watchdog):
    handler = await start_handler(events=[speech_started(), failed("r1"), created("r2")])
    await eventually(lambda: handler.unrecoverable == 1)
    await settle()
    handler.conn.response.create.assert_awaited_once()  # the FAILED path's single retry only
    handler.conn.response.cancel.assert_not_awaited()
    await handler.cleanup()


async def test_completed_after_a_recovered_timeout_resets_the_counter(fast_watchdog):
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    handler.conn.queue.put_nowait(created("r2"))
    handler.conn.queue.put_nowait(response_done("completed", "r2"))
    await eventually(lambda: handler._consecutive_failed_responses == 0)
    handler.conn.queue.put_nowait(created("r3"))  # a later, unrelated stuck response gets its own retry
    await eventually(lambda: handler.conn.response.create.await_count == 2)
    assert handler.unrecoverable == 0
    await handler.cleanup()


async def test_done_that_beats_an_already_expired_timer_wins(monkeypatch):
    # Race, done side: the timer has expired but the receiver handles response.done before the
    # timer task runs. The done wins: the timer is cancelled and no retry happens.
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", 0)
    handler = await start_handler(events=[created("r1"), response_done("completed", "r1")])
    await eventually(lambda: handler.conversation_id == "conv-1")
    await settle()
    handler.conn.response.create.assert_not_awaited()
    handler.conn.response.cancel.assert_not_awaited()
    assert not all_watchdog_tasks(handler)
    await handler.cleanup()


async def test_late_done_after_timeout_is_ignored(fast_watchdog, monkeypatch, caplog):
    # Race, timer side: once the timer has claimed the response, a late response.done for it is
    # logged and skipped: no second retry, and a late "completed" does not reset the counter.
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    gate = gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)  # recovery in flight
    no_more_timeouts(monkeypatch)
    handler.conn.queue.put_nowait(response_done("completed", "r1"))
    await eventually(lambda: handler.conversation_id == "conv-1")
    assert handler._consecutive_failed_responses == 1  # the late completion did not reset it
    gate.set()
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    handler.conn.queue.put_nowait(created("r2"))  # the retry starts normally
    handler.conn.queue.put_nowait(response_done("failed", "r1"))  # a late failure is skipped too

    def late_logs():
        return [r for r in caplog.records if "response_done_after_timeout id=r1" in r.getMessage()]

    await eventually(lambda: len(late_logs()) == 2)
    handler.conn.response.create.assert_awaited_once()  # no second retry from the late events
    assert handler._consecutive_failed_responses == 1
    assert handler.unrecoverable == 0
    handler.conn.queue.put_nowait(response_done("completed", "r2"))
    await eventually(lambda: handler._consecutive_failed_responses == 0)
    await settle()
    handler.conn.response.create.assert_awaited_once()
    assert not all_watchdog_tasks(handler)
    await handler.cleanup()


async def test_new_response_replaces_the_previous_timer(monkeypatch):
    no_more_timeouts(monkeypatch)
    handler = await start_handler(events=[created("r1")])
    await eventually(lambda: handler._watchdog_task is not None)
    first = handler._watchdog_task
    handler.conn.queue.put_nowait(created("r2"))
    await eventually(lambda: handler._watchdog_task is not first)
    await eventually(first.done)
    assert first.cancelled()
    assert handler._watched_response_id == "r2"
    second = handler._watchdog_task
    handler.conn.queue.put_nowait(response_done("completed", "r2"))
    await eventually(lambda: handler.conversation_id == "conv-1")
    await eventually(second.done)
    assert second.cancelled() and handler._watchdog_task is None  # exactly one timer at any time
    handler.conn.response.create.assert_not_awaited()
    await handler.cleanup()


@pytest.mark.parametrize("events, route, expected", [
    ([created("r1")], ROUTE, "call_connected"),
    ([speech_started(), created("r1")], ROUTE, "response_stalled"),
    ([transcription_completed(), created("r1")], ROUTE, "response_stalled"),
    ([created("r1")], None, "none"),
])
async def test_stuck_retry_marker_choice(fast_watchdog, caplog, events, route, expected):
    # Same selection as the FAILED path, except that after a caller turn the timeout path uses the
    # accurate response-stalled marker (the caller was understood; the agent's response stalled).
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(route=route, events=events)
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    retries = [r.getMessage() for r in caplog.records if "response_retry " in r.getMessage()]
    assert len(retries) == 1 and f"marker={expected}" in retries[0]
    create_item = handler.conn.conversation.item.create
    if expected == "response_stalled":
        item = create_item.await_args.kwargs["item"]
        assert isinstance(item, UserMessageItem)
        assert item.as_dict()["content"] == [{"type": "input_text", "text": vmh.RESPONSE_STALLED_MARKER}]
    elif expected == "call_connected":
        assert_call_connected_item(create_item.await_args.kwargs["item"])
    else:
        create_item.assert_not_awaited()
    assert_plain_tagged_retry(handler.conn.response.create)
    await handler.cleanup()


async def test_stuck_retry_adds_item_before_response_create(fast_watchdog):
    order = []
    handler = await start_handler(events=[created("r1")])
    handler.conn.response.cancel.side_effect = lambda **kw: order.append("cancel")
    handler.conn.conversation.item.create.side_effect = lambda **kw: order.append("item")
    handler.conn.response.create.side_effect = lambda **kw: order.append("response")
    await eventually(lambda: "response" in order)
    assert order == ["cancel", "item", "response"]
    await handler.cleanup()


async def test_cancel_error_does_not_block_the_retry(fast_watchdog):
    handler = await start_handler(events=[speech_started(), created("r1")])
    handler.conn.response.cancel.side_effect = RuntimeError("no active response")
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    assert handler.unrecoverable == 0
    await handler.cleanup()


async def test_stuck_retry_send_error_ends_call_and_leaves_no_timer(fast_watchdog):
    handler = await start_handler(events=[speech_started(), created("r1")])
    handler.conn.response.create.side_effect = RuntimeError("socket closed")
    await eventually(lambda: handler.unrecoverable == 1)
    await eventually(lambda: not all_watchdog_tasks(handler))
    await settle()
    assert handler.unrecoverable == 1  # the retry's own watch was disarmed, nothing fires later
    await handler.cleanup()


async def test_cleanup_cancels_a_pending_timer(monkeypatch):
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", 3600)
    handler = await start_handler(events=[created("r1")])
    await eventually(lambda: handler._watchdog_task is not None)
    timer = handler._watchdog_task
    await handler.cleanup()
    assert timer.done() and timer.cancelled()
    assert handler._watchdog_task is None and not handler._recovery_tasks


async def test_cleanup_cancels_an_in_flight_recovery(fast_watchdog):
    handler = await start_handler(events=[speech_started(), created("r1")])
    gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    (recovery,) = handler._recovery_tasks
    await handler.cleanup()
    assert recovery.done() and recovery.cancelled()
    assert handler._watchdog_task is None and not handler._recovery_tasks


async def test_cleanup_during_retry_send_leaves_no_pending_retry_timer(fast_watchdog, monkeypatch):
    # The recovery arms the retry's own timer before response.create(); cleanup landing while that
    # send is in flight must cancel both the recovery and that timer.
    handler = await start_handler(events=[speech_started(), created("r1")])
    cancel_gate = gated(handler.conn.response.cancel)
    gated(handler.conn.response.create)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    no_more_timeouts(monkeypatch)  # the pending-retry timer must not fire before cleanup
    cancel_gate.set()
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    timer = handler._watchdog_task
    assert timer is not None  # the pending-retry timer
    (recovery,) = handler._recovery_tasks
    await handler.cleanup()
    assert timer.cancelled() and recovery.cancelled()
    assert not all_watchdog_tasks(handler)
    await settle()
    assert handler.unrecoverable == 0


async def test_force_close_cancels_a_pending_timer(monkeypatch):
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", 3600)
    handler = await start_handler(events=[created("r1")])
    await eventually(lambda: handler._watchdog_task is not None)
    timer = handler._watchdog_task
    handler.force_close()
    await eventually(timer.done)
    assert timer.cancelled()
    assert handler._watchdog_task is None
    await handler.cleanup()


async def test_voicelive_drop_disarms_the_timer(fast_watchdog):
    handler = await start_handler(events=[created("r1")])
    await eventually(lambda: handler._watchdog_task is not None)
    timer = handler._watchdog_task
    handler.conn.queue.put_nowait(None)  # Voice Live ends the stream
    await eventually(lambda: handler.ended == 1)
    await eventually(timer.done)
    await settle()
    assert timer.cancelled()
    handler.conn.response.create.assert_not_awaited()
    await handler.cleanup()


def mcp_call_in_progress():
    # Real SDK shape: this event carries no response_id.
    return ServerEventResponseMcpCallInProgress({"type": "response.mcp_call.in_progress",
                                                 "item_id": "i1", "output_index": 0})


async def test_cancel_confirmation_releases_the_retry_without_waiting(fast_watchdog, monkeypatch, caplog):
    caplog.set_level("INFO", logger=vmh.logger.name)
    monkeypatch.setattr(vmh, "CANCEL_ACK_TIMEOUT_SECONDS", 3600)
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    no_more_timeouts(monkeypatch)
    await settle()
    handler.conn.response.create.assert_not_awaited()  # still waiting for the cancel to land
    handler.conn.queue.put_nowait(response_done("cancelled", "r1", {"type": "cancelled", "reason": "client_cancelled"}))
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    assert handler._cancel_ack is None
    assert not any("response_stuck_cancel_unconfirmed" in r.getMessage() for r in caplog.records)
    await handler.cleanup()


async def test_unconfirmed_cancel_still_retries(fast_watchdog, caplog):
    caplog.set_level("INFO", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    assert any("response_stuck_cancel_unconfirmed id=r1" in r.getMessage() for r in caplog.records)
    assert handler._cancel_ack is None
    await handler.cleanup()


async def test_retry_skipped_when_a_new_response_starts_during_recovery(fast_watchdog, monkeypatch, caplog):
    # If Voice Live starts answering on its own while the recovery is in flight, the retry would
    # only collide with it: no marker, no response.create(), and the new response is the one watched.
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    gate = gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    no_more_timeouts(monkeypatch)
    handler.conn.queue.put_nowait(created("r2"))
    await eventually(lambda: handler._watched_response_id == "r2")
    watch = handler._watchdog_task
    gate.set()
    await eventually(lambda: not handler._recovery_tasks)
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_not_awaited()
    assert handler._watchdog_task is watch and not watch.done()  # r2's timer untouched
    assert any("response_retry_skipped attempt=1" in r.getMessage() for r in caplog.records)
    handler.conn.queue.put_nowait(response_done("completed", "r2"))
    await eventually(lambda: handler._consecutive_failed_responses == 0)
    await handler.cleanup()


async def test_retry_skipped_when_a_new_response_starts_while_adding_the_marker(fast_watchdog, monkeypatch):
    handler = await start_handler(events=[speech_started(), created("r1")])
    gate = gated(handler.conn.conversation.item.create)
    await eventually(lambda: handler.conn.conversation.item.create.await_count == 1)
    no_more_timeouts(monkeypatch)
    handler.conn.queue.put_nowait(created("r2"))
    await eventually(lambda: handler._watched_response_id == "r2")
    gate.set()
    await eventually(lambda: not handler._recovery_tasks)
    handler.conn.response.create.assert_not_awaited()
    assert handler._watched_response_id == "r2"  # not replaced by a pending-retry timer
    await handler.cleanup()


async def test_late_audio_from_a_timed_out_response_is_dropped(fast_watchdog, monkeypatch):
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    no_more_timeouts(monkeypatch)
    handler.on_audio_delta = AsyncMock()
    handler.conn.queue.put_nowait(created("r2"))
    handler.conn.queue.put_nowait(audio_delta("r1"))  # the stalled response wakes up
    handler.conn.queue.put_nowait(audio_delta("r2"))  # the retry's answer
    await eventually(lambda: handler.on_audio_delta.await_count == 1)
    await asyncio.sleep(0.02)
    handler.on_audio_delta.assert_awaited_once()  # only r2's audio reached the caller
    await handler.cleanup()


def mcp_call_completed():
    return SimpleNamespace(type="response.mcp_call.completed", item_id="i1", output_index=0)


async def test_tool_call_stretches_the_deadline_until_the_tool_ends(monkeypatch):
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(vmh, "CANCEL_ACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(vmh, "TOOL_CALL_TIMEOUT_SECONDS", 3600)
    handler = await start_handler(events=[created("r1"), mcp_call_in_progress()])
    await asyncio.sleep(0.3)  # three ordinary timeouts: a tool call is running, so no recovery
    handler.conn.queue.put_nowait(audio_delta("r1"))  # interim filler audio while the tool still runs
    await asyncio.sleep(0.3)  # the filler must not pull the stretched deadline back in
    handler.conn.response.create.assert_not_awaited()
    handler.conn.queue.put_nowait(mcp_call_completed())  # the tool ends; ordinary deadline again
    await eventually(lambda: handler.conn.response.create.await_count == 1)  # r1 then stalls
    await handler.cleanup()


def test_tool_call_events_are_real_sdk_event_types():
    names = {e.value for e in vmh.ServerEventType}
    assert vmh._TOOL_CALL_EVENTS <= names
    assert vmh._TOOL_CALL_END_EVENTS <= names
    assert not vmh._TOOL_CALL_EVENTS & vmh._TOOL_CALL_END_EVENTS
    assert vmh.TOOL_CALL_TIMEOUT_SECONDS > vmh.RESPONSE_TIMEOUT_SECONDS


async def test_voicelive_drop_during_recovery_cancels_it(fast_watchdog):
    handler = await start_handler(events=[speech_started(), created("r1")])
    gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    (recovery,) = handler._recovery_tasks
    handler.conn.queue.put_nowait(None)  # Voice Live ends the stream mid-recovery
    await eventually(recovery.done)
    assert recovery.cancelled()
    handler.conn.response.create.assert_not_awaited()  # nothing sent on the dead connection
    assert not all_watchdog_tasks(handler)
    await handler.cleanup()


def error_event(code="conversation_already_has_active_response", event_id=None):
    # Parsed by the real SDK model, so error.event_id is read the way it is on a live call.
    error = {"type": "invalid_request_error", "code": code, "message": "rejected"}
    if event_id is not None:
        error["event_id"] = event_id
    return ServerEventError({"type": "error", "event_id": "srv-1", "error": error})


async def retried_with_pending_watch(monkeypatch, events=None):
    """A stuck r1 has been retried; the retry's own (pending) timer is armed and will not fire."""
    handler = await start_handler(events=events or [speech_started(), created("r1")])
    cancel_gate = gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    no_more_timeouts(monkeypatch)
    cancel_gate.set()
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    assert handler._watched_response_id is vmh._PENDING_RETRY
    return handler


async def test_rejected_retry_ends_the_call_without_another_timeout(fast_watchdog, monkeypatch, caplog):
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await retried_with_pending_watch(monkeypatch)
    retry_event_id = handler.conn.response.create.await_args.kwargs["event_id"]
    handler.conn.queue.put_nowait(error_event(event_id=retry_event_id))
    await eventually(lambda: handler.unrecoverable == 1)  # timers can't fire: the error did this
    handler.conn.response.create.assert_awaited_once()
    assert any("response_stuck id=pending_retry reason=retry_rejected attempt=2" in r.getMessage()
               for r in caplog.records)
    await eventually(lambda: not all_watchdog_tasks(handler))
    await handler.cleanup()


@pytest.mark.parametrize("event_id", [None, "some-other-client-event"])
async def test_unrelated_error_during_the_pending_retry_does_not_end_the_call(fast_watchdog, monkeypatch, event_id):
    # e.g. a late error for the marker item or the cancel: not the retry's own rejection.
    handler = await retried_with_pending_watch(monkeypatch)
    timer = handler._watchdog_task
    handler.conn.queue.put_nowait(error_event("response_cancel_not_active", event_id=event_id))
    handler.conn.queue.put_nowait(created("r2"))  # the retry then starts normally
    await eventually(lambda: handler._watched_response_id == "r2")
    await eventually(timer.done)
    assert timer.cancelled()  # replaced by r2's watch, not claimed by a rejection
    assert handler.unrecoverable == 0 and handler._consecutive_failed_responses == 1
    assert not handler._recovery_tasks
    await handler.cleanup()


async def test_error_with_no_pending_retry_changes_nothing(monkeypatch):
    no_more_timeouts(monkeypatch)
    handler = await start_handler(events=[created("r1"), error_event("some_other_error")])
    await eventually(lambda: handler._watched_response_id == "r1")
    await asyncio.sleep(0.02)
    assert handler._watched_response_id == "r1" and not handler._recovery_tasks
    handler.conn.response.create.assert_not_awaited()
    assert handler.unrecoverable == 0
    await handler.cleanup()


async def test_unrelated_done_does_not_clear_the_pending_retry_watch(fast_watchdog, monkeypatch):
    handler = await retried_with_pending_watch(monkeypatch)
    timer = handler._watchdog_task
    handler.conn.queue.put_nowait(response_done("completed", "r-unrelated"))
    handler.conn.queue.put_nowait(SimpleNamespace(type=vmh.ServerEventType.RESPONSE_DONE,
                                                  response=SimpleNamespace(id=None, status="completed")))
    await eventually(lambda: handler.conversation_id == "conv-1")
    await asyncio.sleep(0.02)
    assert handler._watched_response_id is vmh._PENDING_RETRY and handler._watchdog_task is timer
    assert not timer.done()
    await handler.cleanup()


async def test_progress_is_not_credited_to_a_retry_that_has_not_started(fast_watchdog, monkeypatch):
    # A late id-less tool event from the cancelled response must not stretch the retry's watch.
    handler = await retried_with_pending_watch(monkeypatch)
    monkeypatch.setattr(vmh, "RESPONSE_TIMEOUT_SECONDS", FAST_TIMEOUT)
    monkeypatch.setattr(vmh, "TOOL_CALL_TIMEOUT_SECONDS", 3600)
    handler._arm_response_watchdog(vmh._PENDING_RETRY)  # re-arm with the short timeout
    handler.conn.queue.put_nowait(mcp_call_in_progress())
    await eventually(lambda: handler.unrecoverable == 1)
    await handler.cleanup()


async def test_response_without_an_id_is_not_watched_and_does_not_mute_audio(fast_watchdog, caplog):
    # Its response.done could never be matched, so watching it would only cause a spurious retry.
    caplog.set_level("WARNING", logger=vmh.logger.name)
    no_id = SimpleNamespace(type=vmh.ServerEventType.RESPONSE_CREATED, response=SimpleNamespace(id=None))
    handler = await start_handler(events=[speech_started(), no_id])
    handler.on_audio_delta = AsyncMock()
    handler.conn.queue.put_nowait(SimpleNamespace(type=vmh.ServerEventType.RESPONSE_AUDIO_DELTA, delta=b"\x01\x02"))
    await eventually(lambda: handler.on_audio_delta.await_count == 1)
    await settle()
    assert handler._watchdog_task is None and not handler._recovery_tasks
    assert None not in handler._timed_out_response_ids
    handler.conn.response.create.assert_not_awaited()
    assert any("response_created_without_id" in r.getMessage() for r in caplog.records)
    await handler.cleanup()


async def test_transcript_of_a_timed_out_response_is_not_forwarded(fast_watchdog, monkeypatch):
    handler = await start_handler(events=[speech_started(), created("r1")])
    await eventually(lambda: handler.conn.response.create.await_count == 1)
    no_more_timeouts(monkeypatch)
    handler.on_transcript_done = AsyncMock()

    def transcript_done(rid):
        return SimpleNamespace(type=vmh.ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE, transcript=f"t-{rid}",
                               response_id=rid)

    handler.conn.queue.put_nowait(transcript_done("r1"))
    handler.conn.queue.put_nowait(transcript_done("r2"))
    await eventually(lambda: handler.on_transcript_done.await_count == 1)
    await asyncio.sleep(0.02)
    handler.on_transcript_done.assert_awaited_once_with("t-r2")
    await handler.cleanup()


async def test_retry_skipped_when_the_failed_path_handled_a_later_failure(fast_watchdog, monkeypatch, caplog):
    # A new response is created and fails while the timeout recovery is awaiting: the FAILED path
    # acts on it (attempt 2, end the call), so the timeout recovery must not also retry.
    caplog.set_level("WARNING", logger=vmh.logger.name)
    handler = await start_handler(events=[speech_started(), created("r1")])
    gate = gated(handler.conn.response.cancel)
    await eventually(lambda: handler.conn.response.cancel.await_count == 1)
    no_more_timeouts(monkeypatch)
    handler.conn.queue.put_nowait(created("r2"))
    handler.conn.queue.put_nowait(failed("r2"))
    await eventually(lambda: handler.unrecoverable == 1)
    gate.set()
    await eventually(lambda: not handler._recovery_tasks)
    handler.conn.conversation.item.create.assert_not_awaited()
    handler.conn.response.create.assert_not_awaited()
    assert handler.unrecoverable == 1
    assert any("response_retry_skipped attempt=1 reason=failure_count_changed" in r.getMessage()
               for r in caplog.records)
    await handler.cleanup()
