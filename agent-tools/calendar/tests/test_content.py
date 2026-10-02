"""Event content (spec 5.3 `notes`, 7.3; plan UC04b `test_content.py`).

Templates come from the binding (placeholders validated at load, UC02a);
values are inserted as plain text, control characters and newlines are
stripped from inserted values, the title is capped at 200 characters and the
notes block is empty unless the binding accepts notes and notes are present.
"""

from __future__ import annotations

import dataclasses

import pytest

from calendar_tools.core import content
from calendar_tools.core.bindings import BindingConfigError
from calendar_tools.core.contact import Contact
from calendar_tools.core.ports import BookingMeta, NewEvent
from tests.test_check_availability import make_binding

ALL = (
    "{appointment_type_label}|{contact_name}|{contact_phone}|{contact_email}|"
    "{duration_minutes}|{booking_ref}|{notes_block}end"
)
CONTACT = Contact(name="Jordan Example", phone="+16135550123", email="jordan@example.com")


def binding(**changes):
    return make_binding(**changes)


def render(b=None, contact=CONTACT, type_id="phone_call", duration=30, ref="B7K2Q9", notes=None):
    b = b or binding(event_title_template="{appointment_type_label}: {contact_name}", event_description_template=ALL)
    appointment_type = next(t for t in b.appointment_types if t.id == type_id)
    return content.render_event(b, contact, appointment_type, duration, ref, notes)


def test_each_placeholder_is_rendered() -> None:
    title, description = render()
    assert title == "Phone call: Jordan Example"
    assert description == "Phone call|Jordan Example|+16135550123|jordan@example.com|30|B7K2Q9|end"


def test_phone_is_unmasked_e164_in_the_description() -> None:
    _, description = render()
    assert "+16135550123" in description
    assert "***" not in description


def test_absent_contact_fields_render_empty() -> None:
    _, description = render(contact=Contact(name="Sam Sample", phone=None, email="sam@example.com"))
    assert description == "Phone call|Sam Sample||sam@example.com|30|B7K2Q9|end"


def test_unknown_placeholder_is_refused_at_load() -> None:
    with pytest.raises(BindingConfigError):
        binding(event_description_template="{contact_address}")
    with pytest.raises(BindingConfigError):
        binding(event_title_template="{contact_name.upper}")


def test_values_are_plain_text_not_templates() -> None:
    title, description = render(contact=Contact(name="{booking_ref}", phone="+16135550123", email=None))
    assert title == "Phone call: {booking_ref}"
    assert "|{booking_ref}|" in description


@pytest.mark.parametrize("raw", ["Jordan\nExample", "Jordan\r\nExample", "Jordan\x00\x1bExample", "Jordan‮Example"])
def test_control_characters_and_newlines_stripped_from_name(raw) -> None:
    title, description = render(contact=Contact(name=raw, phone="+16135550123", email=None))
    assert "\n" not in title and "\r" not in title
    assert "\x00" not in description and "\x1b" not in description and "‮" not in description
    assert "Jordan" in title and "Example" in title


def test_template_newlines_are_kept_in_the_description_but_not_the_title() -> None:
    b = binding(event_title_template="A\n{contact_name}", event_description_template="Name: {contact_name}\nRef: {booking_ref}")
    title, description = render(b)
    assert title == "A Jordan Example"
    assert description == "Name: Jordan Example\nRef: B7K2Q9"


def test_title_capped_at_200() -> None:
    b = binding(event_title_template="x" * 190 + " {contact_name}")
    title, _ = render(b)
    assert len(title) == 200


def test_notes_block_empty_when_notes_not_accepted() -> None:
    _, description = render(notes="Some notes")
    assert description.endswith("|B7K2Q9|end")
    assert "Some notes" not in description


@pytest.mark.parametrize("notes", [None, "", "   ", "\n\x00"])
def test_notes_block_empty_when_notes_absent(notes) -> None:
    b = binding(accept_notes=True, event_description_template=ALL)
    _, description = render(b, notes=notes)
    assert description.endswith("|B7K2Q9|end")


def test_notes_block_present_when_accepted_and_given() -> None:
    b = binding(accept_notes=True, event_description_template=ALL)
    _, description = render(b, notes="Alpha: one\nBeta: two")
    assert description.endswith("|B7K2Q9|Notes: Alpha: one Beta: two\nend")


def test_notes_control_characters_stripped() -> None:
    b = binding(accept_notes=True, event_description_template="{notes_block}")
    _, description = render(b, notes="a\x00b\x1bc‮d\te")
    assert description == "Notes: abcd e\n"


def test_notes_capped_at_500() -> None:
    b = binding(accept_notes=True, event_description_template="{notes_block}")
    _, description = render(b, notes="n" * 600)
    assert description == "Notes: " + "n" * 500 + "\n"


def test_non_string_notes_are_ignored() -> None:
    b = binding(accept_notes=True, event_description_template="{notes_block}")
    _, description = render(b, notes=["SENTINEL"])  # type: ignore[arg-type]
    assert description == ""


def test_no_attendee_field_in_new_event() -> None:
    names = {f.name for f in dataclasses.fields(NewEvent)} | {f.name for f in dataclasses.fields(BookingMeta)}
    assert not {n for n in names if "attendee" in n or "guest" in n or "invite" in n}
