import pytest

from tests.helpers import acs_env


def test_webclient_and_telephony_both_active_refuses_to_start(load_server, logs):
    # D-028: web client on + a telephony provider configured must fail startup, not just warn.
    with pytest.raises(SystemExit) as exc_info:
        load_server(ENABLE_WEB_CLIENT="true", **acs_env())
    assert exc_info.value.code == 1
    assert "Refusing to start" in logs.text
    assert "D-028" in logs.text


def test_webclient_and_other_provider_both_active_refuses_to_start(load_server):
    # Same guard, exercised via a different provider's detect key (not just ACS), so a future
    # refactor can't quietly narrow the check to be ACS-specific.
    with pytest.raises(SystemExit) as exc_info:
        load_server(ENABLE_WEB_CLIENT="true", TWILIO_AUTH_TOKEN="fake-token")
    assert exc_info.value.code == 1


def test_webclient_alone_with_no_provider_still_starts(load_server):
    # The normal local-dev-only debug case: web client on, nothing else configured.
    server = load_server(ENABLE_WEB_CLIENT="true")
    assert "web_ws" in {rule.endpoint for rule in server.app.url_map.iter_rules()}


def test_telephony_alone_with_webclient_off_still_starts(load_server):
    # Today's normal deployed case: a real provider, web client off.
    server = load_server(**acs_env())
    assert "web_ws" not in {rule.endpoint for rule in server.app.url_map.iter_rules()}


async def test_neither_webclient_nor_telephony_still_starts(load_server):
    # Edge case: nothing configured at all.
    server = load_server()
    assert "web_ws" not in {rule.endpoint for rule in server.app.url_map.iter_rules()}
    assert (await server.app.test_client().get("/health")).status_code == 200


async def test_web_client_disabled_by_default(load_server):
    server = load_server()
    client = server.app.test_client()
    assert (await client.get("/")).status_code == 404
    assert (await client.get("/health")).status_code == 200
    assert "web_ws" not in {rule.endpoint for rule in server.app.url_map.iter_rules()}
    # The static folder itself must not be exposed while the web client is off (D-007):
    # its debug HTML/JS would otherwise be readable even with /web/ws unregistered.
    assert (await client.get("/static/index.html")).status_code == 404


async def test_web_client_enabled_explicitly(load_server):
    server = load_server(ENABLE_WEB_CLIENT="true")
    client = server.app.test_client()
    assert (await client.get("/")).status_code == 200
    assert "web_ws" in {rule.endpoint for rule in server.app.url_map.iter_rules()}
    # The static assets the web client's page depends on must be reachable too, not just "/".
    assert (await client.get("/static/audio-processor.js")).status_code == 200


def test_spec_cap_name_reaches_call_manager(load_server):
    server = load_server(MAX_CALL_SECONDS="60", MAX_CALL_DURATION="3600")
    assert server.call_manager.get_stats()["max_call_duration_s"] == 60


def test_spec_endpoint_name_reaches_app_config(load_server):
    server = load_server(VOICE_LIVE_ENDPOINT="https://spec.example")
    assert server.app.config["AZURE_VOICE_LIVE_ENDPOINT"] == "https://spec.example"
    assert server.app.config["BRIDGE"].voice_live_endpoint == "https://spec.example"


def test_bad_bridge_config_exits(load_server, logs):
    # ACS is active (ACS_CONNECTION_STRING is set) but AGENT_ROUTING_JSON/MEDIA_WS_TOKEN/
    # ACS_COGNITIVE_SERVICES_ENDPOINT are missing, so load_bridge_config() itself must be
    # what raises — not some unrelated exit path (e.g. upstream's own provider validation).
    with pytest.raises(SystemExit) as exc_info:
        load_server(ACS_CONNECTION_STRING="endpoint=https://x/;accesskey=eA==")
    assert exc_info.value.code == 1
    assert "Bridge configuration error" in logs.text
