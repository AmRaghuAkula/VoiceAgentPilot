import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.log_mask import mask_number
from app.routing import AgentRoute, is_valid_e164, normalize_number

DEFAULT_FALLBACK_MESSAGE = "Sorry, we're having trouble right now. Please call back in a few minutes."
DEFAULT_GOODBYE_MESSAGE = "We've reached the time limit for this call. Thank you for calling, goodbye."
DEFAULT_TTS_VOICE = "en-US-JennyNeural"
MIN_TOKEN_LENGTH = 32
MAX_NUMERIC_VALUE = 86_400  # 24h; generous upper bound for any timeout/duration setting here
_VERSION = re.compile(r"\A[1-9][0-9]*\Z", re.ASCII)
_NUMERIC = re.compile(r"\A[0-9]+(\.[0-9]+)?\Z", re.ASCII)


class BridgeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BridgeConfig:
    routes: Mapping[str, AgentRoute]
    media_ws_token: str
    acs_cognitive_services_endpoint: str
    fallback_message: str
    goodbye_message: str
    tts_voice: str
    max_call_seconds: int
    voice_live_endpoint: str | None
    media_connect_timeout: float
    voice_live_connect_timeout: float
    media_lost_grace: float
    enable_web_client: bool
    interim_response: Mapping | None
    voice_live_api_version: str | None
    callback_jwt_audience: str | None


_FOUR_OR_MORE_DIGITS = re.compile(r"\d{4,}")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            label = mask_number(key) if _FOUR_OR_MORE_DIGITS.search(key) else key
            raise BridgeConfigError(f"duplicate JSON key {label!r}")
        result[key] = value
    return result


def parse_routing(raw: str) -> dict[str, AgentRoute]:
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise BridgeConfigError(f"AGENT_ROUTING_JSON is not valid JSON: {exc.msg}") from exc
    except BridgeConfigError:
        raise
    except (ValueError, RecursionError) as exc:
        raise BridgeConfigError(f"AGENT_ROUTING_JSON could not be parsed: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise BridgeConfigError("AGENT_ROUTING_JSON must be a non-empty JSON object")

    routes: dict[str, AgentRoute] = {}
    for raw_key, entry in data.items():
        label = mask_number(raw_key)
        number = normalize_number(raw_key)
        if not is_valid_e164(number):
            raise BridgeConfigError(f"AGENT_ROUTING_JSON key {label} is not a valid E.164 phone number")
        if number in routes:
            raise BridgeConfigError(f"AGENT_ROUTING_JSON has two keys for {label}")
        if not isinstance(entry, dict):
            raise BridgeConfigError(f"AGENT_ROUTING_JSON entry for {label} must be an object")
        for field in ("project", "agent"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise BridgeConfigError(f"AGENT_ROUTING_JSON entry for {label} needs a non-empty '{field}'")
        version = entry.get("version")
        if not isinstance(version, str) or not _VERSION.match(version.strip()):
            raise BridgeConfigError(
                f"AGENT_ROUTING_JSON entry for {label} must pin 'version' as a string of digits"
            )
        routes[number] = AgentRoute(entry["project"].strip(), entry["agent"].strip(), version.strip())
    return routes


def _deep_freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _positive(env: Mapping[str, str], name: str, default, cast):
    raw = _get(env, name)
    if raw is None:
        return default
    if not _NUMERIC.match(raw):
        raise BridgeConfigError(f"{name} must be a number")
    try:
        value = cast(raw)
    except (ValueError, OverflowError) as exc:
        raise BridgeConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise BridgeConfigError(f"{name} must be positive")
    if value > MAX_NUMERIC_VALUE:
        raise BridgeConfigError(f"{name} must be at most {MAX_NUMERIC_VALUE}")
    return value


def load_bridge_config(env: Mapping[str, str], *, acs_active: bool) -> BridgeConfig:
    routing_raw = _get(env, "AGENT_ROUTING_JSON")
    token = _get(env, "MEDIA_WS_TOKEN") or ""
    cognitive = _get(env, "ACS_COGNITIVE_SERVICES_ENDPOINT") or ""

    if acs_active:
        for name, value in (
            ("AGENT_ROUTING_JSON", routing_raw),
            ("MEDIA_WS_TOKEN", token),
            ("ACS_COGNITIVE_SERVICES_ENDPOINT", cognitive),
        ):
            if not value:
                raise BridgeConfigError(f"{name} is required when ACS is configured")
        if len(token) < MIN_TOKEN_LENGTH:
            raise BridgeConfigError(f"MEDIA_WS_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")

    routes = parse_routing(routing_raw) if routing_raw else {}

    max_name = "MAX_CALL_SECONDS" if _get(env, "MAX_CALL_SECONDS") else "MAX_CALL_DURATION"

    interim = None
    interim_raw = _get(env, "INTERIM_RESPONSE_JSON")
    if interim_raw:
        def _reject_non_finite(_text: str):
            raise BridgeConfigError("INTERIM_RESPONSE_JSON must not contain NaN/Infinity")

        try:
            interim = json.loads(
                interim_raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite
            )
        except json.JSONDecodeError as exc:
            raise BridgeConfigError("INTERIM_RESPONSE_JSON is not valid JSON") from exc
        except BridgeConfigError:
            raise
        except (ValueError, RecursionError) as exc:
            raise BridgeConfigError(f"INTERIM_RESPONSE_JSON could not be parsed: {exc}") from exc
        if not isinstance(interim, dict):
            raise BridgeConfigError("INTERIM_RESPONSE_JSON must be a JSON object")
        interim = _deep_freeze(interim)

    return BridgeConfig(
        routes=MappingProxyType(routes),
        media_ws_token=token,
        acs_cognitive_services_endpoint=cognitive,
        fallback_message=_get(env, "FALLBACK_MESSAGE") or DEFAULT_FALLBACK_MESSAGE,
        goodbye_message=_get(env, "GOODBYE_MESSAGE") or DEFAULT_GOODBYE_MESSAGE,
        tts_voice=_get(env, "ACS_TTS_VOICE") or DEFAULT_TTS_VOICE,
        max_call_seconds=_positive(env, max_name, 600, int),
        voice_live_endpoint=_get(env, "VOICE_LIVE_ENDPOINT") or _get(env, "AZURE_VOICE_LIVE_ENDPOINT"),
        media_connect_timeout=_positive(env, "MEDIA_CONNECT_TIMEOUT_SECONDS", 10.0, float),
        voice_live_connect_timeout=_positive(env, "VOICE_LIVE_CONNECT_TIMEOUT_SECONDS", 8.0, float),
        media_lost_grace=_positive(env, "MEDIA_LOST_GRACE_SECONDS", 5.0, float),
        enable_web_client=(env.get("ENABLE_WEB_CLIENT") or "").strip().lower() == "true",
        interim_response=interim,
        voice_live_api_version=_get(env, "VOICE_LIVE_API_VERSION"),
        callback_jwt_audience=_get(env, "ACS_CALLBACK_JWT_AUDIENCE"),
    )
