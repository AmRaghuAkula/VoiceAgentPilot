"""Log redaction (spec 10, 13.1; plan UC04b `test_log_redaction.py`): a full
check-then-book through the HTTP service with a fictional contact, notes and a
recognizable calendar ID. The captured logs (every logger, every level)
contain none of the name, the phone number (not even masked: the spec 10 line
has no phone field), the notes, the slot token, the event title or
description, or the `calendar_id`."""

from __future__ import annotations

import logging

import pytest

from calendar_tools.core.ports import ProviderTimeout
from tests.booking_world import PHONE
from tests.test_http_book_appointment import BOOK, HttpWorld

NAME = "Jordan Example"
NOTES = "SENTINEL-NOTE"
CALENDAR_ID = "sentinel-calendar-id-zq"


def everything(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.name} {r.levelname} {r.getMessage()} {r.args!r}" for r in caplog.records)


@pytest.mark.parametrize("fail_create", [False, True], ids=["booked", "unconfirmed"])
async def test_logs_hold_no_contact_data_tokens_or_calendar_id(caplog, fail_create) -> None:
    caplog.set_level(logging.DEBUG)
    w = HttpWorld(
        accept_notes=True,
        calendar_id=CALENDAR_ID,
        event_title_template="Meeting: {contact_name}",
        event_description_template="{notes_block}Phone: {contact_phone}",
    )
    if fail_create:
        # The create and its one retry (UC04c) are both uncertain.
        w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
        w.provider.fail_next("create_event", ProviderTimeout(maybe_committed=True))
    slot = await w.offered()
    body = {"slot_id": slot["slot_id"], "start": slot["start"], "appointment_type": "phone_call",
            "contact": {"name": NAME, "phone": "+1 613 555 0123"}, "notes": NOTES, "unknown_field": NAME}
    resp = await w.post(BOOK, body)
    assert resp["status"] == ("booking_unconfirmed" if fail_create else "booked")
    text = everything(caplog)
    assert '"operation":"book_appointment"' in text  # the request's line was captured
    for secret in (NAME, "Jordan", NOTES, slot["slot_id"], slot["slot_id"][3:], CALENDAR_ID, "Meeting:"):
        assert secret not in text
    for digits in (PHONE, PHONE[1:], "6135550123", "5550123", "***0123", "0123"):
        assert digits not in text
    if not fail_create:
        [event] = list(w.provider._store(w.bindings["test-alpha"].calendar_ref).values())
        assert event.title not in text and event.description not in text
        # The content did reach the event itself.
        assert NOTES in event.description and PHONE in event.description
