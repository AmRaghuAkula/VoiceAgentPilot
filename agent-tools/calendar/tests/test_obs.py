"""The one-line request log and phone masking (spec section 10, rev 3.2)."""

from __future__ import annotations

import json
import logging

import pytest

from calendar_tools import obs

LOGGER = logging.getLogger("calendar_tools.test_obs")
KEYS = {
    "request_id",
    "binding_id",
    "operation",
    "status",
    "diagnostic",
    "reason",
    "principal",
    "provider_ms",
    "total_ms",
    "replayed",
}


@pytest.mark.parametrize(
    ("value", "masked"),
    [
        ("+16135550123", "***0123"),
        ("+1 (613) 555-0123", "***0123"),
        ("613.555.0199", "***0199"),
        ("123", "***"),
        ("", "***"),
        ("no digits", "***"),
        (None, "***"),
        (6135550142, "***0142"),
    ],
)
def test_mask_phone(value, masked):
    assert obs.mask_phone(value) == masked


def _log(**overrides):
    fields = dict(
        request_id="7d1c2b3a" * 4,
        binding_id="test-alpha",
        operation="check_availability",
        status="available",
        diagnostic="ok",
        provider_ms=12,
        total_ms=40,
        replayed=False,
    )
    fields.update(overrides)
    obs.log_request(LOGGER, **fields)


def _lines(caplog, level=logging.INFO):
    return [r for r in caplog.records if r.name == LOGGER.name and r.levelno == level]


def test_strict_is_on_for_the_suite():
    assert obs.STRICT is True


