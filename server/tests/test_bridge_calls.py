import asyncio

import pytest

from app.bridge_config import load_bridge_config
from app.providers.acs.bridge_calls import BridgeCallController
from app.providers.acs.call_session import CallSessionRegistry
from app.providers.acs.signing import sign
from tests.helpers import TOKEN, acs_env

HOST = "https://bridge.example"


def incoming(to_number="+14165551234", context="ctx-1", to=None):
    data = {
        "to": to if to is not None else {"kind": "phoneNumber", "phoneNumber": {"value": to_number}},
        "from": {"kind": "phoneNumber", "phoneNumber": {"value": "+16475559876"}},
    }
    if context is not None:
        data["incomingCallContext"] = context
    return [{"eventType": "Microsoft.Communication.IncomingCall", "data": data}]


def make(fake_acs):
    bridge = load_bridge_config(acs_env(), acs_active=True)
    registry = CallSessionRegistry()
    return BridgeCallController(acs_client=fake_acs, bridge=bridge, registry=registry), registry


async def test_subscription_validation(fake_acs):
    controller, _ = make(fake_acs)
    body, status = await controller.handle_incoming(
        [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "abc"}}], HOST
    )
    assert (body, status) == ({"validationResponse": "abc"}, 200)


async def test_route_hit_answers_with_signed_media_and_no_numbers_in_urls(fake_acs):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(), HOST)
    assert status == 200
    (session,) = [registry.get(k) for k in list(registry._by_key)]
    kwargs = fake_acs.answer_kwargs
    ws_url = kwargs["media_streaming"].transport_url
    assert ws_url == f"wss://bridge.example/acs/ws/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}"
    assert kwargs["callback_url"] == f"{HOST}/acs/callbacks/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}"
    assert kwargs["cognitive_services_endpoint"] == "https://cog.example"
    for url in (ws_url, kwargs["callback_url"]):
        assert "4165551234" not in url and "6475559876" not in url and TOKEN not in url
    assert session.call_connection_id == "conn-1"
    assert session.route.agent == "agent-a"


async def test_session_inserted_before_answer_call(fake_acs):
    controller, registry = make(fake_acs)
    seen = []
    fake_acs.on_answer = lambda: seen.append(len(registry))
    await controller.handle_incoming(incoming(), HOST)
    assert seen == [1]


async def test_route_miss_answers_without_media_and_plays_fallback(fake_acs):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(to_number="+14165550000"), HOST)
    assert "media_streaming" not in fake_acs.answer_kwargs
    (key,) = list(registry._by_key)
    controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallConnected", "data": {}}])
    await asyncio.sleep(0.02)
    assert fake_acs.log == [("play", controller.bridge.fallback_message)]


async def test_non_phone_called_identity_is_route_miss(fake_acs):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(to={"kind": "communicationUser", "rawId": "8:acs:res_user"}), HOST)
    (session,) = list(registry._by_key.values())
    assert session.route is None
    assert "media_streaming" not in fake_acs.answer_kwargs


async def test_missing_incoming_call_context_is_400_without_session(fake_acs):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(context=None), HOST)
    assert status == 400
    assert len(registry) == 0
    assert fake_acs.answer_kwargs is None


async def test_answer_failure_removes_session(fake_acs):
    fake_acs.answer_error = RuntimeError("boom")
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(), HOST)
    assert status == 200
    assert len(registry) == 0


async def test_unknown_event_type_is_400(fake_acs):
    controller, _ = make(fake_acs)
    _, status = await controller.handle_incoming([{"eventType": "Other", "data": {}}], HOST)
    assert status == 400


async def test_callbacks_for_unknown_key_or_empty_body_do_not_raise(fake_acs):
    controller, _ = make(fake_acs)
    controller.handle_callbacks("nope", [{"type": "Microsoft.Communication.CallConnected"}])
    controller.handle_callbacks("nope", None)


async def test_callback_dispatch(fake_acs):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(), HOST)
    (key,) = list(registry._by_key)
    session = registry.get(key)
    controller.handle_callbacks(key, None)
    controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallConnected", "data": {}}])
    assert session._connected.is_set()
    controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallDisconnected", "data": {}}])
    await asyncio.sleep(0.02)
    assert session.terminated_reason == "caller_hangup"
    assert len(registry) == 0


async def test_logs_never_contain_full_numbers(fake_acs, logs):
    controller, _ = make(fake_acs)
    await controller.handle_incoming(incoming(), HOST)
    assert "4165551234" not in logs.text
    assert "6475559876" not in logs.text
    assert "***1234" in logs.text


