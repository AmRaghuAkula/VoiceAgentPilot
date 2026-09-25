import pytest

from app.routing import (
    AgentRoute,
    called_number_from_event,
    caller_number_from_event,
    is_valid_e164,
    normalize_number,
    resolve_route,
)


@pytest.mark.parametrize(
    "raw",
    ["+14165551234", "14165551234", "4165551234", "(416) 555-1234", "+1 416-555-1234", "4:+14165551234"],
)
def test_formats_normalize_to_same_key(raw):
    assert normalize_number(raw) == "+14165551234"


@pytest.mark.parametrize("raw, expected", [(None, ""), ("", ""), ("abc", ""), ("416555123", "416555123")])
def test_garbage_stays_unmatchable(raw, expected):
    assert normalize_number(raw) == expected


@pytest.mark.parametrize(
    "number, valid",
    [
        ("+14165551234", True),
        ("+1416555123", False),
        ("+141655512345", False),
        ("+442071838750", True),
        ("416555123", False),
        ("+", False),
    ],
)
def test_is_valid_e164(number, valid):
    assert is_valid_e164(number) is valid


ROUTES = {"+14165551234": AgentRoute("proj", "agent-a", "10")}


def test_resolve_hit_from_any_format():
    assert resolve_route(ROUTES, "4:+14165551234") == AgentRoute("proj", "agent-a", "10")


def test_resolve_miss():
    assert resolve_route(ROUTES, "+14165550000") is None
    assert resolve_route(ROUTES, None) is None
    assert resolve_route(ROUTES, "8:acs:resource_user-id") is None


def test_event_extraction_prefers_phone_number_then_raw_id():
    data = {
        "to": {"kind": "phoneNumber", "rawId": "4:+10000000000", "phoneNumber": {"value": "+14165551234"}},
        "from": {"rawId": "4:+16475559876"},
    }
    assert called_number_from_event(data) == "+14165551234"
    assert caller_number_from_event(data) == "4:+16475559876"


def test_event_extraction_missing_fields():
    assert called_number_from_event({}) is None
    assert caller_number_from_event({"from": None}) is None
