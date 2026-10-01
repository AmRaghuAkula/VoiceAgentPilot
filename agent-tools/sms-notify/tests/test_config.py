"""Config: settings and the recipient secret (spec section 7, section 9 "Config")."""

from __future__ import annotations

import json

import pytest

from sms_notify.core.config import load_settings, parse_recipients
from sms_notify.core.errors import ConfigError
from sms_notify.core.phone import country_of, is_e164, mask
from tests.fakes import APP_ID, BASE_ENV, RECIPIENT_1, make_settings


def test_defaults():
    s = make_settings()
    assert (s.max_chars, s.max_lines) == (480, 8)
    assert (s.min_interval_seconds, s.max_per_hour, s.dedupe_minutes) == (90, 6, 30)
    assert s.prefix == "" and s.require_role is False
    assert s.allowed_countries == frozenset({"CA"})
    assert s.audiences == (f"api://{APP_ID}", APP_ID)


@pytest.mark.parametrize(
    "name",
    [
        "SMS_FROM_NUMBER",
        "SMS_ALLOWED_PRINCIPALS",
        "SMS_AUTH_AUDIENCE",
        "SMS_AUTH_TENANT_ID",
        "KEY_VAULT_URI",
        "STATE_BLOB_URL",
    ],
)
def test_missing_required_setting(name):
    env = {k: v for k, v in BASE_ENV.items() if k != name}
    with pytest.raises(ConfigError) as err:
        load_settings(env)
    assert name in str(err.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SMS_FROM_NUMBER", "6135550100"),
        ("SMS_ALLOWED_COUNTRIES", "US"),
        ("SMS_ALLOWED_PRINCIPALS", "not-a-guid"),
        ("SMS_AUTH_AUDIENCE", "https://example.invalid"),
        ("SMS_AUTH_TENANT_ID", "contoso"),
        ("SMS_MAX_CHARS", "481"),
        ("SMS_MAX_CHARS", "abc"),
        ("SMS_REQUIRE_ROLE", "maybe"),
        ("KEY_VAULT_URI", "http://kv-example.vault.azure.net/"),
        ("KEY_VAULT_URI", "https://attacker.invalid/"),
        ("STATE_BLOB_URL", "https://stexample.blob.core.windows.net/"),
        ("STATE_BLOB_URL", "https://stexample.blob.core.windows.net/a/b"),
        ("STATE_BLOB_URL", "https://other.invalid/sms-state"),
    ],
)
def test_invalid_setting_never_echoes_the_value(name, value):
    with pytest.raises(ConfigError) as err:
        make_settings(**{name: value})
    assert value not in str(err.value)
    assert "555" not in str(err.value)


def test_non_guid_audience_has_only_the_uri_form():
    s = make_settings(SMS_AUTH_AUDIENCE="api://sms-notify-example")
    assert s.audiences == ("api://sms-notify-example",)


def test_require_role_true():
    assert make_settings(SMS_REQUIRE_ROLE="true").require_role is True


def test_recipients_parse():
    raw = json.dumps({"recipients": [RECIPIENT_1]})
    assert parse_recipients(raw, frozenset({"CA"})) == (RECIPIENT_1,)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["+16135550199"]),
        json.dumps({"recipients": []}),
        json.dumps({"recipients": ["6135550199"]}),
        json.dumps({"recipients": ["+12025550123"]}),
        json.dumps({"recipients": ["+16135550101", "+16135550102", "+16135550103", "+16135550104"]}),
        json.dumps({"recipients": ["+16135550101", "+16135550101"]}),
        json.dumps({"recipients": [16135550101]}),
    ],
    ids=["json", "shape", "empty", "e164", "country", "more-than-3", "duplicate", "type"],
)
def test_bad_recipients_raise_without_the_number(raw):
    with pytest.raises(ConfigError) as err:
        parse_recipients(raw, frozenset({"CA"}))
    assert "555" not in str(err.value) and "613" not in str(err.value)


CURRENT_CANADIAN_GEOGRAPHIC_AREA_CODES = {
    "204", "226", "236", "249", "250", "257", "263", "289", "306", "343", "354", "365",
    "367", "368", "382", "403", "416", "418", "428", "431", "437", "438", "450", "468",
    "474", "506", "514", "519", "548", "579", "581", "584", "587", "604", "613", "639",
    "647", "672", "683", "705", "709", "742", "753", "778", "780", "782", "807", "819",
    "825", "867", "873", "879", "902", "905", "942",
}  # fmt: skip


def test_canadian_area_code_list_is_complete():
    from sms_notify.core.phone import CANADIAN_AREA_CODES

    assert "226" in CANADIAN_AREA_CODES
    assert CURRENT_CANADIAN_GEOGRAPHIC_AREA_CODES <= CANADIAN_AREA_CODES
    assert country_of("+12265550142") == "CA"


@pytest.mark.parametrize("npa", sorted(CURRENT_CANADIAN_GEOGRAPHIC_AREA_CODES))
def test_every_canadian_area_code_is_accepted(npa):
    assert country_of(f"+1{npa}5550123") == "CA"


def test_phone_helpers():
    assert is_e164("+16135550199") and not is_e164("16135550199") and not is_e164(None)
    assert country_of("+16135550199") == "CA"
    assert country_of("+12025550123") is None
    assert country_of("+44" + "7700" + "900123") is None
    assert mask("+16135550199") == "***0199"
    assert mask("12") == "***" and mask(None) == "***"
