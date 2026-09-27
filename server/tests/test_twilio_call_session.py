"""UT01b: Twilio call-ending hardening.

Voice Live drop detection, the Voice Live connect timeout, an idempotent request_end(), no
orphaned Voice Live connection when a call ends mid-connect, and non-blocking cap/idle hooks.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from quart.testing import WebsocketResponseError
from quart.testing.connections import WebsocketDisconnectError

import app.handler.voicelive_media_handler as vmh
from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.handler.voicelive_close import close_voicelive
from app.providers.twilio.call_session import CallEnded, TwilioCallSession
from app.providers.twilio.event_handler import TwilioEventHandler
from app.providers.twilio.media_handler import TwilioMediaHandler
from app.routing import AgentRoute
from tests.helpers import BRIDGE_ENV_KEYS, VALID_ROUTING
from tests.test_voicelive_handler import FakeConn, FakeCredential, FakeCtx

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTH = "twilio-test-auth-token"
ROUTED = "+14165551234"  # the key in VALID_ROUTING
ROUTE = AgentRoute("proj", "agent-a", "10")


def _handler():
    return TwilioMediaHandler({
        "AZURE_VOICE_LIVE_ENDPOINT": "https://vl.example",
        "VOICE_LIVE_MODEL": "gpt-4o-mini",
        "AZURE_VOICE_LIVE_API_KEY": "key",
        "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID": "",
        "AMBIENT_PRESET": "none",
        "TWILIO_AUTH_TOKEN": AUTH,
    })


class FakeWs:
    """A Twilio media socket: receive() pops queued messages; close() records its calls."""

    def __init__(self, close_blocks=False):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closes = []
        self._close_blocks = close_blocks

    async def receive(self):
        return await self.inbox.get()

    async def send(self, data):
        pass

    async def close(self, code, reason=""):
        self.closes.append(code)
        if self._close_blocks:
            await asyncio.sleep(3600)


def _session(handler=None, ws=None, timeout=5.0):
    handler = handler or _handler()
    ws = ws or FakeWs()
    session = TwilioCallSession(
        handler=handler, twilio_ws=ws, fallback_message="fb", goodbye_message="bye",
        voice_live_connect_timeout=timeout, log_context="call=CA-test",
    )
    handler.route = ROUTE
    handler.session = session
    return handler, ws, session


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- request_end ---------------------------------------------------------------------------------


async def test_request_end_is_idempotent_first_reason_wins():
    handler, ws, session = _session()
    stops = []
    handler.stop_forwarding_agent_audio = lambda: stops.append(1)
    session.request_end("voicelive_dropped", "fb")
    session.request_end("idle", "fb")
    session.request_end("call_cap", "bye")
    await _settle()
    assert session.terminated_reason == "voicelive_dropped"
    assert stops == [1]
    assert ws.closes == [1000]  # closed exactly once


async def test_request_end_stops_agent_audio_and_closes_twilio_ws():
    handler, ws, session = _session()
    session.request_end("idle", "fb")
    assert handler._forward_agent_audio is False  # synchronous, before the close task runs
    await _settle()
    assert ws.closes == [1000]


async def test_request_end_never_touches_voicelive(monkeypatch):
    handler, ws, session = _session()
    cleanups = []

    async def fake_cleanup():
        cleanups.append(1)

    monkeypatch.setattr(handler, "cleanup", fake_cleanup)
    monkeypatch.setattr(handler, "force_close", lambda: cleanups.append("force"))
    session.request_end("voicelive_dropped", "fb")
    await _settle()
    assert cleanups == []  # the only Voice Live close is /twilio/ws's finally


async def test_request_end_survives_a_failing_ws_close():
    handler, ws, session = _session()

    async def boom(code, reason=""):
        raise RuntimeError("Cannot close websocket multiple times")

    ws.close = boom
    session.request_end("idle", "fb")
    await session.wait_closed(1.0)  # must not raise
    assert session.terminated_reason == "idle"


# --- receive(): the call loop's exit path --------------------------------------------------------


async def test_receive_passes_messages_through():
    _, ws, session = _session()
    await ws.inbox.put("hello")
    assert await session.receive() == "hello"


async def test_receive_raises_call_ended_when_request_end_runs_while_waiting():
    _, ws, session = _session()
    pending = asyncio.ensure_future(session.receive())
    await _settle()
    session.request_end("voicelive_dropped", "fb")
    with pytest.raises(CallEnded):
        await asyncio.wait_for(pending, 1.0)
    # Once ended, receive() raises straight away, even if more media is queued.
    await ws.inbox.put("late media")
    with pytest.raises(CallEnded):
        await session.receive()


async def test_receive_cancelled_by_timeout_leaves_no_pending_inner_receive():
    _, ws, session = _session()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(session.receive(), 0.01)
    await ws.inbox.put("next")
    # The earlier inner receive was cancelled, so it didn't swallow this message.
    assert await asyncio.wait_for(session.receive(), 1.0) == "next"


# --- TwilioMediaHandler hooks --------------------------------------------------------------------


class RecordingSession:
    def __init__(self, timeout=5.0):
        self.fallback_message = "fb"
        self.goodbye_message = "bye"
        self.voice_live_connect_timeout = timeout
        self.log_context = "call=CA-test"
        self.terminated_reason = None
        self.ends = []

    def request_end(self, reason, message):
        self.ends.append((reason, message))
        self.terminated_reason = self.terminated_reason or reason


@pytest.mark.parametrize(
    "hook, expected",
    [
        ("on_voicelive_ended", ("voicelive_dropped", "fb")),
        ("on_call_cap", ("call_cap", "bye")),
        ("on_idle", ("idle", "fb")),
    ],
)
async def test_hooks_route_to_request_end(hook, expected):
    handler = _handler()
    handler.session = RecordingSession()
    await getattr(handler, hook)()
    assert handler.session.ends == [expected]


@pytest.mark.parametrize("hook", ["on_call_cap", "on_idle", "on_voicelive_ended"])
async def test_hooks_do_not_await_twilio_io(hook):
    """Q-008: run_call_loop awaits cap/idle with no timeout. Even if closing the Twilio socket never
    returns, the hook must return at once; the close runs in a task the session owns."""
    handler, ws, session = _session(ws=FakeWs(close_blocks=True))
    await asyncio.wait_for(getattr(handler, hook)(), 0.5)
    assert session.terminated_reason is not None
    await _settle()
    assert ws.closes == [1000]
    session._close_task.cancel()


async def test_log_context_comes_from_session():
    handler, _, session = _session()
    assert handler.log_context == "call=CA-test"


# --- connect timeout -----------------------------------------------------------------------------


async def test_connect_timeout_is_a_failure_that_ends_the_call(monkeypatch):
    async def hang(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", hang)
    handler = _handler()
    handler.session = RecordingSession(timeout=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(handler.connect_voicelive(), 2.0)
    assert handler.session.ends == [("voicelive_connect_failed", "fb")]


async def test_connect_error_ends_the_call(monkeypatch):
    async def boom(self):
        raise RuntimeError("bad agent version")

    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", boom)
    handler = _handler()
    handler.session = RecordingSession()
    with pytest.raises(RuntimeError):
        await handler.connect_voicelive()
    assert handler.session.ends == [("voicelive_connect_failed", "fb")]


async def test_connect_skipped_when_call_already_ended(monkeypatch):
    calls = []

    async def ok(self):
        calls.append(1)

    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", ok)
    handler = _handler()
    handler.session = RecordingSession()
    handler.session.terminated_reason = "idle"
    await handler.connect_voicelive()
    assert calls == []
    assert handler.session.ends == []


# --- a call ending while Voice Live connect is in flight (U11's regression, adapted) -------------


class _ClosableConn(FakeConn):
    def __init__(self):
        super().__init__()
        self.closed = 0

    async def close(self):
        self.closed += 1


class _SdkLikeCtx:
    """Mirrors azure.ai.voicelive.aio: __aexit__ only closes once __aenter__ has returned a connection.

    Records the order of events so a test can prove cleanup never runs while the handshake is open.
    """

    def __init__(self):
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.conn = None
        self.events = []

    async def __aenter__(self):
        self.entered.set()
        try:
            await self.release.wait()  # the WebSocket handshake
        except asyncio.CancelledError:
            self.events.append("handshake_cancelled")
            raise
        self.conn = _ClosableConn()
        self.events.append("connected")
        return self.conn

    async def __aexit__(self, *exc):
        self.events.append("aexit")
        if self.conn is not None:
            await self.conn.close()


@pytest.fixture
def sdk_like(monkeypatch):
    ctx = _SdkLikeCtx()
    monkeypatch.setattr(vmh, "voicelive_connect", lambda **kwargs: ctx)
    monkeypatch.setattr(vmh, "DefaultAzureCredential", FakeCredential)
    return ctx


async def test_call_ending_mid_connect_leaves_no_orphaned_connection(sdk_like):
    """The call ends while the Voice Live handshake is still in flight. run_call_loop cancels and awaits
    the connect task before /twilio/ws's finally runs the one Voice Live close, so a connection can
    never complete after cleanup has already run (U11's leak, prevented by ordering)."""
    handler, ws, session = _session()
    call_manager = CallManager()
    await call_manager.acquire("CA-test", "twilio")
    loop_task = asyncio.ensure_future(
        run_call_loop(call_manager=call_manager, call_id="CA-test", ws=session, handler=handler)
    )
    await asyncio.wait_for(sdk_like.entered.wait(), 1.0)
    session.request_end("idle", "fb")  # the call ends mid-handshake
    with pytest.raises(CallEnded):
        await asyncio.wait_for(loop_task, 1.0)
    await close_voicelive(handler, 1.0, session.log_context)  # what /twilio/ws's finally does
    sdk_like.release.set()  # a handshake "completing" now has nobody left to hand a connection to
    await _settle()
    assert sdk_like.events[0] == "handshake_cancelled"
    assert "connected" not in sdk_like.events
    assert handler.conn is None and handler._conn_ctx is None


async def test_call_ending_after_connect_closes_the_connection_once(sdk_like):
    handler, ws, session = _session()
    call_manager = CallManager()
    await call_manager.acquire("CA-test", "twilio")
    loop_task = asyncio.ensure_future(
        run_call_loop(call_manager=call_manager, call_id="CA-test", ws=session, handler=handler)
    )
    sdk_like.release.set()
    for _ in range(100):
        if handler._voicelive_connected:
            break
        await asyncio.sleep(0.01)
    assert handler._voicelive_connected
    session.request_end("idle", "fb")
    with pytest.raises(CallEnded):
        await asyncio.wait_for(loop_task, 1.0)
    await close_voicelive(handler, 1.0, session.log_context)
    assert sdk_like.conn.closed == 1


# --- end to end through /twilio/ws ---------------------------------------------------------------


def twilio_env(**overrides):
    env = {"TWILIO_AUTH_TOKEN": AUTH, "AGENT_ROUTING_JSON": VALID_ROUTING}
    env.update(overrides)
    return env


def _start_msg(token):
    return json.dumps({
        "event": "start",
        "streamSid": "MZ-test",
        "start": {"callSid": "CA-test", "customParameters": {"token": token, "calledNumber": ROUTED},
                  "mediaFormat": {}},
    })


def _token():
    return TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)


