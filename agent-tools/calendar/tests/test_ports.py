"""Core ports (spec section 6.2, plan P2, P3, rev 1.6 SecretInvalid)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from calendar_tools.core import ports
from calendar_tools.providers import base

UTC_START = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
UTC_END = UTC_START + timedelta(minutes=30)
NAIVE = datetime(2026, 10, 5, 14, 0)
OFFSET = datetime(2026, 10, 5, 10, 0, tzinfo=timezone(timedelta(hours=-4)))

META = ports.BookingMeta(
    service_tag="svc",
    binding_id="test-alpha",
    fingerprint="fp",
    contact_tag="ct",
    booking_ref="B7K2Q9",
)


def _event(**overrides):
    fields = dict(
        start=UTC_START,
        end=UTC_END,
        timezone="America/Toronto",
        title="t",
        description="d",
        meta=META,
        request_key="rk",
    )
    fields.update(overrides)
    return ports.NewEvent(**fields)


@pytest.mark.parametrize("bad", [NAIVE, OFFSET], ids=["naive", "non-utc"])
def test_interval_rejects_non_utc(bad):
    with pytest.raises(ValueError):
        ports.Interval(start=bad, end=UTC_END)
    with pytest.raises(ValueError):
        ports.Interval(start=UTC_START, end=bad)


@pytest.mark.parametrize("bad", [NAIVE, OFFSET], ids=["naive", "non-utc"])
def test_new_event_rejects_non_utc(bad):
    with pytest.raises(ValueError):
        _event(start=bad)
    with pytest.raises(ValueError):
        _event(end=bad)


@pytest.mark.parametrize("bad", [NAIVE, OFFSET], ids=["naive", "non-utc"])
def test_booking_record_rejects_non_utc(bad):
    with pytest.raises(ValueError):
        ports.BookingRecord(event_id="e1", start=bad, end=UTC_END, meta=META)
    with pytest.raises(ValueError):
        ports.BookingRecord(event_id="e1", start=UTC_START, end=bad, meta=META)


def test_utc_values_accepted_and_frozen():
    interval = ports.Interval(start=UTC_START, end=UTC_END)
    record = ports.BookingRecord(event_id="e1", start=UTC_START, end=UTC_END, meta=META)
    event = _event()
    ref = ports.CalendarRef(provider="fake", calendar_id="primary", credential_secret_name="s")
    for obj in (interval, record, event, ref, META):
        with pytest.raises(AttributeError):
            obj.__setattr__("start" if hasattr(obj, "start") else "provider", UTC_START)


def test_provider_timeout_requires_maybe_committed():
    with pytest.raises(TypeError):
        ports.ProviderTimeout()  # type: ignore[call-arg]
    assert ports.ProviderTimeout(maybe_committed=True).maybe_committed is True
    assert ports.ProviderTimeout(False).maybe_committed is False


def test_provider_timeout_rejects_non_bool():
    with pytest.raises(TypeError):
        ports.ProviderTimeout(maybe_committed="yes")  # type: ignore[arg-type]


EXCEPTIONS = [
    ports.ProviderUnavailable,
    ports.ProviderAuthError,
    ports.ProviderConfigError,
    ports.ProviderTimeout,
    ports.SecretStoreUnavailable,
    ports.SecretInvalid,
]


@pytest.mark.parametrize("exc", EXCEPTIONS)
def test_all_exceptions_subclass_provider_error(exc):
    assert issubclass(exc, ports.ProviderError)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ports.ProviderUnavailable, "provider_unavailable"),
        (ports.ProviderAuthError, "credential_rejected"),
        (ports.ProviderConfigError, "provider_config_error"),
        (ports.ProviderTimeout, "provider_timeout"),
        (ports.SecretStoreUnavailable, "secret_store_unreachable"),
        (ports.SecretInvalid, "secret_invalid"),
    ],
)
def test_exception_diagnostic_codes(exc, code):
    assert exc.diagnostic == code


def test_exception_reason_optional_and_carried():
    assert ports.ProviderUnavailable().reason is None
    err = ports.ProviderConfigError(reason="calendar_not_found")
    assert err.reason == "calendar_not_found"
    assert err.diagnostic == "provider_config_error"
    assert ports.ProviderTimeout(maybe_committed=True, reason="create").reason == "create"


@pytest.mark.parametrize("bad", ["vendor said: calendar x@example.com not found", "", "a" * 257, "+16135550123", "613" + "5550123", "-".join(["613", "555", "0123"])])
def test_non_code_reason_replaced(bad):
    err = ports.ProviderConfigError(reason=bad)
    assert err.reason == "invalid_reason"
    assert bad not in str(err) or bad == ""


def test_longest_secret_invalid_reason_kept():
    err = ports.SecretInvalid("a" * 127, "wrong_length")
    assert err.reason == "a" * 127 + ".wrong_length"
    assert ports.is_reason_code(err.reason)


def test_code_shaped_reasons_kept():
    for good in ("calendar_not_found", "cal-binding-test-alpha-google.bad_json", "contact.phone,slot_id"):
        assert ports.ProviderConfigError(reason=good).reason == good


def test_reason_must_be_a_string_or_none():
    with pytest.raises(TypeError):
        ports.ProviderUnavailable(reason=503)  # type: ignore[arg-type]


def test_secret_invalid_is_secret_store_unavailable():
    err = ports.SecretInvalid("slot-token-key", "wrong_length")
    assert isinstance(err, ports.SecretStoreUnavailable)
    assert err.diagnostic == "secret_invalid"
    assert err.reason == "slot-token-key.wrong_length"
    assert err.secret_name == "slot-token-key"
    assert err.check == "wrong_length"


@pytest.mark.parametrize(
    "check", ["missing", "empty", "not_base64", "wrong_length", "bad_json", "wrong_kind"]
)
def test_secret_invalid_checks(check):
    err = ports.SecretInvalid("fingerprint-key", check)
    assert err.reason == f"fingerprint-key.{check}"


def test_secret_invalid_rejects_unknown_check():
    with pytest.raises(ValueError):
        ports.SecretInvalid("fingerprint-key", "looks_odd")


def test_secret_invalid_rejects_value_shaped_name():
    # A secret name is a Key Vault name, never a value: anything outside
    # the name alphabet is refused, so a value can't ride in on it.
    with pytest.raises(ValueError):
        ports.SecretInvalid("abc+/=SENTINEL VALUE", "empty")


def test_secret_invalid_str_holds_only_name_and_check():
    err = ports.SecretInvalid("cal-binding-test-alpha-google", "bad_json")
    text = str(err)
    assert "cal-binding-test-alpha-google" in text
    assert "bad_json" in text
    # It has no field for a value at all.
    assert set(vars(err)) <= {"reason", "secret_name", "check"}


def test_secret_invalid_caught_by_secret_store_unavailable_mapping():
    try:
        raise ports.SecretInvalid("slot-token-key", "missing")
    except ports.SecretStoreUnavailable as caught:
        assert caught.diagnostic == "secret_invalid"


@pytest.mark.parametrize(
    "name",
    [
        "CalendarRef",
        "Interval",
        "BookingMeta",
        "NewEvent",
        "BookingRecord",
        "CalendarProvider",
        "ProviderError",
        "ProviderUnavailable",
        "ProviderAuthError",
        "ProviderConfigError",
        "ProviderTimeout",
        "SecretStoreUnavailable",
        "SecretInvalid",
    ],
)
def test_providers_base_reexports_identical_objects(name):
    assert getattr(base, name) is getattr(ports, name)


def test_calendar_provider_protocol_methods():
    for method in (
        "resolve_calendar_identity",
        "get_busy",
        "find_bookings",
        "get_event",
        "create_event",
        "delete_event",
    ):
        assert hasattr(ports.CalendarProvider, method)
