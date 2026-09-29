import re
from collections.abc import Mapping
from dataclasses import dataclass

_NON_DIGIT = re.compile(r"[^0-9]", re.ASCII)
_NUMBER_SHAPED = re.compile(r"\A\+?[0-9 ().-]+\Z", re.ASCII)
_E164_NANP = re.compile(r"\A\+1[2-9]\d{2}[2-9]\d{6}\Z", re.ASCII)
_E164_OTHER = re.compile(r"\A\+[2-9]\d{7,14}\Z", re.ASCII)
_PINNED_VERSION = re.compile(r"\A[1-9][0-9]*\Z", re.ASCII)

# D-049 (founder-directed override of D-004's "never latest", 2026-09-29): the one and only
# spelling that selects unpinned "latest" resolution. Matched by exact string equality, never
# case-folded or stripped, so a near-miss typed into an env var ("Latest", "latest ") fails
# loudly instead of silently changing which agent version answers calls.
LATEST_AGENT_VERSION = "latest"


@dataclass(frozen=True)
class AgentRoute:
    project: str
    agent: str
    version: str

    def __post_init__(self):
        # D-004 defense in depth: a malformed version here would reach the Voice Live SDK, which
        # treats a missing/empty agent_version as "latest" with no error. So every AgentRoute
        # (however constructed) must carry either a pinned digit string -- still validated
        # exactly as strictly as before -- or the explicit LATEST_AGENT_VERSION sentinel.
        #
        # Why the sentinel exists at all (D-049): the founder chose, with the risk explained, to
        # have calls use the agent's most recently *saved* Foundry version (including unpublished
        # drafts) so prompt edits take effect on the next call without a repo change. That is a
        # deliberate, named mode -- not a fallback -- so it is opt-in only via this exact string;
        # None, "", or any other value still raises. The handler omits agent_version entirely in
        # this mode rather than sending the literal "latest" to the service.
        if not isinstance(self.version, str) or not (
            self.version == LATEST_AGENT_VERSION or _PINNED_VERSION.match(self.version)
        ):
            raise ValueError(
                "AgentRoute.version must be a pinned digit string or exactly "
                f"{LATEST_AGENT_VERSION!r}, got {self.version!r}"
            )

    @property
    def is_unpinned(self) -> bool:
        """True when this route resolves the agent's latest saved version (D-049)."""
        return self.version == LATEST_AGENT_VERSION


def normalize_number(raw: object) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    if not _NUMBER_SHAPED.match(text):
        return text
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


def _number_from_identifier(identifier: object) -> str | None:
    if not isinstance(identifier, Mapping):
        return None
    phone_number = identifier.get("phoneNumber")
    phone = phone_number.get("value") if isinstance(phone_number, Mapping) else None
    phone = phone if isinstance(phone, str) else None
    raw_id = identifier.get("rawId")
    return phone or (raw_id if isinstance(raw_id, str) else None)


def called_number_from_event(data: object) -> str | None:
    if not isinstance(data, Mapping):
        return None
    return _number_from_identifier(data.get("to"))


def caller_number_from_event(data: object) -> str | None:
    if not isinstance(data, Mapping):
        return None
    return _number_from_identifier(data.get("from"))


def resolve_route(routes: Mapping[str, AgentRoute], called_number: object) -> AgentRoute | None:
    return routes.get(normalize_number(called_number))