async def _wait_for_loop_exit(server, timeout=2.0):
    """The server-side call loop has exited on its own when /twilio/ws's finally released the call."""
    deadline = asyncio.get_running_loop().time() + timeout
    while server.call_manager.active_count:
        assert asyncio.get_running_loop().time() < deadline, "the call loop never exited"
        await asyncio.sleep(0.01)


async def test_voicelive_drop_mid_call_ends_the_twilio_call(load_server, monkeypatch, logs):
    """B3: Voice Live ends mid-call. The Twilio socket must be closed and the call loop must exit on
    its own, well before MAX_CALL_SECONDS or the idle timeout — while the caller is still connected."""
    server = load_server(**twilio_env())
    connected = asyncio.Event()
    conn = FakeConn(block=False)  # the receiver loop ends at once: Voice Live dropped

    def fake_connect(**kwargs):
        connected.set()
        return FakeCtx(conn)

    monkeypatch.setattr(vmh, "voicelive_connect", fake_connect)
    monkeypatch.setattr(vmh, "DefaultAzureCredential", FakeCredential)

    async with server.app.test_client().websocket("/twilio/ws") as ws:
        await ws.send(_start_msg(_token()))
        with pytest.raises(WebsocketDisconnectError):
            await asyncio.wait_for(ws.receive(), timeout=2)  # the server closed the socket
        await _wait_for_loop_exit(server)  # ...and the loop exited without the client disconnecting
    assert connected.is_set()
    assert "call_ended reason=voicelive_dropped" in logs.text