async def test_dev_tunnel_override(fake_acs):
    bridge = load_bridge_config(acs_env(), acs_active=True)
    controller = BridgeCallController(
        acs_client=fake_acs, bridge=bridge, registry=CallSessionRegistry(),
        public_base_url_override="https://tunnel.example/",
    )
    await controller.handle_incoming(incoming(), HOST)
    assert fake_acs.answer_kwargs["callback_url"].startswith("https://tunnel.example/acs/callbacks/")
    assert fake_acs.answer_kwargs["media_streaming"].transport_url.startswith("wss://tunnel.example/acs/ws/")


# --- malformed input (review finding: the controller must never raise on bad payloads) ---

MALFORMED_INCOMING = [
    None,
    {"a": 1},
    "x",
    ["x", 1, None],
    [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent"}],
    [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": None}],
    [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": 5}}],
    [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": ""}}],
    [{"eventType": "Microsoft.Communication.IncomingCall", "data": "str"}],
    [{"eventType": "Microsoft.Communication.IncomingCall", "data": None}],
]


@pytest.mark.parametrize("body", MALFORMED_INCOMING)
async def test_malformed_incoming_body_is_400_without_session(fake_acs, body):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(body, HOST)
    assert status == 400
    assert len(registry) == 0
    assert fake_acs.answer_kwargs is None


async def test_non_string_incoming_call_context_is_400_without_session(fake_acs):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(context={"not": "a string"}), HOST)
    assert status == 400
    assert len(registry) == 0
    assert fake_acs.answer_kwargs is None


@pytest.mark.parametrize("events", [None, {"type": "Microsoft.Communication.CallConnected"}, "x",
                                    ["x", 1, None], [{"data": {}}]])
async def test_malformed_callbacks_for_known_key_do_not_raise_or_change_state(fake_acs, events):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(), HOST)
    (key,) = list(registry._by_key)
    session = registry.get(key)
    controller.handle_callbacks(key, events)
    await asyncio.sleep(0.02)
    assert not session._connected.is_set()
    assert session.terminated_reason is None
    assert registry.get(key) is session
    assert fake_acs.log == []


# --- base URL handling (review finding: broken URLs from a bad override) ---

@pytest.mark.parametrize("override", ["tunnel.example", "tunnel.example/path", "ftp://tunnel.example",
                                      "https://", "https://tunnel.example/?x=1"])
def test_invalid_override_is_rejected_at_construction(fake_acs, override):
    bridge = load_bridge_config(acs_env(), acs_active=True)
    with pytest.raises(ValueError):
        BridgeCallController(acs_client=fake_acs, bridge=bridge, registry=CallSessionRegistry(),
                             public_base_url_override=override)


async def test_override_with_path_keeps_path_in_both_urls(fake_acs):
    bridge = load_bridge_config(acs_env(), acs_active=True)
    controller = BridgeCallController(
        acs_client=fake_acs, bridge=bridge, registry=CallSessionRegistry(),
        public_base_url_override="https://tunnel.example/bridge/",
    )
    await controller.handle_incoming(incoming(), HOST)
    assert fake_acs.answer_kwargs["callback_url"].startswith("https://tunnel.example/bridge/acs/callbacks/")
    assert fake_acs.answer_kwargs["media_streaming"].transport_url.startswith("wss://tunnel.example/bridge/acs/ws/")


@pytest.mark.parametrize("host_url", ["", None, "bridge.example"])
async def test_invalid_host_url_is_400_without_session(fake_acs, host_url):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(), host_url)
    assert status == 400
    assert len(registry) == 0
    assert fake_acs.answer_kwargs is None


# --- race: caller hangs up while answer_call is still in flight ---

async def test_disconnect_during_answer_leaves_no_connection_index(fake_acs):
    controller, registry = make(fake_acs)
    real_answer = fake_acs.answer_call

    async def answer_with_disconnect_racing_ahead(**kwargs):
        (key,) = list(registry._by_key)
        controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallDisconnected", "data": {}}])
        await asyncio.sleep(0.02)  # let the session finalize (and leave the registry) before answer returns
        assert len(registry) == 0
        return await real_answer(**kwargs)

    fake_acs.answer_call = answer_with_disconnect_racing_ahead
    await controller.handle_incoming(incoming(), HOST)
    await asyncio.sleep(0.02)
    assert len(registry) == 0
    assert registry._by_conn == {}
    assert registry.get_by_connection("conn-1") is None
