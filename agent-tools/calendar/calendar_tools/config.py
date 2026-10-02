"""Startup configuration (plan section 3): validated once, fail closed.

A bad or missing value raises `ConfigError`, whose text names the variable and
the failed check only, never the value. UC06/UC07 extend this module with the
claim-store and Key Vault settings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

TENANT_ID = "CALENDAR_AUTH_TENANT_ID"
APP_ID = "CALENDAR_AUTH_APP_ID"
REQUIRED_ROLE = "CALENDAR_AUTH_REQUIRED_ROLE"
PRINCIPAL_CLAIM = "CALENDAR_AUTH_PRINCIPAL_CLAIM"

DEFAULT_REQUIRED_ROLE = "Calendar.Invoke"
DEFAULT_PRINCIPAL_CLAIM = "oid"
# Spec section 4.2: the claims that can identify the caller. D-059 (b): Foundry
# issues v1 tokens with no `azp`, so `oid` is the deployed value.
PRINCIPAL_CLAIMS: frozenset[str] = frozenset({"oid", "azp", "appid"})

_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ROLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


class ConfigError(Exception):
    """A configuration variable is missing, blank or invalid. Holds only the
    variable's name and the check (`missing`, `blank`, `invalid`)."""

    def __init__(self, variable: str, check: str) -> None:
        self.variable = variable
        self.check = check
        super().__init__(variable, check)

    def __str__(self) -> str:
        return f"config error: variable={self.variable} check={self.check}"


@dataclass(frozen=True)
class Config:
    tenant_id: str
    app_id: str
    required_role: str
    principal_claim: str

    @property
    def audiences(self) -> frozenset[str]:
        """Spec section 9.1: the app ID URI or the bare app ID."""
        return frozenset({f"api://{self.app_id}", self.app_id})

    @property
    def issuers(self) -> frozenset[str]:
        """Spec section 9.1: the v1 or the v2 issuer for our tenant, exactly."""
        return frozenset(
            {
                f"https://sts.windows.net/{self.tenant_id}/",
                f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
            }
        )


def _read(env: Mapping[str, str], variable: str, default: str | None = None) -> str:
    raw = env.get(variable)
    if raw is None:
        if default is None:
            raise ConfigError(variable, "missing")
        return default
    value = raw.strip()
    if not value:
        # An explicitly blank value is a misconfiguration, not "use the default".
        raise ConfigError(variable, "blank")
    return value


def _guid(env: Mapping[str, str], variable: str) -> str:
    value = _read(env, variable).lower()
    if not _GUID.fullmatch(value):
        raise ConfigError(variable, "invalid")
    return value


def load_config(env: Mapping[str, str]) -> Config:
    tenant_id = _guid(env, TENANT_ID)
    app_id = _guid(env, APP_ID)
    role = _read(env, REQUIRED_ROLE, DEFAULT_REQUIRED_ROLE)
    if not _ROLE.fullmatch(role):
        raise ConfigError(REQUIRED_ROLE, "invalid")
    claim = _read(env, PRINCIPAL_CLAIM, DEFAULT_PRINCIPAL_CLAIM)
    if claim not in PRINCIPAL_CLAIMS:
        raise ConfigError(PRINCIPAL_CLAIM, "invalid")
    return Config(tenant_id=tenant_id, app_id=app_id, required_role=role, principal_claim=claim)
