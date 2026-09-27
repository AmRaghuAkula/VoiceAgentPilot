import asyncio
import contextlib
import time
from types import SimpleNamespace

import jwt
import pytest
from azure.communication.callautomation.aio import CallAutomationClient
from cryptography.hazmat.primitives.asymmetric import rsa
from quart.testing import WebsocketResponseError
from quart.testing.connections import WebsocketDisconnectError

import app.handler.voicelive_media_handler as vmh
import app.providers.acs as acs_pkg
from app.providers.acs.call_session import CallSession
from app.providers.acs.callback_auth import ACS_CALLBACK_ISSUER
from app.providers.acs.media_handler import ACSMediaHandler
from app.providers.acs.signing import sign
from app.routing import AgentRoute
from tests.helpers import TOKEN, acs_env

SILENT_FRAME = '{"kind": "AudioData", "audioData": {"data": "", "silent": true}}'


@pytest.fixture
def make_acs_server(load_server, monkeypatch, fake_acs):
    monkeypatch.setattr(CallAutomationClient, "from_connection_string", classmethod(lambda cls, cs: fake_acs))

    def _make(**env_overrides):
        return _wire(load_server(**acs_env(**env_overrides)), fake_acs)

    return _make


@pytest.fixture
def acs_server(make_acs_server):
    return make_acs_server()


def _wire(server, fake_acs):
    registry = server.app.config["ACS_CALL_REGISTRY"]
    bridge = server.app.config["BRIDGE"]

    def new_session():
        from app.providers.acs.bridge_calls import settings_from_bridge

        session = CallSession(
            call_key="a" * 32, route=AgentRoute("proj", "agent-a", "10"), masked_caller="***9876",
            masked_called="***1234", settings=settings_from_bridge(bridge), acs_client=fake_acs, registry=registry,
        )
        registry.add(session)
        return session

    server.new_session = new_session
    return server


async def test_callback_with_bad_signature_rejected_without_state_change(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{'0' * 64}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
    )
    assert resp.status_code == 403
    assert not session._connected.is_set()


async def test_callback_with_good_signature_dispatches(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
    )
    assert resp.status_code == 200
    assert session._connected.is_set()


async def test_media_ws_signature_is_not_a_callback_signature(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}", json=[]
    )
    assert resp.status_code == 403


async def _open_ws(client, path):
    async with client.websocket(path) as ws:
        await ws.send(SILENT_FRAME)
        await asyncio.sleep(0.05)


async def test_ws_unknown_key_rejected(acs_server):
    client = acs_server.app.test_client()
    with pytest.raises(WebsocketResponseError) as exc:
        await _open_ws(client, f"/acs/ws/{'b' * 32}/{sign(TOKEN, 'ws', 'b' * 32)}")
    assert exc.value.response.status_code == 403


async def test_ws_bad_signature_rejected(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    with pytest.raises(WebsocketResponseError):
        await _open_ws(client, f"/acs/ws/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}")
    assert session.ws_used is False


async def test_ws_reused_or_terminated_rejected(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    path = f"/acs/ws/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}"
    session.ws_used = True
    with pytest.raises(WebsocketResponseError):
        await _open_ws(client, path)
    session.ws_used = False
    session.terminated_reason = "media_timeout"
    with pytest.raises(WebsocketResponseError):
        await _open_ws(client, path)


async def test_ws_valid_signature_attaches_handler_and_never_logs_signature(acs_server, monkeypatch, logs):
    async def idle_connect(self):
        await asyncio.sleep(10)

    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", idle_connect)
    session = acs_server.new_session()
    sig = sign(TOKEN, "ws", session.call_key)
    client = acs_server.app.test_client()
    await _open_ws(client, f"/acs/ws/{session.call_key}/{sig}")
    assert session.ws_used is True
    assert session.handler is not None
    assert sig not in logs.text
    assert TOKEN not in logs.text


async def test_validation_handshake_echoes_code(acs_server):
    client = acs_server.app.test_client()
    resp = await client.post(
        "/acs/incomingcall",
        json=[{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "abc123"}}],
    )
    assert resp.status_code == 200
    assert await resp.get_json() == {"validationResponse": "abc123"}


async def test_incoming_call_route_answers(acs_server, fake_acs):
    client = acs_server.app.test_client()
    resp = await client.post(
        "/acs/incomingcall",
        json=[{
            "eventType": "Microsoft.Communication.IncomingCall",
            "data": {
                "to": {"kind": "phoneNumber", "phoneNumber": {"value": "+14165551234"}},
                "from": {"kind": "phoneNumber", "phoneNumber": {"value": "+16475559876"}},
                "incomingCallContext": "ctx",
            },
        }],
    )
    assert resp.status_code == 200
    assert fake_acs.answer_kwargs["callback_url"].startswith("https://")


# --- callback JWT enforced at the route level -------------------------------------------------

JWT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    def __init__(self, *args, **kwargs):
        pass

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=JWT_KEY.public_key())


