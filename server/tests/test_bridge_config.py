import pytest

from app.bridge_config import (
    DEFAULT_FALLBACK_MESSAGE,
    BridgeConfigError,
    load_bridge_config,
    parse_routing,
)
from app.routing import AgentRoute
from tests.helpers import TOKEN, VALID_ROUTING, acs_env


def test_valid_acs_config():
    cfg = load_bridge_config(acs_env(), acs_active=True)
    assert cfg.routes == {"+14165551234": AgentRoute("proj", "agent-a", "10")}
    assert cfg.media_ws_token == TOKEN
    assert cfg.fallback_message == DEFAULT_FALLBACK_MESSAGE
    assert cfg.max_call_seconds == 600
    assert cfg.enable_web_client is False


def test_routing_keys_are_normalized():
    routes = parse_routing('{"(416) 555-1234": {"project": "p", "agent": "a", "version": "10"}}')
    assert list(routes) == ["+14165551234"]


def test_raw_duplicate_keys_rejected():
    raw = (
        '{"+14165551234": {"project": "p", "agent": "a", "version": "10"},'
        ' "+14165551234": {"project": "p", "agent": "a", "version": "11"}}'
    )
    with pytest.raises(BridgeConfigError, match="duplicate"):
        parse_routing(raw)


def test_normalized_duplicate_keys_rejected():
    raw = (
        '{"+14165551234": {"project": "p", "agent": "a", "version": "10"},'
        ' "416-555-1234": {"project": "p", "agent": "a", "version": "10"}}'
    )
    with pytest.raises(BridgeConfigError, match="two keys"):
        parse_routing(raw)


@pytest.mark.parametrize("key", ["416555123", "+1416555123", "not-a-number"])
def test_invalid_e164_key_rejected(key):
    with pytest.raises(BridgeConfigError, match="E.164"):
        parse_routing('{"%s": {"project": "p", "agent": "a", "version": "10"}}' % key)


@pytest.mark.parametrize("version", ['"latest"', '"Latest"', '" latest "', '""', "10", "null"])
def test_unpinned_versions_rejected(version):
    with pytest.raises(BridgeConfigError, match="version"):
        parse_routing('{"+14165551234": {"project": "p", "agent": "a", "version": %s}}' % version)


def test_version_is_stripped():
    routes = parse_routing('{"+14165551234": {"project": "p", "agent": "a", "version": " 10 "}}')
    assert routes["+14165551234"].version == "10"


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"+14165551234": "x"}'])
def test_malformed_routing_rejected(raw):
    with pytest.raises(BridgeConfigError):
        parse_routing(raw)


def test_missing_project_rejected():
    with pytest.raises(BridgeConfigError, match="project"):
        parse_routing('{"+14165551234": {"agent": "a", "version": "10"}}')


def test_error_messages_mask_numbers():
    with pytest.raises(BridgeConfigError) as exc:
        parse_routing('{"+1416555123": {"project": "p", "agent": "a", "version": "10"}}')
    assert "416555123" not in str(exc.value)


@pytest.mark.parametrize(
    "missing", ["AGENT_ROUTING_JSON", "MEDIA_WS_TOKEN", "ACS_COGNITIVE_SERVICES_ENDPOINT"]
)
def test_acs_required_keys(missing):
    with pytest.raises(BridgeConfigError, match=missing):
        load_bridge_config(acs_env(**{missing: None}), acs_active=True)


def test_short_token_rejected():
    with pytest.raises(BridgeConfigError, match="MEDIA_WS_TOKEN"):
        load_bridge_config(acs_env(MEDIA_WS_TOKEN="short"), acs_active=True)


def test_acs_inactive_needs_nothing():
    cfg = load_bridge_config({}, acs_active=False)
    assert cfg.routes == {}
    assert cfg.media_ws_token == ""


def test_spec_names_win_over_accelerator_names():
    cfg = load_bridge_config(
        acs_env(
            MAX_CALL_SECONDS="60",
            MAX_CALL_DURATION="3600",
            VOICE_LIVE_ENDPOINT="https://spec.example",
            AZURE_VOICE_LIVE_ENDPOINT="https://accel.example",
        ),
        acs_active=True,
    )
    assert cfg.max_call_seconds == 60
    assert cfg.voice_live_endpoint == "https://spec.example"


def test_accelerator_names_used_as_fallback():
    cfg = load_bridge_config(
        acs_env(MAX_CALL_DURATION="900", AZURE_VOICE_LIVE_ENDPOINT="https://accel.example"),
        acs_active=True,
    )
    assert cfg.max_call_seconds == 900
    assert cfg.voice_live_endpoint == "https://accel.example"


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_bad_numbers_rejected(value):
    with pytest.raises(BridgeConfigError, match="MAX_CALL_SECONDS"):
        load_bridge_config(acs_env(MAX_CALL_SECONDS=value), acs_active=True)


def test_optional_fields():
    cfg = load_bridge_config(
        acs_env(
            ENABLE_WEB_CLIENT="TRUE",
            INTERIM_RESPONSE_JSON='{"type": "llm_interim_response"}',
            VOICE_LIVE_API_VERSION="2026-07-15",
            ACS_CALLBACK_JWT_AUDIENCE="acs-resource-id",
        ),
        acs_active=True,
    )
    assert cfg.enable_web_client is True
    assert cfg.interim_response == {"type": "llm_interim_response"}
    assert cfg.voice_live_api_version == "2026-07-15"
    assert cfg.callback_jwt_audience == "acs-resource-id"


def test_interim_response_must_be_object():
    with pytest.raises(BridgeConfigError, match="INTERIM_RESPONSE_JSON"):
        load_bridge_config(acs_env(INTERIM_RESPONSE_JSON="[1]"), acs_active=True)


def test_routes_mapping_is_read_only():
    cfg = load_bridge_config(acs_env(), acs_active=True)
    with pytest.raises(TypeError):
        cfg.routes["+19999999999"] = AgentRoute("x", "y", "1")  # type: ignore[index]


def test_valid_routing_constant_is_valid():
    assert parse_routing(VALID_ROUTING)