def test_one_json_line_with_exactly_the_documented_keys(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log()
    records = _lines(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "\n" not in message
    payload = json.loads(message)
    assert set(payload) == KEYS
    assert payload["reason"] is None
    assert payload["principal"] is None
    assert payload["status"] == "available"
    assert payload["replayed"] is False


def test_reason_and_principal_carried(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(
        status=403,
        diagnostic="forbidden",
        reason="principal_not_allowed",
        principal="00000000-0000-0000-0000-0000000000a1",
    )
    payload = json.loads(_lines(caplog)[0].getMessage())
    assert payload["status"] == 403
    assert payload["reason"] == "principal_not_allowed"
    assert payload["principal"] == "00000000-0000-0000-0000-0000000000a1"


@pytest.mark.parametrize("status", ["ok", "available", "no_availability", "booked"])
def test_ok_outcomes_with_ok_do_not_raise(status, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(status=status, diagnostic="ok")
    assert json.loads(_lines(caplog)[0].getMessage())["diagnostic"] == "ok"


@pytest.mark.parametrize(
    "diagnostic", ["claim_finalize_failed", "duplicate_event_removed", "stale_claim_recovered"]
)
def test_booked_with_informational_diagnostic_is_kept(diagnostic, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(operation="book_appointment", status="booked", diagnostic=diagnostic)
    payload = json.loads(_lines(caplog)[0].getMessage())
    assert payload["diagnostic"] == diagnostic
    assert _lines(caplog, logging.WARNING) == []


def test_replayed_booked_is_ok(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(operation="book_appointment", status="booked", diagnostic="ok", replayed=True)
    assert json.loads(_lines(caplog)[0].getMessage())["replayed"] is True


NON_OK = [
    "calendar_unavailable",
    "invalid_request",
    "invalid_slot",
    "slot_unavailable",
    "limit_reached",
    "booking_unconfirmed",
    401,
    403,
    404,
    405,
    413,
]


@pytest.mark.parametrize("status", NON_OK)
@pytest.mark.parametrize("diagnostic", ["", "ok", None])
def test_non_ok_without_diagnostic_raises_under_strict(status, diagnostic, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    with pytest.raises(ValueError):
        _log(status=status, diagnostic=diagnostic)


def test_bare_provider_error_diagnostic_fails_under_strict(caplog):
    from calendar_tools.core.ports import ProviderError

    caplog.set_level(logging.INFO, logger=LOGGER.name)
    exc = ProviderError()
    with pytest.raises(ValueError):
        _log(status="calendar_unavailable", diagnostic=exc.diagnostic, reason=exc.reason)


def test_non_code_reason_raises_under_strict(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    with pytest.raises(ValueError):
        _log(status="invalid_request", diagnostic="invalid_request", reason="Jordan Example")


@pytest.mark.parametrize("reason", ["invalid_reason", "613" + "5550123", "id." + "613-555-0123"])
def test_replaced_or_phone_like_reason_raises_under_strict(reason, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    with pytest.raises(ValueError):
        _log(status="calendar_unavailable", diagnostic="provider_config_error", reason=reason)


def test_exception_with_bad_reason_fails_the_suite_when_logged(caplog):
    from calendar_tools.core.ports import ProviderConfigError

    caplog.set_level(logging.INFO, logger=LOGGER.name)
    exc = ProviderConfigError(reason="vendor text: not found")
    with pytest.raises(ValueError):
        _log(status="calendar_unavailable", diagnostic=exc.diagnostic, reason=exc.reason)


def test_non_code_reason_replaced_in_production(caplog, monkeypatch):
    monkeypatch.setattr(obs, "STRICT", False)
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(status="invalid_request", diagnostic="invalid_request", reason="Jordan Example")
    assert "Jordan" not in caplog.text
    assert json.loads(_lines(caplog)[0].getMessage())["reason"] == "invalid_reason"


def test_unclassified_passed_in_production_is_logged(caplog, monkeypatch):
    monkeypatch.setattr(obs, "STRICT", False)
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(status="calendar_unavailable", diagnostic="unclassified")
    assert json.loads(_lines(caplog)[0].getMessage())["diagnostic"] == "unclassified"


@pytest.mark.parametrize("status", NON_OK)
def test_non_ok_with_diagnostic_logs(status, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(status=status, diagnostic="provider_unavailable")
    assert json.loads(_lines(caplog)[0].getMessage())["diagnostic"] == "provider_unavailable"


@pytest.mark.parametrize("diagnostic", ["", "ok", None])
def test_non_ok_without_diagnostic_in_production_logs_unclassified(diagnostic, caplog, monkeypatch):
    monkeypatch.setattr(obs, "STRICT", False)
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(status="calendar_unavailable", diagnostic=diagnostic)
    info = _lines(caplog)
    warnings = _lines(caplog, logging.WARNING)
    assert len(info) == 1
    assert json.loads(info[0].getMessage())["diagnostic"] == "unclassified"
    assert len(warnings) == 1
    assert "unclassified" in warnings[0].getMessage()


def test_log_line_carries_no_extra_fields(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    with pytest.raises(TypeError):
        obs.log_request(LOGGER, contact_name="Jordan Example", **{  # type: ignore[call-arg]
            "request_id": "0" * 32,
            "binding_id": "test-alpha",
            "operation": "book_appointment",
            "status": "booked",
            "diagnostic": "ok",
            "provider_ms": 1,
            "total_ms": 2,
            "replayed": False,
        })


# --- UC02b: identifiers are validated and bounded before they are logged ------
# (UC02a cso note: `binding_id` can come from a request path, so it must never
# carry free text or a phone number into the log line.)

BAD_IDS = {
    "request_id": ["", "zq", "7D1C2B3A" * 4, "7d1c2b3a" * 5, "not hex at all, 32 characters!!"],
    "binding_id": ["", "NOT A BINDING", "x" * 41, "a-" + "6135550" + "123", "../etc"],
    "principal": ["", "not-a-guid", "00000000-0000-0000-0000-0000000000A1x"],
    "operation": ["", "Check Availability", "x" * 65],
}


@pytest.mark.parametrize(
    ("field", "value"), [(f, v) for f, values in BAD_IDS.items() for v in values]
)
def test_bad_identifier_raises_under_strict(field, value):
    with pytest.raises(ValueError) as info:
        _log(**{field: value})
    assert value not in str(info.value) or value == ""


@pytest.mark.parametrize(
    ("field", "value", "replacement"),
    [
        ("request_id", "zq", "invalid"),
        ("binding_id", "NOT A BINDING", None),
        ("principal", "not-a-guid", None),
        ("operation", "Check Availability", "invalid"),
    ],
)
def test_bad_identifier_replaced_in_production(field, value, replacement, caplog, monkeypatch):
    monkeypatch.setattr(obs, "STRICT", False)
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(**{field: value})
    info = _lines(caplog)
    assert len(info) == 1
    assert value not in info[0].getMessage()
    assert json.loads(info[0].getMessage())[field] == replacement
    assert len(_lines(caplog, logging.WARNING)) == 1


def test_valid_identifiers_pass_through(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    _log(binding_id=None, principal="00000000-0000-0000-0000-0000000000a1", operation="health")
    payload = json.loads(_lines(caplog)[0].getMessage())
    assert payload["binding_id"] is None
    assert payload["operation"] == "health"