def _jwt(**claims):
    body = {"aud": "acs-id", "iss": ACS_CALLBACK_ISSUER, "exp": int(time.time()) + 60}
    body.update(claims)
    return jwt.encode(body, JWT_KEY, algorithm="RS256")


@pytest.fixture
def jwt_server(make_acs_server, monkeypatch):
    monkeypatch.setattr(jwt, "PyJWKClient", _FakeJwks)
    return make_acs_server(ACS_CALLBACK_JWT_AUDIENCE="acs-id")


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": f"Bearer {_jwt(aud='other')}"}],
)
async def test_callback_jwt_required_when_audience_set(jwt_server, headers):
    session = jwt_server.new_session()
    client = jwt_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
        headers=headers,
    )
    assert resp.status_code == 403
    assert not session._connected.is_set()


async def test_callback_with_valid_jwt_and_signature_dispatches(jwt_server):
    session = jwt_server.new_session()
    client = jwt_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
        headers={"Authorization": f"Bearer {_jwt()}"},
    )
    assert resp.status_code == 200
    assert session._connected.is_set()


async def test_valid_jwt_does_not_bypass_bad_signature(jwt_server):
    session = jwt_server.new_session()
    client = jwt_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{'0' * 64}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
        headers={"Authorization": f"Bearer {_jwt()}"},
    )
    assert resp.status_code == 403
    assert not session._connected.is_set()


# --- media websocket lifecycle ----------------------------------------------------------------


async def test_ws_at_capacity_ends_call_with_fallback(make_acs_server):
    server = make_acs_server(MAX_CONCURRENT_CALLS="0")
    session = server.new_session()
    client = server.app.test_client()
    path = f"/acs/ws/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}"
    with contextlib.suppress(WebsocketResponseError, WebsocketDisconnectError):
        await _open_ws(client, path)
    assert session.ws_used is True
    assert session.terminated_reason == "at_capacity"
    assert session.handler is None
    assert server.call_manager.active_count == 0


async def test_ws_close_releases_slot_and_closes_voicelive(acs_server, monkeypatch):
    async def idle_connect(self):
        await asyncio.sleep(10)

    cleaned = []

    async def record_cleanup(self):
        cleaned.append(self)

    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", idle_connect)
    monkeypatch.setattr(ACSMediaHandler, "cleanup", record_cleanup)
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    await _open_ws(client, f"/acs/ws/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}")
    for _ in range(100):
        if cleaned and acs_server.call_manager.active_count == 0:
            break
        await asyncio.sleep(0.01)
    assert cleaned == [session.handler]
    assert acs_server.call_manager.active_count == 0


# --- stale-session sweeper --------------------------------------------------------------------


async def test_sweeper_survives_a_failing_sweep_and_stops_on_shutdown(acs_server, monkeypatch, logs):
    monkeypatch.setattr(acs_pkg, "SWEEP_INTERVAL_SECONDS", 0.01)
    app = acs_server.app
    registry = app.config["ACS_CALL_REGISTRY"]
    calls = []

    def flaky_sweep(max_age, now=None):
        calls.append(max_age)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(registry, "sweep", flaky_sweep)
    async with app.test_app():
        task = app.config["ACS_SWEEPER"]
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        assert not task.done()
    assert len(calls) >= 3
    assert calls[0] == app.config["BRIDGE"].max_call_seconds + 120
    assert "stale_call_sweep_failed" in logs.text
    assert task.cancelled()
    assert "ACS_SWEEPER" not in app.config


async def test_sweeper_removes_stale_sessions(acs_server, monkeypatch):
    monkeypatch.setattr(acs_pkg, "SWEEP_INTERVAL_SECONDS", 0.01)
    app = acs_server.app
    registry = app.config["ACS_CALL_REGISTRY"]
    session = acs_server.new_session()
    session.created_at -= app.config["BRIDGE"].max_call_seconds + 1000
    async with app.test_app():
        for _ in range(200):
            if registry.get(session.call_key) is None:
                break
            await asyncio.sleep(0.01)
    assert registry.get(session.call_key) is None
    assert session.terminated_reason == "stale"
