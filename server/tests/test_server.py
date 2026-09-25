import pytest


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
