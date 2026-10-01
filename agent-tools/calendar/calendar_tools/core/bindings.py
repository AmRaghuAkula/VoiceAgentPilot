"""Bindings (spec section 4.1): the only place anything agent-, business- or
person-specific lives. Loaded once at startup; any error fails closed.

A `BindingConfigError` names the binding ID (once the ID itself is valid) and the
field, never the value, so nothing from the document can leak into a log line.
The loader itself logs nothing.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers

from calendar_tools.core.deadline import claim_cells_fit_budget
from calendar_tools.core.ports import CalendarRef

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

TEMPLATE_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "contact_name",
        "contact_phone",
        "contact_email",
        "appointment_type_label",
        "duration_minutes",
        "booking_ref",
        "notes_block",
    }
)

CONTACT_FIELDS: frozenset[str] = frozenset({"name", "phone", "email"})
SLOT_SELECTIONS: frozenset[str] = frozenset({"earliest", "spread"})

MAX_DURATION_PLUS_BUFFER = 240
CELL_MINUTES = 5

_BINDING_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
_PHONE_LIKE_RUN = re.compile(r"\d{7,}")
_TYPE_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_LOCALE = re.compile(r"en(-[A-Za-z0-9]{1,8})*")
_SECRET_NAME = re.compile(r"[A-Za-z0-9-]{1,127}")
_REGION = re.compile(r"[A-Z]{2}")
_MAX_LABEL = 80
_MAX_TEXT = 1024


class BindingConfigError(Exception):
    """A binding failed validation. Holds only the binding ID and the field."""

    def __init__(self, binding_id: str | None, field: str) -> None:
        self.binding_id = binding_id
        self.field = field
        super().__init__(self._text())

    def _text(self) -> str:
        return f"binding config error: binding={self.binding_id or '-'} field={self.field}"

    def __str__(self) -> str:
        return self._text()


@dataclass(frozen=True)
class AppointmentType:
    id: str
    label: str


@dataclass(frozen=True)
class Binding:
    binding_id: str
    enabled: bool
    provider: str
    calendar_id: str
    credential_secret_name: str
    timezone: ZoneInfo
    locale: str
    bookable_hours: Mapping[str, tuple[tuple[time, time], ...]]
    default_duration_minutes: int
    allowed_durations_minutes: tuple[int, ...]
    slot_step_minutes: int
    buffer_minutes: int
    min_notice_minutes: int
    max_days_ahead: int
    default_search_days: int
    max_slots_returned: int
    slot_selection: str
    slot_token_ttl_minutes: int
    appointment_types: tuple[AppointmentType, ...]
    required_contact_fields: frozenset[str]
    default_phone_region: str
    accept_notes: bool
    max_active_bookings_per_contact: int
    host_display_name: str | None
    event_title_template: str
    event_description_template: str
    allowed_principals: frozenset[str]

    @property
    def calendar_ref(self) -> CalendarRef:
        return CalendarRef(
            provider=self.provider,
            calendar_id=self.calendar_id,
            credential_secret_name=self.credential_secret_name,
        )


class _Reader:
    """Typed field access for one binding object; every failure is a
    `BindingConfigError(binding_id, field)`."""

    _MISSING = object()

    def __init__(self, binding_id: str, raw: Mapping[str, Any]) -> None:
        self.binding_id = binding_id
        self.raw = raw

    def fail(self, field: str) -> BindingConfigError:
        return BindingConfigError(self.binding_id, field)

    def get(self, field: str, *, optional: bool = False) -> Any:
        value = self.raw.get(field, self._MISSING)
        if value is self._MISSING:
            if optional:
                return None
            raise self.fail(field)
        return value

    def boolean(self, field: str) -> bool:
        value = self.get(field)
        if not isinstance(value, bool):
            raise self.fail(field)
        return value

    def integer(self, field: str, low: int | None = None, high: int | None = None) -> int:
        value = self.get(field)
        if not _is_int(value):
            raise self.fail(field)
        if low is not None and value < low:
            raise self.fail(field)
        if high is not None and value > high:
            raise self.fail(field)
        return value

    def text(self, field: str, *, optional: bool = False, max_len: int = _MAX_TEXT) -> str | None:
        value = self.get(field, optional=optional)
        if value is None and optional:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > max_len:
            raise self.fail(field)
        return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_hhmm(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    match = _HHMM.fullmatch(value)
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _bookable_hours(r: _Reader) -> Mapping[str, tuple[tuple[time, time], ...]]:
    field = "bookable_hours"
    raw = r.get(field)
    if not isinstance(raw, Mapping):
        raise r.fail(field)
    parsed: dict[str, tuple[tuple[time, time], ...]] = {}
    for day, windows in raw.items():
        if day not in WEEKDAYS or not isinstance(windows, list):
            raise r.fail(field)
        day_windows: list[tuple[time, time]] = []
        for window in windows:
            if not isinstance(window, list) or len(window) != 2:
                raise r.fail(field)
            start, end = _parse_hhmm(window[0]), _parse_hhmm(window[1])
            if start is None or end is None:
                raise r.fail(field)
            if _minutes(start) % CELL_MINUTES or _minutes(end) % CELL_MINUTES:
                raise r.fail(field)
            if end <= start:
                raise r.fail(field)
            day_windows.append((start, end))
        day_windows.sort()
        for (_, prev_end), (next_start, _) in zip(day_windows, day_windows[1:], strict=False):
            if next_start < prev_end:  # touching windows are fine; overlapping are not
                raise r.fail(field)
        parsed[day] = tuple(day_windows)
    return MappingProxyType(parsed)


def _durations(r: _Reader) -> tuple[int, ...]:
    field = "allowed_durations_minutes"
    raw = r.get(field)
    if not isinstance(raw, list) or not raw:
        raise r.fail(field)
    for value in raw:
        if not _is_int(value) or value <= 0 or value % CELL_MINUTES:
            raise r.fail(field)
    return tuple(raw)


def _multiple_of_five(r: _Reader, field: str, *, positive: bool) -> int:
    value = r.integer(field, low=1 if positive else 0)
    if value % CELL_MINUTES:
        raise r.fail(field)
    return value


def _timezone(r: _Reader) -> ZoneInfo:
    field = "timezone"
    value = r.text(field)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        raise r.fail(field) from None


def _appointment_types(r: _Reader) -> tuple[AppointmentType, ...]:
    field = "appointment_types"
    raw = r.get(field)
    if not isinstance(raw, list) or not raw:
        raise r.fail(field)
    seen: set[str] = set()
    types: list[AppointmentType] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise r.fail(field)
        type_id, label = item.get("id"), item.get("label")
        if not isinstance(type_id, str) or not _TYPE_ID.fullmatch(type_id) or type_id in seen:
            raise r.fail(field)
        if not isinstance(label, str) or not label.strip() or len(label) > _MAX_LABEL:
            raise r.fail(field)
        seen.add(type_id)
        types.append(AppointmentType(id=type_id, label=label))
    return tuple(types)


def _template(r: _Reader, field: str) -> str:
    value = r.text(field, max_len=4000)
    try:
        parts = list(string.Formatter().parse(value))
    except ValueError:
        raise r.fail(field) from None
    for _literal, name, spec, conversion in parts:
        if name is None:
            continue
        # Only the bare documented names: no attributes, indexes, conversions or specs.
        if name not in TEMPLATE_PLACEHOLDERS or spec or conversion:
            raise r.fail(field)
    return value


def _contact_fields(r: _Reader) -> frozenset[str]:
    field = "required_contact_fields"
    raw = r.get(field)
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise r.fail(field)
    fields = frozenset(raw)
    if len(fields) != len(raw) or not fields <= CONTACT_FIELDS:
        raise r.fail(field)
    if "name" not in fields or not fields & {"phone", "email"}:
        raise r.fail(field)
    return fields


def _principals(r: _Reader) -> frozenset[str]:
    field = "allowed_principals"
    raw = r.get(field)
    if not isinstance(raw, list) or not raw:
        raise r.fail(field)
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise r.fail(field)
    return frozenset(raw)


def _binding(binding_id: str, raw: Any, provider_names: frozenset[str]) -> Binding:
    if not isinstance(raw, Mapping):
        raise BindingConfigError(binding_id, "$")
    r = _Reader(binding_id, raw)

    enabled = r.boolean("enabled")
    provider = r.get("provider")
    if not isinstance(provider, str) or provider not in provider_names:
        raise r.fail("provider")
    calendar_id = r.text("calendar_id")
    secret_name = r.text("credential_secret_name")
    if not _SECRET_NAME.fullmatch(secret_name):
        raise r.fail("credential_secret_name")
    timezone = _timezone(r)
    locale = r.text("locale", max_len=35)
    if not _LOCALE.fullmatch(locale):
        raise r.fail("locale")
    bookable_hours = _bookable_hours(r)

    allowed = _durations(r)
    default = r.integer("default_duration_minutes")
    if default not in allowed:
        raise r.fail("default_duration_minutes")
    step = _multiple_of_five(r, "slot_step_minutes", positive=True)
    buffer = _multiple_of_five(r, "buffer_minutes", positive=False)
    largest = max(allowed) + buffer
    if largest > MAX_DURATION_PLUS_BUFFER:
        raise r.fail("allowed_durations_minutes")
    if not claim_cells_fit_budget(largest // CELL_MINUTES):  # plan P17
        raise r.fail("allowed_durations_minutes")

    min_notice = r.integer("min_notice_minutes", low=0)
    max_days = r.integer("max_days_ahead", low=1, high=60)
    search_days = r.integer("default_search_days", low=1)
    max_slots = r.integer("max_slots_returned", low=1, high=10)
    selection = r.get("slot_selection")
    if not isinstance(selection, str) or selection not in SLOT_SELECTIONS:
        raise r.fail("slot_selection")
    ttl = r.integer("slot_token_ttl_minutes", low=1)
    appointment_types = _appointment_types(r)
    contact_fields = _contact_fields(r)
    region = r.get("default_phone_region")
    if (
        not isinstance(region, str)
        or not _REGION.fullmatch(region)
        or region not in phonenumbers.SUPPORTED_REGIONS
    ):
        raise r.fail("default_phone_region")
    accept_notes = r.boolean("accept_notes")
    limit = r.integer("max_active_bookings_per_contact", low=1)
    host = r.text("host_display_name", optional=True, max_len=_MAX_LABEL)
    title = _template(r, "event_title_template")
    description = _template(r, "event_description_template")
    principals = _principals(r)

    return Binding(
        binding_id=binding_id,
        enabled=enabled,
        provider=provider,
        calendar_id=calendar_id,
        credential_secret_name=secret_name,
        timezone=timezone,
        locale=locale,
        bookable_hours=bookable_hours,
        default_duration_minutes=default,
        allowed_durations_minutes=allowed,
        slot_step_minutes=step,
        buffer_minutes=buffer,
        min_notice_minutes=min_notice,
        max_days_ahead=max_days,
        default_search_days=search_days,
        max_slots_returned=max_slots,
        slot_selection=selection,
        slot_token_ttl_minutes=ttl,
        appointment_types=appointment_types,
        required_contact_fields=contact_fields,
        default_phone_region=region,
        accept_notes=accept_notes,
        max_active_bookings_per_contact=limit,
        host_display_name=host,
        event_title_template=title,
        event_description_template=description,
        allowed_principals=principals,
    )


def _valid_binding_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _BINDING_ID.fullmatch(value) is not None
        and _PHONE_LIKE_RUN.search(value) is None
    )


def load_bindings(raw_json: str, provider_names: frozenset[str]) -> Mapping[str, Binding]:
    """Parse and validate the `CALENDAR_BINDINGS_JSON` document.

    Returns a read-only mapping of every binding, enabled or not (a disabled
    binding is served as unknown by the HTTP layer).
    """
    try:
        doc = json.loads(raw_json)
    except (ValueError, TypeError):
        # `from None`: the decoder's message quotes the document.
        raise BindingConfigError(None, "$") from None
    if not isinstance(doc, dict):
        raise BindingConfigError(None, "$")
    raw_bindings = doc.get("bindings")
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise BindingConfigError(None, "bindings")

    bindings: dict[str, Binding] = {}
    for binding_id, raw in raw_bindings.items():
        if not _valid_binding_id(binding_id):
            raise BindingConfigError(None, "binding_id")
        bindings[binding_id] = _binding(binding_id, raw, provider_names)
    return MappingProxyType(bindings)
