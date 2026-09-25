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


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ""),
        ("", ""),
        ("abc", "abc"),
        ("416555123", "416555123"),
        ("416555123 ext 4", "416555123 ext 4"),
        ("8:acs:x_1234567890ab", "x_1234567890ab"),
        (14165551234, "+14165551234"),
        ("+", "+"),
    ],
)
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
        # dropped-digit NANP forms must not slip through as valid (bare 11-digit -> +11...)
        ("+11416555123", False),
        ("+11234567890", False),
        # non-NANP boundaries: 7 digits after +[2-9] is the floor, 14 is the ceiling
        ("+25551234", True),
        ("+2555123", False),
        ("+25551234567890", True),
        ("+2555123456789012", False),
        # an 11-digit non-NANP number is still valid via the "other" pattern
        ("+25551234567", True),
        # non-ASCII digits and a trailing newline must not pass
        ("+1٤١٥٥٥١٢٣٤", False),
        ("+14165551234\n", False),
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


def test_event_extraction_falls_back_to_raw_id_when_phone_number_value_missing():
    assert called_number_from_event({"to": {"phoneNumber": {}, "rawId": "4:+14165551234"}}) == "4:+14165551234"
    assert called_number_from_event({"to": {"phoneNumber": {"value": None}, "rawId": "4:+14165551234"}}) == "4:+14165551234"


def test_event_extraction_non_dict_identifier_does_not_raise():
    assert called_number_from_event({"to": "4:+14165551234"}) is None
    assert called_number_from_event({"to": {"phoneNumber": "+14165551234"}}) is None
    assert called_number_from_event({"to": []}) is None
