"""The canonical contract document (spec sections 4.3, 5)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "openapi" / "calendar-tools.openapi.yaml"
EXAMPLES = ROOT / "tests" / "data" / "spec_examples"
PLACEHOLDER_URL = "https://example.invalid/api/v1/bindings/BINDING_ID"

# Spec section 5.4, copied byte for byte.
CHECK_DESCRIPTION = (
    "Finds open appointment times on the calendar connected to this tool. Returns only times "
    "that are actually free now and inside the calendar's bookable hours. Each slot includes a "
    "slot_id, which book_appointment requires. Times are in the calendar's timezone and include "
    "a UTC offset. Call without from_date to search starting today. The response includes "
    "today's date and weekday in the calendar's timezone, which you can use to work out relative "
    'dates such as "tomorrow" or "next Tuesday". status "no_availability" means the range was '
    "searched and nothing is free; a different range may have openings. status "
    '"calendar_unavailable" means availability could not be checked, so no times are known; do '
    "not state or guess any times."
)
BOOK_DESCRIPTION = (
    "Books one slot returned by check_availability. Send the slot's slot_id and start exactly as "
    "returned, an appointment_type id from the check_availability response, and the contact's "
    'details. Only status "booked" means an appointment now exists. Every other status means no '
    'appointment was made, except "booking_unconfirmed", which means it is unknown whether one '
    'was made. Do not describe an appointment as booked unless status is "booked". Retrying with '
    "identical inputs is safe; it is designed not to create a duplicate booking."
)
SLOT_ID_DESCRIPTION = (
    "Opaque token from a check_availability slot. Copy it exactly; do not construct or modify it."
)

CHECK_STATUSES = {"available", "no_availability", "calendar_unavailable", "invalid_request"}
BOOK_STATUSES = {
    "booked",
    "slot_unavailable",
    "invalid_slot",
    "limit_reached",
    "invalid_request",
    "calendar_unavailable",
    "booking_unconfirmed",
}
CHECK_REASONS = {"fully_booked", "outside_bookable_hours", "beyond_booking_horizon"}
BOOK_REASONS = {
    "taken",
    "too_soon",
    "cancelled",
    "expired",
    "malformed",
    "wrong_binding",
    "start_mismatch",
    "outside_bookable_hours",
    "duration_not_allowed",
}


@pytest.fixture(scope="module")
def doc() -> dict:
    return yaml.safe_load(DOC_PATH.read_text(encoding="utf-8"))


def _resolve(doc: dict, schema: dict) -> dict:
    """Inline every local `$ref` so jsonschema sees one self-contained schema."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return _resolve(doc, doc["components"]["schemas"][name])
        return {k: _resolve(doc, v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_resolve(doc, v) for v in schema]
    return schema


def _to_json_schema(schema):
    """OpenAPI 3.0 `nullable: true` -> JSON Schema type union."""
    if isinstance(schema, dict):
        out = {k: _to_json_schema(v) for k, v in schema.items() if k != "nullable"}
        if schema.get("nullable") and "type" in schema:
            out["type"] = [schema["type"], "null"]
        return out
    if isinstance(schema, list):
        return [_to_json_schema(v) for v in schema]
    return schema


def _operation(doc: dict, op_id: str) -> dict:
    for path_item in doc["paths"].values():
        for op in path_item.values():
            if isinstance(op, dict) and op.get("operationId") == op_id:
                return op
    raise AssertionError(op_id)


def _request_schema(doc: dict, op_id: str) -> dict:
    op = _operation(doc, op_id)
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    return _to_json_schema(_resolve(doc, schema))


def _response_schema(doc: dict, op_id: str) -> dict:
    op = _operation(doc, op_id)
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    return _to_json_schema(_resolve(doc, schema))


def test_document_validates(doc):
    validate(doc)
    assert doc["openapi"] == "3.0.3"


def test_placeholder_server_url(doc):
    assert doc["servers"][0]["url"] == PLACEHOLDER_URL
    assert len(doc["servers"]) == 1


def test_paths_and_operation_ids(doc):
    assert set(doc["paths"]) == {"/check-availability", "/book-appointment"}
    assert doc["paths"]["/check-availability"]["post"]["operationId"] == "check_availability"
    assert doc["paths"]["/book-appointment"]["post"]["operationId"] == "book_appointment"


def test_operation_descriptions_verbatim(doc):
    assert _operation(doc, "check_availability")["description"] == CHECK_DESCRIPTION
    assert _operation(doc, "book_appointment")["description"] == BOOK_DESCRIPTION


def test_slot_id_descriptions_verbatim(doc):
    schemas = doc["components"]["schemas"]
    assert schemas["Slot"]["properties"]["slot_id"]["description"] == SLOT_ID_DESCRIPTION
    book = schemas["BookAppointmentRequest"]["properties"]["slot_id"]
    assert book["description"] == SLOT_ID_DESCRIPTION


def test_status_enums(doc):
    schemas = doc["components"]["schemas"]
    assert set(schemas["CheckAvailabilityResponse"]["properties"]["status"]["enum"]) == CHECK_STATUSES
    assert set(schemas["BookAppointmentResponse"]["properties"]["status"]["enum"]) == BOOK_STATUSES


def test_detail_reason_enums(doc):
    schemas = doc["components"]["schemas"]
    assert set(schemas["CheckAvailabilityDetail"]["properties"]["reason"]["enum"]) == CHECK_REASONS
    assert set(schemas["BookAppointmentDetail"]["properties"]["reason"]["enum"]) == BOOK_REASONS


def test_bearer_security_scheme_documented(doc):
    scheme = doc["components"]["securitySchemes"]["bearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"


def test_book_request_required_fields(doc):
    required = set(doc["components"]["schemas"]["BookAppointmentRequest"]["required"])
    assert required == {"slot_id", "start", "appointment_type", "contact"}


def test_check_request_all_optional(doc):
    assert "required" not in doc["components"]["schemas"]["CheckAvailabilityRequest"]


def test_request_id_required_on_every_response(doc):
    schemas = doc["components"]["schemas"]
    for name in ("CheckAvailabilityResponse", "BookAppointmentResponse"):
        assert set(schemas[name]["required"]) >= {"status", "request_id"}


RESPONSE_EXAMPLES = [
    ("check_available.json", "check_availability"),
    ("check_no_availability.json", "check_availability"),
    ("check_calendar_unavailable.json", "check_availability"),
    ("check_invalid_request.json", "check_availability"),
    ("book_booked.json", "book_appointment"),
]
REQUEST_EXAMPLES = [
    ("check_request.json", "check_availability"),
    ("book_request.json", "book_appointment"),
]


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


FORMATS = jsonschema.FormatChecker()


@pytest.mark.parametrize(("name", "op_id"), RESPONSE_EXAMPLES)
def test_spec_response_examples_validate(doc, name, op_id):
    jsonschema.validate(_example(name), _response_schema(doc, op_id), format_checker=FORMATS)


@pytest.mark.parametrize(("name", "op_id"), REQUEST_EXAMPLES)
def test_spec_request_examples_validate(doc, name, op_id):
    jsonschema.validate(_example(name), _request_schema(doc, op_id), format_checker=FORMATS)


def test_format_checker_rejects_a_naive_datetime(doc):
    bad = _example("check_available.json")
    bad["slots"][0]["start"] = "2026-10-05T10:00:00"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _response_schema(doc, "check_availability"), format_checker=FORMATS)


def test_every_example_file_is_exercised():
    covered = {n for n, _ in RESPONSE_EXAMPLES + REQUEST_EXAMPLES}
    assert {p.name for p in EXAMPLES.glob("*.json")} == covered


def test_schema_rejects_unknown_status(doc):
    bad = _example("check_calendar_unavailable.json") | {"status": "maybe"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _response_schema(doc, "check_availability"))


def test_schema_rejects_unknown_reason(doc):
    bad = _example("check_no_availability.json")
    bad["detail"] = {"reason": "closed"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _response_schema(doc, "check_availability"))


def test_book_request_without_slot_id_rejected(doc):
    bad = _example("book_request.json")
    del bad["slot_id"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _request_schema(doc, "book_appointment"))


def test_unknown_request_fields_allowed(doc):
    # Spec 5.1 rule 7: unknown request fields are ignored, not rejected.
    extra = _example("check_request.json") | {"extra_field": 1}
    jsonschema.validate(extra, _request_schema(doc, "check_availability"))
