"""UT01a: Twilio agent routing — a matched call reaches Voice Live in agent mode.

Covers /voice (POST-only, route hit/miss TwiML), the called-number-bound WS token, the
/twilio/ws route resolution, and the startup gate that requires AGENT_ROUTING_JSON when
Twilio is the active provider.
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from quart.testing import WebsocketResponseError
from quart.testing.connections import WebsocketDisconnectError
from twilio.request_validator import RequestValidator

import app.handler.voicelive_media_handler as vmh
from app.bridge_config import BridgeConfigError, load_bridge_config
from app.providers.twilio.event_handler import TwilioEventHandler
from app.providers.twilio.media_handler import TwilioMediaHandler
from tests.helpers import BRIDGE_ENV_KEYS, VALID_ROUTING, acs_env

SERVER_DIR = Path(__file__).resolve().parent.parent
AUTH = "twilio-test-auth-token"
ROUTED = "+14165551234"  # the key in VALID_ROUTING
UNROUTED = "+14165559999"
VOICE_URL = "https://localhost/voice"  # how TwilioEventHandler reconstructs the test client's URL


def twilio_env(**overrides):
    env = {"TWILIO_AUTH_TOKEN": AUTH, "AGENT_ROUTING_JSON": VALID_ROUTING}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def twilio_server(load_server):
    return load_server(**twilio_env())


def _signed_post(client, params, url=VOICE_URL):
    signature = RequestValidator(AUTH).compute_signature(url, params)
    return client.post("/voice", form=params, headers={"X-Twilio-Signature": signature})


def _stream_params(twiml: str) -> dict:
    root = ET.fromstring(twiml)
    stream = root.find("./Connect/Stream")
    assert stream is not None
    return {p.get("name"): p.get("value") for p in stream.findall("Parameter")}


def _handler(auth=AUTH):
    return TwilioMediaHandler({
        "AZURE_VOICE_LIVE_ENDPOINT": "https://vl.example",
        "VOICE_LIVE_MODEL": "gpt-4o-mini",
        "AZURE_VOICE_LIVE_API_KEY": "key",
        "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID": "",
        "AMBIENT_PRESET": "none",
        "TWILIO_AUTH_TOKEN": auth,
    })


# --- /voice ------------------------------------------------------------------------------------


async def test_voice_rejects_get(twilio_server):
    client = twilio_server.app.test_client()
    resp = await client.get(f"/voice?To={ROUTED}")
    assert resp.status_code == 405


async def test_voice_bad_signature_rejected(twilio_server):
    client = twilio_server.app.test_client()
    resp = await client.post("/voice", form={"To": ROUTED}, headers={"X-Twilio-Signature": "bogus"})
    assert resp.status_code == 403


async def test_voice_route_hit_connects_stream_with_bound_called_number(twilio_server):
    client = twilio_server.app.test_client()
    resp = await _signed_post(client, {"To": ROUTED, "From": "+16475559876"})
    assert resp.status_code == 200
    twiml = (await resp.get_data()).decode()
    root = ET.fromstring(twiml)
    assert root.find("./Connect/Stream").get("url") == "wss://localhost/twilio/ws"
    params = _stream_params(twiml)
    assert params["calledNumber"] == ROUTED
    # The token handed to Twilio verifies for exactly this called number, and no other.
    handler = _handler()
    assert handler._verify_ws_token(params["token"], ROUTED)
    assert not handler._verify_ws_token(params["token"], UNROUTED)


async def test_voice_route_hit_normalizes_called_number(twilio_server):
    client = twilio_server.app.test_client()
    resp = await _signed_post(client, {"To": "+1 (416) 555-1234"})
    assert _stream_params((await resp.get_data()).decode())["calledNumber"] == ROUTED


async def test_voice_route_miss_says_fallback_and_never_streams(load_server, logs):
    server = load_server(**twilio_env(FALLBACK_MESSAGE="No agent for this number."))
    client = server.app.test_client()
    resp = await _signed_post(client, {"To": UNROUTED, "From": "+16475559876"})
    assert resp.status_code == 200
    root = ET.fromstring((await resp.get_data()).decode())
    assert root.find("./Connect") is None
    assert root.find(".//Stream") is None
    assert root.find("./Say").text == "No agent for this number."
    assert root.find("./Hangup") is not None
    # D-006: the called number is only ever logged masked.
    assert UNROUTED not in logs.text
    assert "***9999" in logs.text


async def test_voice_missing_to_is_a_route_miss(twilio_server):
    client = twilio_server.app.test_client()
    resp = await _signed_post(client, {"From": "+16475559876"})
    root = ET.fromstring((await resp.get_data()).decode())
    assert root.find(".//Stream") is None
    assert root.find("./Say") is not None


# --- WS token binding --------------------------------------------------------------------------


def test_ws_token_bound_to_called_number():
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)
    handler = _handler()
    assert handler._verify_ws_token(token, ROUTED)
    assert not handler._verify_ws_token(token, UNROUTED)
    assert not handler._verify_ws_token(token, "")
    assert not handler._verify_ws_token(token, None)


def test_ws_token_signed_with_other_auth_token_rejected():
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": "other"})._generate_ws_token(ROUTED)
    assert not _handler()._verify_ws_token(token, ROUTED)


def test_ws_token_expired_rejected(monkeypatch):
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 61)
    assert not _handler()._verify_ws_token(token, ROUTED)


def test_ws_token_timestamp_only_scheme_rejected():
    # A token in the old (upstream) format — HMAC over the timestamp alone — must not verify.
    import hashlib
    import hmac

    ts = str(int(time.time()))
    old = f"{ts}.{hmac.new(AUTH.encode(), ts.encode(), hashlib.sha256).hexdigest()}"
    assert not _handler()._verify_ws_token(old, ROUTED)


# --- /twilio/ws --------------------------------------------------------------------------------


@pytest.fixture
def fake_voicelive(monkeypatch):
    """Capture the kwargs /twilio/ws's handler passes to the Voice Live SDK's connect()."""
    from tests.test_voicelive_handler import FakeConn, FakeCredential, FakeCtx

    captured = {"conn": FakeConn()}
    connected = asyncio.Event()

    def fake_connect(**kwargs):
        captured["kwargs"] = kwargs
        connected.set()
        return FakeCtx(captured["conn"])

    monkeypatch.setattr(vmh, "voicelive_connect", fake_connect)
    monkeypatch.setattr(vmh, "DefaultAzureCredential", FakeCredential)
    captured["connected"] = connected
    return captured


def _start_msg(token, called_number):
    params = {"token": token}
    if called_number is not None:
        params["calledNumber"] = called_number
    return json.dumps({
        "event": "start",
        "streamSid": "MZ-test",
        "start": {"callSid": "CA-test", "customParameters": params, "mediaFormat": {}},
    })


async def _run_ws(client, token, called_number, wait_for=None):
    with contextlib.suppress(WebsocketResponseError, WebsocketDisconnectError):
        async with client.websocket("/twilio/ws") as ws:
            await ws.send(json.dumps({"event": "connected", "protocol": "Call"}))
            await ws.send(_start_msg(token, called_number))
            if wait_for is not None:
                await asyncio.wait_for(wait_for.wait(), timeout=2)
                await asyncio.sleep(0.05)  # let session.update/response.create run
            else:
                # The server must close the socket; a TimeoutError here fails the test.
                await asyncio.wait_for(ws.receive(), timeout=2)


async def test_ws_matched_route_reaches_agent_mode(twilio_server, fake_voicelive):
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)
    await _run_ws(twilio_server.app.test_client(), token, ROUTED, wait_for=fake_voicelive["connected"])
    kwargs = fake_voicelive["kwargs"]
    assert kwargs["agent_name"] == "agent-a"
    assert kwargs["project_name"] == "proj"
    assert kwargs["agent_version"] == "10"
    assert "model" not in kwargs
    sent = fake_voicelive["conn"].session.update.call_args.kwargs["session"].as_dict()
    assert "instructions" not in sent and "voice" not in sent  # D-004: agent-mode payload


async def test_ws_token_replayed_with_other_called_number_rejected(twilio_server, fake_voicelive):
    # A token minted for UNROUTED, presented alongside the routed number, must be rejected.
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(UNROUTED)
    await _run_ws(twilio_server.app.test_client(), token, ROUTED)
    assert "kwargs" not in fake_voicelive


async def test_ws_missing_called_number_rejected(twilio_server, fake_voicelive):
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)
    await _run_ws(twilio_server.app.test_client(), token, None)
    assert "kwargs" not in fake_voicelive


async def test_ws_route_miss_closes_without_opening_voicelive(twilio_server, fake_voicelive, logs):
    # A validly signed token for a number with no route (e.g. routing changed between /voice and
    # the WS connecting) must close the socket, never fall into non-agent mode (D-004).
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(UNROUTED)
    await _run_ws(twilio_server.app.test_client(), token, UNROUTED)
    assert "kwargs" not in fake_voicelive
    assert UNROUTED not in logs.text


def test_ws_token_non_ascii_input_rejected_not_raised():
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)
    ts = token.split(".", 1)[0]
    handler = _handler()
    assert handler._verify_ws_token(f"{ts}.éé", ROUTED) is False
    assert handler._verify_ws_token(token, "+1416555\ud8001234") is False


@pytest.mark.parametrize("variant", ["non_ascii_sig", "surrogate_number"])
async def test_ws_non_ascii_token_input_closes_4403(twilio_server, fake_voicelive, variant):
    token = TwilioEventHandler({"TWILIO_AUTH_TOKEN": AUTH})._generate_ws_token(ROUTED)
    if variant == "non_ascii_sig":
        token, called = token.split(".", 1)[0] + ".éé", ROUTED
    else:
        called = "+1416555\ud8001234"
    # json.dumps escapes the lone surrogate as \ud800, which json.loads restores server-side.
    async with twilio_server.app.test_client().websocket("/twilio/ws") as ws:
        await ws.send(_start_msg(token, called))
        with pytest.raises(WebsocketDisconnectError) as exc:
            await asyncio.wait_for(ws.receive(), timeout=2)
    assert exc.value.args[0] == 4403  # quart's test client carries the close code in args[0]
    assert "kwargs" not in fake_voicelive


async def test_ws_malformed_start_rejected(twilio_server, fake_voicelive):
    with contextlib.suppress(WebsocketResponseError, WebsocketDisconnectError):
        async with twilio_server.app.test_client().websocket("/twilio/ws") as ws:
            await ws.send(json.dumps({"event": "start", "start": {"customParameters": "nope"}}))
            await asyncio.wait_for(ws.receive(), timeout=2)
    assert "kwargs" not in fake_voicelive


# --- startup gate ------------------------------------------------------------------------------


def test_twilio_without_routing_fails_startup(load_server, logs):
    # Exercises server.py's real startup path, not load_bridge_config() alone, so a break in
    # the wiring between the two would fail this test.
    with pytest.raises(SystemExit) as exc_info:
        load_server(TWILIO_AUTH_TOKEN=AUTH)
    assert exc_info.value.code == 1
    assert "Bridge configuration error" in logs.text
    assert "AGENT_ROUTING_JSON is required when Twilio is configured" in logs.text


def test_twilio_with_routing_starts_and_registers_voice(twilio_server):
    rules = {rule.rule: rule for rule in twilio_server.app.url_map.iter_rules()}
    assert "/voice" in rules
    assert "GET" not in rules["/voice"].methods
    assert "/twilio/ws" in rules


def test_acs_and_twilio_both_set_acs_wins():
    # ACS registers first in a real startup, so it wins detection; server.py's Twilio gate must
    # agree. Run in a fresh interpreter: provider detection order is process-global registration
    # order, and this test module has already imported the Twilio package (registering it first).
    env = {k: v for k, v in os.environ.items() if k not in BRIDGE_ENV_KEYS}
    env.update(acs_env(TWILIO_AUTH_TOKEN=AUTH))
    env.update({"AZURE_VOICE_LIVE_ENDPOINT": "https://vl.example", "AZURE_VOICE_LIVE_API_KEY": "key"})
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
        "import server\n"
        "print(sorted({r.rule for r in server.app.url_map.iter_rules()}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    rules = out.stdout
    assert "'/voice'" not in rules
    assert "'/acs/incomingcall'" in rules


def test_load_bridge_config_twilio_gate():
    with pytest.raises(BridgeConfigError, match="AGENT_ROUTING_JSON is required when Twilio"):
        load_bridge_config({}, acs_active=False, twilio_active=True)
    assert load_bridge_config({}, acs_active=False, twilio_active=False).routes == {}
    cfg = load_bridge_config({"AGENT_ROUTING_JSON": VALID_ROUTING}, acs_active=False, twilio_active=True)
    assert ROUTED in cfg.routes
