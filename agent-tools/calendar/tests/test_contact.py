"""Contact normalization (spec 5.3 contact rows, 7.4 step 1, 9.3; plan UC04a,
P15). All numbers are fictional NANP 555-01xx lines; all names are fictional."""

from __future__ import annotations

import json
from typing import Any

import phonenumbers
import pytest

from calendar_tools.core.bindings import Binding, load_bindings
from calendar_tools.core.contact import MAX_NAME_CHARS, Contact, InvalidFields, normalize
from tests.fakes import jwt_tokens as jt


def binding_doc(**changes: Any) -> dict[str, Any]:
    b: dict[str, Any] = {
        "enabled": True,
        "provider": "fake",
        "calendar_id": "primary",
        "credential_secret_name": "cal-binding-test-alpha-fake",
        "timezone": "America/Toronto",
        "locale": "en-CA",
        "bookable_hours": {"mon": [["14:00", "19:00"]]},
        "default_duration_minutes": 30,
        "allowed_durations_minutes": [30],
        "slot_step_minutes": 30,
        "buffer_minutes": 0,
        "min_notice_minutes": 60,
        "max_days_ahead": 7,
        "default_search_days": 3,
        "max_slots_returned": 6,
        "slot_selection": "spread",
        "slot_token_ttl_minutes": 30,
        "appointment_types": [{"id": "phone_call", "label": "Phone call"}],
        "required_contact_fields": ["name", "phone"],
        "default_phone_region": "CA",
        "accept_notes": False,
        "max_active_bookings_per_contact": 2,
        "event_title_template": "{appointment_type_label}: {contact_name}",
        "event_description_template": "Ref: {booking_ref}",
        "allowed_principals": [jt.PRINCIPAL_A],
    }
    b.update(changes)
    return b


def make_binding(**changes: Any) -> Binding:
    doc = {"bindings": {"test-alpha": binding_doc(**changes)}}
    return load_bindings(json.dumps(doc), frozenset({"fake"}))["test-alpha"]


CA = make_binding()
GB = make_binding(default_phone_region="GB")


def ok(binding: Binding, raw: Any) -> Contact:
    result = normalize(binding, raw)
    assert isinstance(result, Contact), result
    return result


def bad(binding: Binding, raw: Any) -> list[str]:
    result = normalize(binding, raw)
    assert isinstance(result, InvalidFields), result
    return list(result.fields)


# --- phone ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["+1 613 555 0123", "+16135550123", "(613) 555-0123", "613-555-0123", "613.555.0123", "1 613 555 0123"],
)
def test_formatted_and_national_numbers_become_e164(raw):
    assert ok(CA, {"phone": raw}).phone == "+16135550123"


def test_region_is_the_binding_default():
    # An international number ignores the region; a national one is read in it.
    assert ok(GB, {"phone": "+1 613 555 0123"}).phone == "+16135550123"
    under_gb = normalize(GB, {"phone": "613 555 0123", "email": "jordan@example.com"})
    assert not (isinstance(under_gb, Contact) and under_gb.phone == "+16135550123")


def test_installed_phonenumbers_accepts_the_fictional_555_01xx_range():
    # P15: records the installed library's behavior for the fictional range.
    # Normalization relies only on is_possible_number.
    parsed = phonenumbers.parse("+1 613 555 0123", "CA")
    assert phonenumbers.is_possible_number(parsed) is True
    assert ok(CA, {"phone": "+1 613 555 0123"}).phone == "+16135550123"


@pytest.mark.parametrize(
    "raw",
    ["not a number", "12", "+", "555", "16135550123999999", "☎☎☎", "0" * 41],
)
def test_unparseable_or_impossible_phone_is_invalid(raw):
    assert bad(CA, {"phone": raw, "email": "jordan@example.com"}) == ["contact.phone"]


def test_phone_with_letters_is_invalid_not_vanity_converted():
    # A vanity number would turn letters into digits; the service never guesses.
    assert bad(CA, {"phone": "613-555-CALL", "email": "jordan@example.com"}) == ["contact.phone"]


@pytest.mark.parametrize("raw", [123, 1.5, True, ["+16135550123"], {"n": 1}])
def test_non_string_phone_is_invalid(raw):
    assert bad(CA, {"phone": raw, "email": "jordan@example.com"}) == ["contact.phone"]


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_or_blank_phone_counts_as_absent(raw):
    c = ok(CA, {"phone": raw, "email": "jordan@example.com"})
    assert c.phone is None


# --- identity presence ------------------------------------------------------------


@pytest.mark.parametrize("raw", [{}, {"name": "Jordan Example"}, {"phone": "", "email": " "}, None])
def test_neither_phone_nor_email_reports_both_fields(raw):
    assert bad(CA, raw) == ["contact.phone", "contact.email"]


@pytest.mark.parametrize("raw", ["Jordan", 3, ["x"]])
def test_contact_not_an_object_is_invalid(raw):
    assert bad(CA, raw) == ["contact"]


