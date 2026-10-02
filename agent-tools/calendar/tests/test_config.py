"""Startup configuration (plan section 3, UC02b): fail closed, name the variable,
never echo the value."""

from __future__ import annotations

import logging

import pytest

from calendar_tools.config import (
    DEFAULT_PRINCIPAL_CLAIM,
    DEFAULT_REQUIRED_ROLE,
    ConfigError,
    load_config,
)

TENANT = "00000000-0000-0000-0000-0000000000c1"
APP = "00000000-0000-0000-0000-0000000000c2"
SENTINEL = "SENTINEL-VALUE-9"


def _env(**overrides: str | None) -> dict[str, str]:
    env = {
        "CALENDAR_AUTH_TENANT_ID": TENANT,
        "CALENDAR_AUTH_APP_ID": APP,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def test_minimal_env_uses_defaults() -> None:
    config = load_config(_env())
    assert config.tenant_id == TENANT
    assert config.app_id == APP
    assert config.required_role == DEFAULT_REQUIRED_ROLE == "Calendar.Invoke"
    assert config.principal_claim == DEFAULT_PRINCIPAL_CLAIM == "oid"


def test_audiences_and_issuers() -> None:
    config = load_config(_env())
    assert config.audiences == frozenset({f"api://{APP}", APP})
    assert config.issuers == frozenset(
        {f"https://sts.windows.net/{TENANT}/", f"https://login.microsoftonline.com/{TENANT}/v2.0"}
    )


def test_guids_are_lowercased() -> None:
    config = load_config(
        _env(CALENDAR_AUTH_TENANT_ID=TENANT.upper(), CALENDAR_AUTH_APP_ID=APP.upper())
    )
    assert config.tenant_id == TENANT
    assert config.app_id == APP


@pytest.mark.parametrize("claim", ["oid", "azp", "appid"])
def test_principal_claim_choices(claim: str) -> None:
    assert load_config(_env(CALENDAR_AUTH_PRINCIPAL_CLAIM=claim)).principal_claim == claim


def test_custom_role() -> None:
    assert load_config(_env(CALENDAR_AUTH_REQUIRED_ROLE="Other.Role")).required_role == "Other.Role"


@pytest.mark.parametrize(
    ("variable", "value", "check"),
    [
        ("CALENDAR_AUTH_TENANT_ID", None, "missing"),
        ("CALENDAR_AUTH_TENANT_ID", "   ", "blank"),
        ("CALENDAR_AUTH_TENANT_ID", SENTINEL, "invalid"),
        ("CALENDAR_AUTH_APP_ID", None, "missing"),
        ("CALENDAR_AUTH_APP_ID", "", "blank"),
        ("CALENDAR_AUTH_APP_ID", SENTINEL, "invalid"),
        ("CALENDAR_AUTH_REQUIRED_ROLE", "", "blank"),
        ("CALENDAR_AUTH_REQUIRED_ROLE", "has space " + SENTINEL, "invalid"),
        ("CALENDAR_AUTH_REQUIRED_ROLE", "x" * 121, "invalid"),
        ("CALENDAR_AUTH_PRINCIPAL_CLAIM", "", "blank"),
        ("CALENDAR_AUTH_PRINCIPAL_CLAIM", " \t", "blank"),
        ("CALENDAR_AUTH_PRINCIPAL_CLAIM", SENTINEL, "invalid"),
        ("CALENDAR_AUTH_PRINCIPAL_CLAIM", "sub", "invalid"),
        ("CALENDAR_AUTH_PRINCIPAL_CLAIM", "OID", "invalid"),
    ],
)
def test_bad_values_fail_closed_without_echo(
    variable: str, value: str | None, check: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConfigError) as info:
        load_config(_env(**{variable: value}))
    err = info.value
    assert err.variable == variable
    assert err.check == check
    assert str(err) == f"config error: variable={variable} check={check}"
    assert SENTINEL not in str(err)
    assert SENTINEL not in repr(err.args)
    assert SENTINEL not in caplog.text