async def test_voicelive_connect_timeout_ends_the_twilio_call(load_server, monkeypatch, logs):
    server = load_server(**twilio_env(VOICE_LIVE_CONNECT_TIMEOUT_SECONDS="0.1"))

    class HangingCtx:
        async def __aenter__(self):
            await asyncio.sleep(3600)

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(vmh, "voicelive_connect", lambda **kwargs: HangingCtx())
    monkeypatch.setattr(vmh, "DefaultAzureCredential", FakeCredential)

    async with server.app.test_client().websocket("/twilio/ws") as ws:
        await ws.send(_start_msg(_token()))
        with pytest.raises(WebsocketDisconnectError):
            await asyncio.wait_for(ws.receive(), timeout=2)
        await _wait_for_loop_exit(server)
    assert "call_ended reason=voicelive_connect_failed" in logs.text


async def test_caller_hangup_mid_connect_leaves_no_orphaned_connection(load_server, sdk_like):
    server = load_server(**twilio_env())
    async with server.app.test_client().websocket("/twilio/ws") as ws:
        await ws.send(_start_msg(_token()))
        await asyncio.wait_for(sdk_like.entered.wait(), 2.0)
    # Leaving the block disconnects the caller and awaits the server handler to completion.
    sdk_like.release.set()
    await _settle()
    assert sdk_like.events[0] == "handshake_cancelled"
    assert "connected" not in sdk_like.events
    assert server.call_manager.active_count == 0


