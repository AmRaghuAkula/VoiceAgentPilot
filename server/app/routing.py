import re
from collections.abc import Mapping
from dataclasses import dataclass

_NON_DIGIT = re.compile(r"\D")
_E164_NANP = re.compile(r"^\+1\d{10}$")
_E164_OTHER = re.compile(r"^\+[2-9]\d{7,14}$")


@dataclass(frozen=True)
class AgentRoute:
    project: str
    agent: str
    version: str


def normalize_number(raw: object) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    has_plus = text.startswith("+")
    digits = _NON_DIGIT.sub("", text)
    if has_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits


def is_valid_e164(number: str) -> bool:
    return bool(_E164_NANP.match(number) or _E164_OTHER.match(number))


def _number_from_identifier(identifier: Mapping | None) -> str | None:
    identifier = identifier or {}
    phone = (identifier.get("phoneNumber") or {}).get("value")
    return phone or identifier.get("rawId")


def called_number_from_event(data: Mapping) -> str | None:
    return _number_from_identifier(data.get("to"))


def caller_number_from_event(data: Mapping) -> str | None:
    return _number_from_identifier(data.get("from"))


def resolve_route(routes: Mapping[str, AgentRoute], called_number: object) -> AgentRoute | None:
    return routes.get(normalize_number(called_number))
