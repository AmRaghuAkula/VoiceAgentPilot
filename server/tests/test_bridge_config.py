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


@pytest.mark.parametrize("key", ["416555123", "+1416555123", "not-a-number", "1416555123"])
def test_invalid_e164_key_rejected(key):
    with pytest.raises(BridgeConfigError, match="E.164"):
        parse_routing('{"%s": {"project": "p", "agent": "a", "version": "10"}}' % key)


def test_dropped_digit_nanp_key_rejected_end_to_end():
    """D-022: a bare 11-digit key with a dropped digit must not silently start up (U03's own bug class)."""
    with pytest.raises(BridgeConfigError, match="E.164"):
        load_bridge_config(acs_env(AGENT_ROUTING_JSON='{"1416555123": {"project": "p", "agent": "a", "version": "10"}}'), acs_active=True)


@pytest.mark.parametrize(
    "version",
    [
        '"latest"', '"Latest"', '" latest "', '""', "10", "null",
        '"0"', '"007"', '"00"',
        '"١٢"',  # Arabic-Indic digits
        '"１０"',  # fullwidth 10
    ],
)
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


def test_whitespace_only_spec_name_falls_through_to_accelerator_name():
    cfg = load_bridge_config(
        acs_env(MAX_CALL_SECONDS="   ", MAX_CALL_DURATION="900", VOICE_LIVE_ENDPOINT="  ", AZURE_VOICE_LIVE_ENDPOINT="https://accel.example"),
        acs_active=True,
    )
    assert cfg.max_call_seconds == 900
    assert cfg.voice_live_endpoint == "https://accel.example"


def test_whitespace_only_secrets_rejected():
    with pytest.raises(BridgeConfigError, match="MEDIA_WS_TOKEN"):
        load_bridge_config(acs_env(MEDIA_WS_TOKEN=" " * 40), acs_active=True)
    with pytest.raises(BridgeConfigError, match="ACS_COGNITIVE_SERVICES_ENDPOINT"):
        load_bridge_config(acs_env(ACS_COGNITIVE_SERVICES_ENDPOINT="   "), acs_active=True)


@pytest.mark.parametrize("value", ["abc", "0", "-5", "60.5"])
def test_bad_numbers_rejected(value):
    with pytest.raises(BridgeConfigError, match="MAX_CALL_SECONDS"):
        load_bridge_config(acs_env(MAX_CALL_SECONDS=value), acs_active=True)


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "-5", "0"])
def test_non_finite_and_non_positive_timeouts_rejected(value):
    with pytest.raises(BridgeConfigError, match="MEDIA_CONNECT_TIMEOUT_SECONDS"):
        load_bridge_config(acs_env(MEDIA_CONNECT_TIMEOUT_SECONDS=value), acs_active=True)


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


@pytest.mark.parametrize("value", ["[1]", '"just a string"', "123", "null"])
def test_interim_response_must_be_object(value):
    with pytest.raises(BridgeConfigError, match="INTERIM_RESPONSE_JSON"):
        load_bridge_config(acs_env(INTERIM_RESPONSE_JSON=value), acs_active=True)


def test_interim_response_duplicate_keys_rejected():
    with pytest.raises(BridgeConfigError):
        load_bridge_config(acs_env(INTERIM_RESPONSE_JSON='{"a": 1, "a": 2}'), acs_active=True)


def test_interim_response_is_read_only():
    from types import MappingProxyType

    cfg = load_bridge_config(acs_env(INTERIM_RESPONSE_JSON='{"type": "llm_interim_response"}'), acs_active=True)
    assert isinstance(cfg.interim_response, MappingProxyType)


def test_routes_mapping_is_read_only():
    cfg = load_bridge_config(acs_env(), acs_active=True)
    with pytest.raises(TypeError):
        cfg.routes["+19999999999"] = AgentRoute("x", "y", "1")  # type: ignore[index]


def test_valid_routing_constant_is_valid():
    assert parse_routing(VALID_ROUTING)


def test_acs_inactive_still_validates_provided_routing():
    with pytest.raises(BridgeConfigError):
        load_bridge_config({"AGENT_ROUTING_JSON": "not json"}, acs_active=False)


@pytest.mark.parametrize("value", ["true", "  true  ", "True", "TRUE ", " TrUe"])
def test_enable_web_client_case_and_whitespace_insensitive(value):
    cfg = load_bridge_config(acs_env(ENABLE_WEB_CLIENT=value), acs_active=True)
    assert cfg.enable_web_client is True


@pytest.mark.parametrize("value", ["yes", "1", "on", "false", ""])
def test_enable_web_client_rejects_non_true_values(value):
    cfg = load_bridge_config(acs_env(ENABLE_WEB_CLIENT=value), acs_active=True)
    assert cfg.enable_web_client is False


def test_nested_duplicate_field_reports_honestly_not_as_phone_collision():
    raw = '{"+14165551234": {"project": "p", "project": "q", "agent": "a", "version": "10"}}'
    with pytest.raises(BridgeConfigError) as exc:
        parse_routing(raw)
    assert "4165551234" not in str(exc.value)


def test_duplicate_key_with_digits_but_invalid_e164_is_still_masked():
    """A digit-bearing but not-strictly-E.164 duplicate key (e.g. with an extension) must still be masked."""
    raw = '{"tel:+14165550123 x9": {"project": "p", "agent": "a", "version": "1"}, "tel:+14165550123 x9": {"project": "p", "agent": "a", "version": "1"}}'
    with pytest.raises(BridgeConfigError) as exc:
        parse_routing(raw)
    assert "4165550123" not in str(exc.value)


@pytest.mark.parametrize("value", ["9" * 400, "9" * 4000, "999999"])
def test_huge_and_over_cap_numbers_rejected_without_overflow_error(value):
    with pytest.raises(BridgeConfigError, match="MAX_CALL_SECONDS"):
        load_bridge_config(acs_env(MAX_CALL_SECONDS=value), acs_active=True)


@pytest.mark.parametrize("value", ["٥", "1_000", "1e3", "0x10"])
def test_non_ascii_and_non_plain_digit_numbers_rejected(value):
    with pytest.raises(BridgeConfigError, match="MAX_CALL_SECONDS"):
        load_bridge_config(acs_env(MAX_CALL_SECONDS=value), acs_active=True)


@pytest.mark.parametrize("body", ['{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'])
def test_interim_response_rejects_non_finite_json_constants(body):
    with pytest.raises(BridgeConfigError, match="INTERIM_RESPONSE_JSON"):
        load_bridge_config(acs_env(INTERIM_RESPONSE_JSON=body), acs_active=True)


def test_interim_response_is_frozen_recursively():
    cfg = load_bridge_config(
        acs_env(INTERIM_RESPONSE_JSON='{"a": {"b": 1}, "c": [1, 2, 3]}'), acs_active=True
    )
    from types import MappingProxyType

    assert isinstance(cfg.interim_response["a"], MappingProxyType)
    assert cfg.interim_response["c"] == (1, 2, 3)
    with pytest.raises(TypeError):
        cfg.interim_response["a"]["b"] = 2


def test_bridge_config_error_from_duplicate_hook_is_not_double_wrapped():
    raw = '{"+14165551234": {"project": "p", "agent": "a", "version": "1"}, "+14165551234": {"project": "p", "agent": "a", "version": "2"}}'
    with pytest.raises(BridgeConfigError) as exc:
        parse_routing(raw)
    assert not str(exc.value).startswith("AGENT_ROUTING_JSON could not be parsed")