@pytest.mark.parametrize("path", ["route_miss", "bad_token"])
async def test_ws_early_exit_paths_still_clean_up_the_handler(load_server, monkeypatch, path):
    """The pre-call exits (bad token, route miss) must still release the handler's resources."""
    server = load_server(**twilio_env())
    cleanups = []
    real_cleanup = TwilioMediaHandler.cleanup

    async def spy(self):
        cleanups.append(1)
        await real_cleanup(self)

    monkeypatch.setattr(TwilioMediaHandler, "cleanup", spy)
    if path == "route_miss":
        unrouted = "+14165559999"
        token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(unrouted)
        start = _start_msg(token).replace(ROUTED, unrouted)
    else:
        start = _start_msg("0.bogus")
    with contextlib.suppress(WebsocketResponseError, WebsocketDisconnectError):
        async with server.app.test_client().websocket("/twilio/ws") as ws:
            await ws.send(start)
            await asyncio.wait_for(ws.receive(), timeout=2)
    assert cleanups == [1]


# --- dependency hygiene --------------------------------------------------------------------------


def test_twilio_path_does_not_import_acs_sdk():
    """close_voicelive now lives in a neutral module: Twilio's code path must not pull in the ACS SDK."""
    env = {k: v for k, v in os.environ.items() if k not in BRIDGE_ENV_KEYS}
    code = (
        "import sys\n"
        "import app.providers.twilio, app.providers.twilio.media_handler\n"
        "import app.providers.twilio.call_session, app.handler.voicelive_close\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('azure.communication', 'app.providers.acs')))\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_acs_call_session_still_exports_close_voicelive():
    from app.providers.acs import call_session as acs_call_session

    assert acs_call_session.close_voicelive is close_voicelive
