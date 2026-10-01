"""Binding validation (spec section 4.1, plan P17): every rule fails closed and
never echoes a value, of the bad binding or of any other binding."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from calendar_tools.core.bindings import Binding, BindingConfigError, load_bindings

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL-VALUE-9"
OTHER_SENTINEL = "SENTINEL-OTHER-7"
PROVIDERS = frozenset({"fake"})

BASE: dict = {
    "enabled": True,
    "provider": "fake",
    "calendar_id": "primary",
    "credential_secret_name": "cal-binding-test-alpha-fake",
    "timezone": "America/Toronto",
    "locale": "en-CA",
    "bookable_hours": {
        "mon": [["09:00", "12:00"], ["13:00", "17:00"]],
        "tue": [["09:00", "17:00"]],
    },
    "default_duration_minutes": 30,
    "allowed_durations_minutes": [30],
    "slot_step_minutes": 30,
    "buffer_minutes": 0,
    "min_notice_minutes": 120,
    "max_days_ahead": 14,
    "default_search_days": 3,
    "max_slots_returned": 6,
    "slot_selection": "spread",
    "slot_token_ttl_minutes": 30,
    "appointment_types": [
        {"id": "phone_call", "label": "Phone call"},
        {"id": "in_person", "label": "In-person meeting"},
    ],
    "required_contact_fields": ["name", "phone"],
    "default_phone_region": "CA",
    "accept_notes": False,
    "max_active_bookings_per_contact": 2,
    "host_display_name": "Sam Sample",
    "event_title_template": "{appointment_type_label}: {contact_name}",
    "event_description_template": "Phone: {contact_phone}\n{notes_block}Ref: {booking_ref}",
    "allowed_principals": ["00000000-0000-0000-0000-0000000000a1"],
}

REQUIRED_FIELDS = [k for k in BASE if k != "host_display_name"]


def _other_binding() -> dict:
    """A second, valid binding full of sentinel data that must never leak."""
    other = copy.deepcopy(BASE)
    other["calendar_id"] = OTHER_SENTINEL
    other["host_display_name"] = OTHER_SENTINEL
    other["allowed_principals"] = [OTHER_SENTINEL]
    return other


def _doc(binding: dict, binding_id: str = "test-alpha") -> str:
    return json.dumps({"bindings": {"test-beta": _other_binding(), binding_id: binding}})


def _load(raw: str):
    return load_bindings(raw, PROVIDERS)


def _set(field: str, value) -> Callable[[dict], None]:
    def mutate(b: dict) -> None:
        b[field] = value

    return mutate


def _hours(value) -> Callable[[dict], None]:
    return _set("bookable_hours", value)


def _durations(default: int, allowed: list, buffer: int = 0) -> Callable[[dict], None]:
    def mutate(b: dict) -> None:
        b["default_duration_minutes"] = default
        b["allowed_durations_minutes"] = allowed
        b["buffer_minutes"] = buffer
        b["slot_step_minutes"] = 5

    return mutate


# (id, mutation, expected field, bad value that must not be echoed)
FIELD_CASES = [
    ("provider-unknown", _set("provider", SENTINEL), "provider", SENTINEL),
    ("provider-not-str", _set("provider", 7), "provider", None),
    ("calendar-id-empty", _set("calendar_id", ""), "calendar_id", None),
    ("secret-name-bad", _set("credential_secret_name", SENTINEL + " x"), "credential_secret_name", SENTINEL),
    ("timezone-unknown", _set("timezone", "Mars/" + SENTINEL), "timezone", SENTINEL),
    ("timezone-traversal", _set("timezone", "../" + SENTINEL), "timezone", SENTINEL),
    ("timezone-empty", _set("timezone", ""), "timezone", None),
    ("hours-not-object", _hours([SENTINEL]), "bookable_hours", SENTINEL),
    ("hours-unknown-weekday", _hours({SENTINEL: [["09:00", "10:00"]]}), "bookable_hours", SENTINEL),
    ("hours-malformed-time", _hours({"mon": [[SENTINEL, "10:00"]]}), "bookable_hours", SENTINEL),
    ("hours-hour-25", _hours({"mon": [["09:00", "25:00"]]}), "bookable_hours", None),
    ("hours-one-element", _hours({"mon": [["09:00"]]}), "bookable_hours", None),
    ("hours-day-not-list", _hours({"mon": "09:00-10:00"}), "bookable_hours", None),
    ("hours-inverted", _hours({"mon": [["12:00", "09:00"]]}), "bookable_hours", None),
    ("hours-empty-window", _hours({"mon": [["09:00", "09:00"]]}), "bookable_hours", None),
    (
        "hours-overlapping",
        _hours({"mon": [["09:00", "12:00"], ["11:00", "13:00"]]}),
        "bookable_hours",
        None,
    ),
    (
        "hours-overlapping-unsorted",
        _hours({"mon": [["11:00", "13:00"], ["09:00", "12:00"]]}),
        "bookable_hours",
        None,
    ),
    ("hours-boundary-not-5", _hours({"mon": [["09:03", "10:00"]]}), "bookable_hours", None),
    ("default-not-allowed", _set("default_duration_minutes", 45), "default_duration_minutes", None),
    ("duration-not-5", _durations(32, [32]), "allowed_durations_minutes", None),
    ("duration-zero", _durations(30, [0, 30]), "allowed_durations_minutes", None),
    ("duration-negative", _durations(30, [-5, 30]), "allowed_durations_minutes", None),
    ("durations-empty", _durations(30, []), "allowed_durations_minutes", None),
    ("durations-not-list", _set("allowed_durations_minutes", 30), "allowed_durations_minutes", None),
    ("duration-bool", _durations(30, [True, 30]), "allowed_durations_minutes", None),
    ("step-not-5", _set("slot_step_minutes", 7), "slot_step_minutes", None),
    ("step-zero", _set("slot_step_minutes", 0), "slot_step_minutes", None),
    ("step-negative", _set("slot_step_minutes", -5), "slot_step_minutes", None),
    ("buffer-not-5", _set("buffer_minutes", 3), "buffer_minutes", None),
    ("buffer-negative", _set("buffer_minutes", -5), "buffer_minutes", None),
    ("max-slots-0", _set("max_slots_returned", 0), "max_slots_returned", None),
    ("max-slots-11", _set("max_slots_returned", 11), "max_slots_returned", None),
    ("max-slots-str", _set("max_slots_returned", "6"), "max_slots_returned", None),
    ("max-slots-bool", _set("max_slots_returned", True), "max_slots_returned", None),
    ("max-days-0", _set("max_days_ahead", 0), "max_days_ahead", None),
    ("max-days-61", _set("max_days_ahead", 61), "max_days_ahead", None),
    ("search-days-0", _set("default_search_days", 0), "default_search_days", None),
    ("min-notice-negative", _set("min_notice_minutes", -1), "min_notice_minutes", None),
    ("ttl-zero", _set("slot_token_ttl_minutes", 0), "slot_token_ttl_minutes", None),
    ("limit-zero", _set("max_active_bookings_per_contact", 0), "max_active_bookings_per_contact", None),
    ("over-240", _durations(245, [245]), "allowed_durations_minutes", None),
    ("over-240-with-buffer", _durations(200, [200], buffer=45), "allowed_durations_minutes", None),
    # P17 budget guard at the initial constants: 11 cells = 5.1 s > 8 - 3.
    ("p17-55-plus-0", _durations(55, [55]), "allowed_durations_minutes", None),
    ("p17-45-plus-10", _durations(45, [45], buffer=10), "allowed_durations_minutes", None),
    ("p17-largest-not-default", _durations(30, [30, 55]), "allowed_durations_minutes", None),
    ("type-id-uppercase", _set("appointment_types", [{"id": "Phone", "label": "x"}]), "appointment_types", None),
    ("type-id-too-short", _set("appointment_types", [{"id": "p", "label": "x"}]), "appointment_types", None),
    ("type-id-sentinel", _set("appointment_types", [{"id": SENTINEL, "label": "x"}]), "appointment_types", SENTINEL),
    (
        "type-id-duplicate",
        _set("appointment_types", [{"id": "phone_call", "label": "a"}, {"id": "phone_call", "label": "b"}]),
        "appointment_types",
        None,
    ),
    ("types-empty", _set("appointment_types", []), "appointment_types", None),
    ("type-label-empty", _set("appointment_types", [{"id": "phone_call", "label": ""}]), "appointment_types", None),
    ("type-not-object", _set("appointment_types", [SENTINEL]), "appointment_types", SENTINEL),
    ("title-unknown-placeholder", _set("event_title_template", "{" + "sentinel_value_9" + "}"), "event_title_template", "sentinel_value_9"),
    (
        "description-unknown-placeholder",
        _set("event_description_template", "Ref {booking_ref} {" + "sentinel_value_9" + "}"),
        "event_description_template",
        "sentinel_value_9",
    ),
    ("title-format-spec", _set("event_title_template", "{contact_name:>99}"), "event_title_template", None),
    ("title-conversion", _set("event_title_template", "{contact_name!r}"), "event_title_template", None),
    ("title-attribute", _set("event_title_template", "{contact_name.__class__}"), "event_title_template", None),
    ("title-positional", _set("event_title_template", "{}"), "event_title_template", None),
    ("title-unbalanced", _set("event_title_template", "{contact_name"), "event_title_template", None),
    ("principals-empty", _set("allowed_principals", []), "allowed_principals", None),
    ("principals-blank", _set("allowed_principals", [""]), "allowed_principals", None),
    ("contact-fields-no-name", _set("required_contact_fields", ["phone"]), "required_contact_fields", None),
    ("contact-fields-no-phone-or-email", _set("required_contact_fields", ["name"]), "required_contact_fields", None),
    ("contact-fields-unknown", _set("required_contact_fields", ["name", "phone", SENTINEL]), "required_contact_fields", SENTINEL),
    ("locale-not-english", _set("locale", "fr-CA"), "locale", None),
    ("locale-sentinel", _set("locale", SENTINEL), "locale", SENTINEL),
    ("slot-selection", _set("slot_selection", SENTINEL), "slot_selection", SENTINEL),
    ("phone-region-unknown", _set("default_phone_region", "ZZ"), "default_phone_region", None),
    ("enabled-not-bool", _set("enabled", "yes"), "enabled", None),
    ("accept-notes-not-bool", _set("accept_notes", 1), "accept_notes", None),
    ("host-name-empty", _set("host_display_name", ""), "host_display_name", None),
]


def _assert_safe(err: BindingConfigError, bad, caplog) -> None:
    text = str(err)
    assert SENTINEL not in text
    assert OTHER_SENTINEL not in text
    assert SENTINEL not in caplog.text
    assert OTHER_SENTINEL not in caplog.text
    if isinstance(bad, str):
        assert bad not in text
    # str() holds exactly the binding ID and the field, nothing else.
    assert text == str(BindingConfigError(err.binding_id, err.field))


@pytest.mark.parametrize(("mutate", "field", "bad"), [c[1:] for c in FIELD_CASES], ids=[c[0] for c in FIELD_CASES])
def test_each_rule_fails_closed_without_echo(mutate, field, bad, caplog):
    caplog.set_level(logging.DEBUG)
    binding = copy.deepcopy(BASE)
    mutate(binding)
    with pytest.raises(BindingConfigError) as info:
        _load(_doc(binding))
    err = info.value
    assert err.field == field
    assert err.binding_id == "test-alpha"
    _assert_safe(err, bad, caplog)


BAD_IDS = [
    ("too-short", "ab"),
    ("uppercase", SENTINEL),
    ("leading-dash", "-test-alpha"),
    ("41-chars", "a" * 41),
    ("underscore", "test_alpha"),
    ("phone-like-run", "call-" + "613" + "5550123"),
    ("dashed-phone", "agent-" + "-".join(["613", "555", "0123"])),
    ("dashed-seven-digits", "id-123-4567"),
    ("seven-digits", "id-1234567"),
    ("email-like", "someone@example.com"),
]


@pytest.mark.parametrize("bad_id", [b[1] for b in BAD_IDS], ids=[b[0] for b in BAD_IDS])
def test_binding_id_rules(bad_id, caplog):
    with pytest.raises(BindingConfigError) as info:
        _load(_doc(copy.deepcopy(BASE), binding_id=bad_id))
    err = info.value
    # The bad ID is itself the value, so it is never reported.
    assert err.binding_id is None
    assert err.field == "binding_id"
    assert bad_id not in str(err)
    _assert_safe(err, bad_id, caplog)


def test_binding_id_boundaries_accepted():
    for good in ("abc", "a" * 40, "test-123456"):  # 6 digits is fine, 7 is not
        assert good in _load(_doc(copy.deepcopy(BASE), binding_id=good))


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field(field, caplog):
    binding = copy.deepcopy(BASE)
    del binding[field]
    with pytest.raises(BindingConfigError) as info:
        _load(_doc(binding))
    assert info.value.field == field
    assert info.value.binding_id == "test-alpha"
    _assert_safe(info.value, None, caplog)


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ("[]", "$"),
        ('"' + SENTINEL + '"', "$"),
        ("42", "$"),
        ("{" + SENTINEL, "$"),
        ("", "$"),
        (json.dumps({"bindings": [SENTINEL]}), "bindings"),
        (json.dumps({"other": {}}), "bindings"),
        (json.dumps({"bindings": {}}), "bindings"),
    ],
    ids=["array", "string", "number", "invalid-json", "empty", "bindings-array", "bindings-missing", "bindings-empty"],
)
def test_document_shape(raw, field, caplog):
    with pytest.raises(BindingConfigError) as info:
        _load(raw)
    assert info.value.binding_id is None
    assert info.value.field == field
    _assert_safe(info.value, None, caplog)


def test_binding_not_an_object():
    with pytest.raises(BindingConfigError) as info:
        _load(json.dumps({"bindings": {"test-alpha": [SENTINEL]}}))
    assert info.value.binding_id == "test-alpha"
    assert info.value.field == "$"
    assert SENTINEL not in str(info.value)


def test_invalid_json_does_not_chain_the_decoder_error():
    with pytest.raises(BindingConfigError) as info:
        _load("{" + SENTINEL)
    # The decoder message would quote the document; it must not ride along.
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_p17_fifty_minutes_accepted():
    binding = copy.deepcopy(BASE)
    _durations(50, [50])(binding)
    assert _load(_doc(binding))["test-alpha"].allowed_durations_minutes == (50,)


def test_p17_forty_plus_ten_accepted():
    binding = copy.deepcopy(BASE)
    _durations(40, [40], buffer=10)(binding)
    assert _load(_doc(binding))["test-alpha"].buffer_minutes == 10


def test_valid_binding_parsed():
    bindings = _load(_doc(copy.deepcopy(BASE)))
    b = bindings["test-alpha"]
    assert isinstance(b, Binding)
    assert b.binding_id == "test-alpha"
    assert b.enabled is True
    assert b.timezone == ZoneInfo("America/Toronto")
    assert b.bookable_hours["mon"] == ((time(9), time(12)), (time(13), time(17)))
    assert b.bookable_hours["tue"] == ((time(9), time(17)),)
    assert "wed" not in b.bookable_hours or b.bookable_hours["wed"] == ()
    assert b.allowed_durations_minutes == (30,)
    assert [t.id for t in b.appointment_types] == ["phone_call", "in_person"]
    assert b.appointment_types[0].label == "Phone call"
    assert b.required_contact_fields == frozenset({"name", "phone"})
    assert b.allowed_principals == frozenset({"00000000-0000-0000-0000-0000000000a1"})
    assert b.host_display_name == "Sam Sample"
    assert b.calendar_ref.provider == "fake"
    assert b.calendar_ref.calendar_id == "primary"


def test_result_is_read_only_and_frozen():
    bindings = _load(_doc(copy.deepcopy(BASE)))
    with pytest.raises(TypeError):
        bindings["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        bindings["test-alpha"].bookable_hours["mon"] = ()  # type: ignore[index]
    with pytest.raises(AttributeError):
        bindings["test-alpha"].__setattr__("enabled", False)


def test_touching_windows_accepted():
    binding = copy.deepcopy(BASE)
    binding["bookable_hours"] = {"mon": [["13:00", "15:00"], ["09:00", "13:00"]]}
    b = _load(_doc(binding))["test-alpha"]
    assert b.bookable_hours["mon"] == ((time(9), time(13)), (time(13), time(15)))


def test_closed_day_accepted():
    binding = copy.deepcopy(BASE)
    binding["bookable_hours"] = {"mon": [], "tue": [["09:00", "10:00"]]}
    assert _load(_doc(binding))["test-alpha"].bookable_hours["mon"] == ()


def test_host_display_name_absent_is_accepted():
    binding = copy.deepcopy(BASE)
    del binding["host_display_name"]
    assert _load(_doc(binding))["test-alpha"].host_display_name is None


def test_email_only_contact_fields_accepted():
    binding = copy.deepcopy(BASE)
    binding["required_contact_fields"] = ["name", "email"]
    assert _load(_doc(binding))["test-alpha"].required_contact_fields == frozenset({"name", "email"})


def test_every_documented_placeholder_accepted():
    binding = copy.deepcopy(BASE)
    binding["event_description_template"] = (
        "{contact_name} {contact_phone} {contact_email} {appointment_type_label} "
        "{duration_minutes} {booking_ref} {notes_block} {{literal braces}}"
    )
    assert _load(_doc(binding))["test-alpha"]


def test_disabled_binding_loads_marked_disabled():
    binding = copy.deepcopy(BASE)
    binding["enabled"] = False
    b = _load(_doc(binding))["test-alpha"]
    assert b.enabled is False


def test_misspelt_binding_field_rejected_by_name(caplog):
    # A misspelt optional field would otherwise be dropped silently.
    binding = copy.deepcopy(BASE)
    binding["host_display_nam"] = SENTINEL
    with pytest.raises(BindingConfigError) as info:
        _load(_doc(binding))
    assert info.value.field == "host_display_nam"
    assert info.value.binding_id == "test-alpha"
    _assert_safe(info.value, SENTINEL, caplog)


def test_unknown_non_identifier_key_not_echoed(caplog):
    binding = copy.deepcopy(BASE)
    binding[SENTINEL] = 1
    with pytest.raises(BindingConfigError) as info:
        _load(_doc(binding))
    assert info.value.field == "$"
    _assert_safe(info.value, SENTINEL, caplog)


@pytest.mark.parametrize(
    "raw",
    [
        '{"bindings": {"test-alpha": {}, "test-alpha": {}}}',
        '{"bindings": {}, "bindings": {}}',
    ],
    ids=["duplicate-binding-id", "duplicate-top-level"],
)
def test_duplicate_keys_rejected(raw):
    with pytest.raises(BindingConfigError) as info:
        _load(raw)
    assert info.value.binding_id is None
    assert info.value.field == "$"


def test_duplicate_field_inside_binding_rejected():
    text = _doc(copy.deepcopy(BASE))
    # Repeat one field inside test-alpha's object: the earlier value must not be dropped silently.
    text = text.replace('"buffer_minutes": 0', '"buffer_minutes": 0, "buffer_minutes": 10', 1)
    assert text.count('"buffer_minutes"') == 3  # test-beta's, plus the two in test-alpha
    with pytest.raises(BindingConfigError) as info:
        _load(text)
    assert info.value.field == "$"


def test_sample_file_loads_with_fake_provider():
    doc = json.loads((ROOT / "bindings.sample.json").read_text(encoding="utf-8"))
    for binding in doc["bindings"].values():
        binding["provider"] = "fake"
    bindings = _load(json.dumps(doc))
    assert set(bindings) == {"sample-alpha", "sample-beta"}
    assert bindings["sample-beta"].host_display_name is None
    assert bindings["sample-beta"].slot_selection == "earliest"


def test_sample_file_rejects_unregistered_provider():
    raw = (ROOT / "bindings.sample.json").read_text(encoding="utf-8")
    with pytest.raises(BindingConfigError) as info:
        _load(raw)
    assert info.value.field == "provider"


def test_error_str_shape():
    err = BindingConfigError("test-alpha", "timezone")
    assert "test-alpha" in str(err)
    assert "timezone" in str(err)
    assert err.binding_id == "test-alpha"
    assert err.field == "timezone"
    assert BindingConfigError(None, "$").binding_id is None
