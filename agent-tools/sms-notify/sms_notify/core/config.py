"""Settings (spec section 7) and the recipient secret. Parsed at request time.

Error messages name the setting, never its value (no number ever appears in them).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from sms_notify.core.errors import ConfigError
from sms_notify.core.phone import SUPPORTED_COUNTRIES, country_of, is_e164

DEFAULT_MAX_CHARS = 480
DEFAULT_MAX_LINES = 8
HARD_MAX_CHARS = 480  # the adapter's max_body_chars; SMS_MAX_CHARS may only lower it
MAX_RECIPIENTS = 3

TWILIO_SECRET_NAME = "twilio-api"
RECIPIENTS_SECRET_NAME = "sms-recipients"

_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CONTAINER = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])")


@dataclass(frozen=True)
class Settings:
    from_number: str
    allowed_countries: frozenset[str]
    max_chars: int
    max_lines: int
    allowed_principals: frozenset[str]
    auth_audience: str
    auth_app_id: str | None
    auth_tenant_id: str
    require_role: bool
    min_interval_seconds: int
    max_per_hour: int
    dedupe_minutes: int
    prefix: str
    key_vault_uri: str
    state_blob_url: str

    @property
    def audiences(self) -> tuple[str, ...]:
        return (self.auth_audience, self.auth_app_id) if self.auth_app_id else (self.auth_audience,)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is missing")
    return value


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} is not an integer") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} is out of range")
    return value


def _bool(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in ("", "false", "0", "no"):
        return False
    if raw in ("true", "1", "yes"):
        return True
    raise ConfigError(f"{name} is not a boolean")


def _https_url(value: str, name: str, host_suffix: str) -> SplitResult:
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not host.endswith(host_suffix)
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ConfigError(f"{name} is not a valid https endpoint")
    return parts


def load_settings(env: Mapping[str, str]) -> Settings:
    from_number = _required(env, "SMS_FROM_NUMBER")
    if not is_e164(from_number):
        raise ConfigError("SMS_FROM_NUMBER is not E.164")

    countries = frozenset(
        c.strip().upper() for c in env.get("SMS_ALLOWED_COUNTRIES", "CA").split(",") if c.strip()
    )
    if not countries or not countries <= SUPPORTED_COUNTRIES:
        raise ConfigError("SMS_ALLOWED_COUNTRIES has an unsupported value")

    principals = frozenset(
        p.strip().lower() for p in _required(env, "SMS_ALLOWED_PRINCIPALS").split(",") if p.strip()
    )
    if not principals or not all(_GUID.fullmatch(p) for p in principals):
        raise ConfigError("SMS_ALLOWED_PRINCIPALS must be a comma-separated list of object IDs")

    audience = _required(env, "SMS_AUTH_AUDIENCE")
    if not audience.startswith("api://") or len(audience) <= len("api://"):
        raise ConfigError("SMS_AUTH_AUDIENCE must be an api:// application ID URI")
    tail = audience[len("api://") :]
    app_id = tail.lower() if _GUID.fullmatch(tail) else None

    tenant = _required(env, "SMS_AUTH_TENANT_ID")
    if not _GUID.fullmatch(tenant):
        raise ConfigError("SMS_AUTH_TENANT_ID must be a tenant ID")

    vault = _required(env, "KEY_VAULT_URI")
    _https_url(vault, "KEY_VAULT_URI", ".vault.azure.net")

    state = _required(env, "STATE_BLOB_URL").rstrip("/")
    parts = _https_url(state, "STATE_BLOB_URL", ".blob.core.windows.net")
    if not _CONTAINER.fullmatch(parts.path.strip("/")):
        raise ConfigError("STATE_BLOB_URL must name exactly one container")

    return Settings(
        from_number=from_number,
        allowed_countries=countries,
        max_chars=_int(env, "SMS_MAX_CHARS", DEFAULT_MAX_CHARS, 1, HARD_MAX_CHARS),
        max_lines=_int(env, "SMS_MAX_LINES", DEFAULT_MAX_LINES, 1, 20),
        allowed_principals=principals,
        auth_audience=audience,
        auth_app_id=app_id,
        auth_tenant_id=tenant.lower(),
        require_role=_bool(env, "SMS_REQUIRE_ROLE"),
        min_interval_seconds=_int(env, "SMS_MIN_INTERVAL_SECONDS", 90, 0, 86_400),
        max_per_hour=_int(env, "SMS_MAX_PER_HOUR", 6, 1, 60),
        dedupe_minutes=_int(env, "SMS_DEDUPE_MINUTES", 30, 1, 1_440),
        prefix=env.get("SMS_PREFIX", ""),
        key_vault_uri=vault.rstrip("/"),
        state_blob_url=state,
    )


def parse_recipients(raw: str, allowed_countries: frozenset[str]) -> tuple[str, ...]:
    """Parse the ``sms-recipients`` secret: ``{"recipients": ["+1..."]}``. 1 to 3 E.164 numbers."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError("sms-recipients is not valid JSON") from None
    recipients = data.get("recipients") if isinstance(data, dict) else None
    if not isinstance(recipients, list) or not recipients:
        raise ConfigError("sms-recipients has no recipients list")
    if len(recipients) > MAX_RECIPIENTS:
        raise ConfigError("sms-recipients has more than 3 recipients")
    for number in recipients:
        if not is_e164(number):
            raise ConfigError("sms-recipients has a number that is not E.164")
        if country_of(number) not in allowed_countries:
            raise ConfigError("sms-recipients has a number outside the allowed countries")
    if len(set(recipients)) != len(recipients):
        raise ConfigError("sms-recipients has a duplicate number")
    return tuple(recipients)