def test_identity_is_phone_when_present_else_lowercased_email():
    assert ok(CA, {"phone": "613 555 0123", "email": "Jordan@Example.com"}).identity == "+16135550123"
    assert ok(CA, {"email": "Jordan@Example.com"}).identity == "jordan@example.com"


def test_required_contact_fields_are_not_checked_here():
    # The binding requires name and phone, but that is a step-4 check (it
    # depends on current config); normalize only enforces an identity.
    c = ok(CA, {"email": "jordan@example.com"})
    assert c.name is None and c.phone is None


# --- name -----------------------------------------------------------------------


def test_name_trimmed():
    assert ok(CA, {"name": "  Jordan Example  ", "phone": "6135550123"}).name == "Jordan Example"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jordan\nExample", "Jordan Example"),
        ("Jordan\r\nExample", "Jordan Example"),
        ("Jordan\tExample", "Jordan Example"),
        ("Jordan\x00\x07Example", "JordanExample"),
        ("Jordan\u2028Example", "Jordan Example"),
        ("Jordan\u202eExample", "JordanExample"),
        ("Jordan\x85Example", "Jordan Example"),
        ("Jordan Example\n", "Jordan Example"),
        ("Jordan \n\n  Example", "Jordan Example"),
    ],
    ids=["lf", "crlf", "tab", "nul-bel", "line-sep", "bidi-override", "nel", "trailing-newline", "collapse"],
)
def test_name_control_characters_and_newlines_stripped(raw, expected):
    assert ok(CA, {"name": raw, "phone": "6135550123"}).name == expected


def test_name_of_80_chars_accepted_and_81_rejected():
    assert MAX_NAME_CHARS == 80
    assert ok(CA, {"name": "J" * 80, "phone": "6135550123"}).name == "J" * 80
    assert bad(CA, {"name": "J" * 81, "phone": "6135550123"}) == ["contact.name"]


def test_name_length_counted_after_stripping():
    assert ok(CA, {"name": "  " + "J" * 80 + "\n\n", "phone": "6135550123"}).name == "J" * 80


@pytest.mark.parametrize("raw", ["", "   ", "\n\t", "\x00"])
def test_name_empty_after_trim_rejected(raw):
    assert bad(CA, {"name": raw, "phone": "6135550123"}) == ["contact.name"]


@pytest.mark.parametrize("raw", [3, ["Jordan"], {"n": "x"}, True])
def test_name_not_a_string_rejected(raw):
    assert bad(CA, {"name": raw, "phone": "6135550123"}) == ["contact.name"]


def test_absent_name_is_none():
    assert ok(CA, {"phone": "6135550123"}).name is None
    assert ok(CA, {"name": None, "phone": "6135550123"}).name is None


def test_name_keeps_letters_from_other_scripts():
    assert ok(CA, {"name": "Zoë Ñame", "phone": "6135550123"}).name == "Zoë Ñame"


# --- email ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["jordan@example.com", "Jordan.Example+tag@mail.example.org", " jordan@example.com "],
)
def test_valid_email_accepted_and_trimmed(raw):
    assert ok(CA, {"email": raw}).email == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [
        "jordan",
        "jordan@",
        "@example.com",
        "jordan@example",
        "jordan@@example.com",
        "jor dan@example.com",
        "jordan@exa mple.com",
        "jordan@example.com\nBcc: x@example.com",
        "jordan@.example.com",
        "jordan@example.com.",
        "a" * 250 + "@example.com",
        3,
    ],
)
def test_invalid_email_rejected(raw):
    assert bad(CA, {"email": raw, "phone": "6135550123"}) == ["contact.email"]


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_blank_email_counts_as_absent(raw):
    assert ok(CA, {"email": raw, "phone": "6135550123"}).email is None


# --- several errors and unknown keys ----------------------------------------------


def test_every_invalid_field_is_reported_in_a_fixed_order():
    assert bad(CA, {"name": "", "phone": "nope", "email": "nope"}) == [
        "contact.name",
        "contact.phone",
        "contact.email",
    ]


def test_invalid_phone_with_no_email_reports_the_phone_only():
    assert bad(CA, {"phone": "nope"}) == ["contact.phone"]


def test_unknown_contact_keys_are_ignored():
    assert ok(CA, {"phone": "6135550123", "nickname": "J"}).phone == "+16135550123"


# --- privacy ----------------------------------------------------------------------


def test_contact_repr_and_str_hold_no_personal_data():
    c = ok(CA, {"name": "Jordan Example", "phone": "+16135550123", "email": "jordan@example.com"})
    for text in (repr(c), str(c)):
        assert "Jordan" not in text
        assert "6135550123" not in text
        assert "jordan@example.com" not in text


def test_invalid_fields_hold_field_names_only():
    result = normalize(CA, {"name": "Jordan\x00" * 20, "phone": "613 555 0123 999", "email": "x"})
    assert isinstance(result, InvalidFields)
    assert "Jordan" not in repr(result)
    assert "613" not in repr(result)


def test_contact_is_frozen():
    c = ok(CA, {"phone": "6135550123"})
    with pytest.raises(AttributeError):
        c.phone = "+16135550199"  # type: ignore[misc]
