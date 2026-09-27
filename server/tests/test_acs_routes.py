import asyncio

import pytest
from azure.communication.callautomation.aio import CallAutomationClient
from quart.testing import WebsocketResponseError

import app.handler.voicelive_media_handler as vmh
from app.providers.acs.call_session import CallSession
from app.providers.acs.signing import sign
from app.routing import AgentRoute
from tests.helpers import TOKEN, acs_env

SILENT_FRAME = '{"kind": "AudioData", "audioData": {"data": "", "silent": true}}'


@pytest.fixture
def acs_server(load_server, monkeypatch, fake_acs):
    monkeypatch.setattr(CallAutomationClient, "from_connection_string", classmethod(lambda cls, cs: fake_acs))
    server = load_server(**acs_env())
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
