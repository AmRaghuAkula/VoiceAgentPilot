"""Contract: the OpenAPI document (spec section 4, section 9 "Contract")."""

from __future__ import annotations

import json
from pathlib import Path

from sms_notify.core.config import DEFAULT_MAX_CHARS
from sms_notify.core.messages import REASONS
from sms_notify.core.service import ALL_CODES
from sms_notify.http.dispatcher import ROUTE
from tests.test_guard_genericity import phone_hits, word_hits

DOC = json.loads((Path(__file__).resolve().parents[1] / "openapi" / "sms-notify.json").read_text("utf-8"))


def _operations():
    return [(path, method, op) for path, item in DOC["paths"].items() for method, op in item.items()]


def test_exactly_one_operation():
    ops = _operations()
    assert len(ops) == 1
    path, method, op = ops[0]
    assert (path, method, op["operationId"]) == ("/v1/follow-up-sms", "post", "send_follow_up_sms")


def test_route_matches_the_dispatcher():
    base = DOC["servers"][0]["url"]
    assert base.endswith("/api")
    assert ROUTE == "/api" + _operations()[0][0]


def test_server_host_is_a_placeholder():
    assert DOC["servers"][0]["url"] == "https://<function-host>/api"


def test_request_schema_has_only_message():
    schema = _operations()[0][2]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"message"}
    assert schema["required"] == ["message"]
    message = schema["properties"]["message"]
    assert message["type"] == "string" and message["minLength"] == 1
    assert message["maxLength"] == DEFAULT_MAX_CHARS == 480


def test_no_recipient_field_anywhere():
    text = json.dumps(DOC).lower()
    assert '"to"' not in text and '"recipient"' not in text


SPEC_REV_2_2_DESCRIPTION = (
    "Sends one short text message to this business's follow-up contact so a team member can reach the "
    "caller. The recipient is fixed; you supply only the message text. Write only lines of the form "
    '"Label: value", one per line, using only the labels your instructions give, each at most once. '
    "Plain text only: letters, digits, spaces and $ # , + - ( ) ' & %, with a period only as a decimal "
    "point between two digits. Never include a link, web address, email address, social handle or "
    "anything beyond those lines. At most 480 characters (aim for 320 or fewer). Use it once per call, "
    "near the end, after the caller has confirmed their callback number and agreed to a follow-up. "
    "Returns ok true or false. If the code is invalid_request, correct the message as its reason says "
    "and call once more; otherwise never call it again in the same call."
)


def test_t12_description_is_the_spec_rev_2_2_text():
    assert _operations()[0][2]["description"] == SPEC_REV_2_2_DESCRIPTION


def test_t12_reason_enum_is_the_eight_k12_values():
    schema = _operations()[0][2]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["properties"]["reason"]["enum"] == list(REASONS)
    assert "reason" not in schema["required"]


def test_response_envelope_and_codes():
    schema = _operations()[0][2]["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"ok", "code", "retry", "reason"}
    assert set(schema["properties"]["code"]["enum"]) == ALL_CODES
    assert "content_rejected" not in schema["properties"]["code"]["enum"]
    assert schema["properties"]["retry"]["enum"] == [False]
    assert set(_operations()[0][2]["responses"]) == {"200"}


def test_descriptions_pass_g2_and_carry_no_format_labels():
    op = _operations()[0][2]
    texts = [DOC["info"]["description"], op["summary"], op["description"]]
    for text in texts:
        assert not word_hits(text) and not phone_hits(text)
    assert "480" in op["description"]
