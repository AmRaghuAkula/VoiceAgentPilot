# Telephony Bridge (Step 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import Microsoft's Call Center Voice Agent Accelerator and change it so an inbound ACS phone call reaches a pinned Azure AI Foundry agent through Voice Live agent mode. The changes cover routing, a secured media WebSocket, a call cap, spoken fallbacks, number masking and bounded cleanup.

**Architecture:** The upstream accelerator (Python, Quart, `azure-ai-voicelive`, ACS Call Automation) is merged in with its git history. New logic lives in new files: `bridge_config.py`, `routing.py`, `log_mask.py`, and in `providers/acs/`: `signing.py`, `call_session.py`, `bridge_calls.py` and `callback_auth.py`. Upstream files only get small hooks. Every way a call can end goes through one idempotent `CallSession.request_end()`.

**Tech Stack:** Python 3.12, uv, Quart 0.20, `azure-ai-voicelive` 1.3.0 (agent mode), `azure-communication-callautomation` 1.6.0, `azure-identity` (DefaultAzureCredential), PyJWT, pytest and pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-09-25-telephony-bridge-design.md](../specs/2026-09-25-telephony-bridge-design.md). Read the spec first. Its §3.3 pseudocode is the source of truth for `CallSession`.

## Global Constraints

- There must be **no real-estate words, agent names, project names or phone numbers in code**. Test fixtures use neutral values such as `"proj"`, `"agent-a"` and `+14165551234`.
- The agent version must be pinned. A routing `version` must match `^\d+$` (as a string); `"latest"` is never allowed.
- The bridge sends **no `instructions`, `voice`, `turn_detection`, noise or echo settings** to Voice Live in agent mode. The only optional extra field is `interim_response`, taken from `INTERIM_RESPONSE_JSON`.
- Agent mode authenticates with Entra ID only, via `DefaultAzureCredential(managed_identity_client_id=AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID or None)`. No API key is used on the agent path.
- Every logged phone number goes through `mask_number()`, which keeps the last 4 digits only. No phone number or secret ever appears in a URL.
- The media URL is `/acs/ws/{call_key}/{HMAC(MEDIA_WS_TOKEN, "ws:"+call_key)}` and the callback URL is `/acs/callbacks/{call_key}/{HMAC(MEDIA_WS_TOKEN, "cb:"+call_key)}`. Both are hex and SHA-256.
- Spec variable names win over accelerator names: `MAX_CALL_SECONDS` over `MAX_CALL_DURATION`, and `VOICE_LIVE_ENDPOINT` over `AZURE_VOICE_LIVE_ENDPOINT`.
- Defaults: `MAX_CALL_SECONDS=600`, `MEDIA_CONNECT_TIMEOUT_SECONDS=10`, `VOICE_LIVE_CONNECT_TIMEOUT_SECONDS=8`, `MEDIA_LOST_GRACE_SECONDS=5`, `ENABLE_WEB_CLIENT=false`, Voice Live close timeout 5 s, play safety timeout 15 s, answered/connected wait 5 s.
- `FALLBACK_MESSAGE` defaults to "Sorry, we're having trouble right now. Please call back in a few minutes."
- `GOODBYE_MESSAGE` defaults to "We've reached the time limit for this call. Thank you for calling, goodbye."
- Do not touch `infra/`, `hooks/`, `azure.yaml`, or the non-ACS provider folders.
- Do not run `azd`, and do not create Azure resources.
- Never commit `.env` files or secrets.
- All commands run from `server/` unless stated otherwise. Use `python -m uv` (uv is installed into user site-packages and may not be on PATH).
- Every commit message ends with the `Co-Authored-By:` trailer for the model doing the work.

## Review Focus

1. **A called number that isn't a phone number** (e.g. `to` is a Teams or ACS user with only a `rawId` such as `8:acs:...`). The caller should hear the fallback message (a route miss), with no crash. The test is in Task 8.
2. **An IncomingCall event with no `incomingCallContext`**. The response should be a 400, and no session should be left in the registry. The test is in Task 8.
3. **A callback body that is empty or `null` JSON**. The response should be a 200 with no exception and no state change. The test is in Task 8.
4. **The media WebSocket closing before Voice Live ever connected** (the handler exists but `conn` is None). Cleanup and force close should do nothing and not raise. The test is in Task 6.
5. **A non-hex or non-ASCII signature path segment**. `verify()` should return False and not raise. The test is in Task 7.

---

## File Structure

| File | Status | Responsibility |
| --- | --- | --- |
| `server/pyproject.toml`, `server/uv.lock` | modify | Pin SDK minimums, add the dev test group and pytest config |
| `server/tests/conftest.py` | create | Shared fixtures: server loader, log capture, fake ACS client |
| `server/app/log_mask.py` | create | `mask_number()` |
| `server/app/routing.py` | create | `AgentRoute`, `normalize_number`, `is_valid_e164`, event number extraction, `resolve_route` |
| `server/app/bridge_config.py` | create | `BridgeConfig`, `load_bridge_config`, `parse_routing`, validation |
| `server/server.py` | modify | Load `BridgeConfig`, apply resolved endpoint and cap, guard the web client |
| `server/app/call_manager.py` | modify | `is_expired()` returns `"duration"` / `"idle"` / `None` |
| `server/app/call_loop.py` | modify | Call `on_call_cap()` / `on_idle()` on expiry |
| `server/app/handler/voicelive_media_handler.py` | modify | Agent mode, audio-only session config, id logging, hooks, `stop_forwarding_agent_audio`, `force_close` |
| `server/app/providers/acs/signing.py` | create | HMAC sign and verify |
| `server/app/providers/acs/call_session.py` | create | `SessionSettings`, `CallSession`, `CallSessionRegistry`, `close_voicelive` |
| `server/app/providers/acs/bridge_calls.py` | create | `BridgeCallController`: IncomingCall answer and callback dispatch |
| `server/app/providers/acs/media_handler.py` | modify | `ACSMediaHandler` bound to a `CallSession` |
| `server/app/providers/acs/callback_auth.py` | create | Optional ACS callback JWT verification |
| `server/app/providers/acs/__init__.py` | modify | New routes: signed callbacks, signed media WebSocket, sweeper |
| `README.md`, `server/.env.sample` | modify | Pilot documentation and config |

`server/app/providers/acs/event_handler.py` (upstream) is **left unchanged and unused**, so upstream merges stay clean.

---

### Task 0: Toolchain, upstream import, test harness

**Files:**
- Modify: `README.md` (merge resolution), `server/pyproject.toml`, `server/uv.lock`
- Create: `docs/ACCELERATOR_README.md`, `server/tests/__init__.py`, `server/tests/conftest.py`, `server/tests/test_smoke.py`

**Interfaces:**
- Produces: the `load_server`, `logs` and `fake_acs` fixtures in `server/tests/conftest.py`, used by every later task.

- [ ] **Step 1: Install uv and rename the branch** (repo root)

```bash
python -m pip install --user uv
python -m uv --version
git status --short
git branch -m design/telephony-bridge feat/telephony-bridge-step3
```
Expected: the uv version prints and `git status` is clean.

- [ ] **Step 2: Merge the upstream accelerator**

```bash
git remote add upstream https://github.com/Azure-Samples/call-center-voice-agent-accelerator.git
git fetch upstream
git merge upstream/main --allow-unrelated-histories --no-edit
```
Expected: a CONFLICT in `README.md` only (add/add).

- [ ] **Step 3: Resolve the README conflict**

```bash
mkdir -p docs
git show upstream/main:README.md | sed 's#(docs/images/#(images/#g; s#"docs/images/#"images/#g' > docs/ACCELERATOR_README.md
git checkout --ours README.md
git add README.md docs/ACCELERATOR_README.md
git commit --no-edit
git log --oneline -1 upstream/main
```
Expected: the merge commit exists, and `upstream/main` is `a4f40bc` (write down whatever SHA prints; Task 11 records it).

- [ ] **Step 4: Update `server/pyproject.toml`**

In `dependencies`, replace `"azure-ai-voicelive[aiohttp]>=1.1.0",` with:
```toml
    "azure-ai-voicelive[aiohttp]>=1.3.0,<2",
```
In `[project.optional-dependencies]`, replace the `acs` list with:
```toml
acs = [
    "azure-communication-callautomation>=1.6.0,<2",
    "azure-eventgrid>=4.22.0",
]
```
Append at the end of the file:
```toml
[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 5: Lock and install**

```bash
cd server
python -m uv lock
python -m uv sync --extra acs --group dev
```
Expected: uv downloads a managed Python 3.12 if needed and resolves `azure-ai-voicelive` at 1.3.x or later.

- [ ] **Step 6: Write `server/tests/__init__.py`** (empty file) and `server/tests/conftest.py`

```python
import importlib
import logging
import sys
from types import SimpleNamespace

import pytest

BRIDGE_ENV_KEYS = [
    "ACS_CONNECTION_STRING", "ACS_DEV_TUNNEL", "ENABLE_WEB_CLIENT", "MAX_CALL_SECONDS", "MAX_CALL_DURATION",
    "VOICE_LIVE_ENDPOINT", "AZURE_VOICE_LIVE_ENDPOINT", "AGENT_ROUTING_JSON", "MEDIA_WS_TOKEN",
    "ACS_COGNITIVE_SERVICES_ENDPOINT", "ACS_CALLBACK_JWT_AUDIENCE", "INTERIM_RESPONSE_JSON",
    "VOICE_LIVE_API_VERSION", "TWILIO_AUTH_TOKEN", "INFOBIP_API_KEY", "GENESYS_API_KEY",
    "SINCH_APPLICATION_KEY", "BANDWIDTH_ACCOUNT_ID", "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID",
]

VALID_ROUTING = '{"+14165551234": {"project": "proj", "agent": "agent-a", "version": "10"}}'
TOKEN = "t" * 40


def acs_env(**overrides):
    env = {
        "ACS_CONNECTION_STRING": "endpoint=https://fake.communication.azure.com/;accesskey=ZmFrZQ==",
        "AGENT_ROUTING_JSON": VALID_ROUTING,
        "MEDIA_WS_TOKEN": TOKEN,
        "ACS_COGNITIVE_SERVICES_ENDPOINT": "https://cog.example",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def load_server(monkeypatch):
    def _load(**env):
        import dotenv

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
        for key in BRIDGE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AZURE_VOICE_LIVE_ENDPOINT", "https://vl.example")
        monkeypatch.setenv("AZURE_VOICE_LIVE_API_KEY", "key")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        sys.modules.pop("server", None)
        return importlib.import_module("server")

    return _load


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


@pytest.fixture
def logs():
    handler = _ListHandler()
    app_logger = logging.getLogger("app")
    old_level = app_logger.level
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.DEBUG)
    yield handler
    app_logger.removeHandler(handler)
    app_logger.setLevel(old_level)


class FakeConnection:
    def __init__(self, acs):
        self._acs = acs

    async def play_media(self, play_source, play_to="all", operation_context=None, **kwargs):
        if self._acs.play_error is not None:
            raise self._acs.play_error
        self._acs.log.append(("play", play_source.text))

    async def hang_up(self, is_for_everyone, **kwargs):
        self._acs.log.append(("hang_up", is_for_everyone))


class FakeAcs:
    def __init__(self):
        self.log = []
        self.play_error = None
        self.answer_kwargs = None
        self.answer_error = None
        self.on_answer = None
        self.connection_ids = []

    def get_call_connection(self, call_connection_id):
        self.connection_ids.append(call_connection_id)
        return FakeConnection(self)

    async def answer_call(self, **kwargs):
        self.answer_kwargs = kwargs
        if self.on_answer is not None:
            self.on_answer()
        if self.answer_error is not None:
            raise self.answer_error
        return SimpleNamespace(call_connection_id="conn-1")


@pytest.fixture
def fake_acs():
    return FakeAcs()
```

- [ ] **Step 7: Write the smoke test** `server/tests/test_smoke.py`

```python
def test_upstream_modules_import():
    import app.call_loop  # noqa: F401
    import app.call_manager  # noqa: F401
    import app.handler.voicelive_media_handler  # noqa: F401
    import app.providers.acs.media_handler  # noqa: F401
```

- [ ] **Step 8: Run it**

Run: `python -m uv run pytest -q`
Expected: `1 passed`

- [ ] **Step 9: Commit**

```bash
cd ..
git add server/pyproject.toml server/uv.lock server/tests
git commit -m "chore: pin Voice Live/ACS SDKs with agent-mode support and add pytest harness"
```

---

### Task 1: Number masking

**Files:**
- Create: `server/app/log_mask.py`
- Test: `server/tests/test_log_mask.py`

**Interfaces:**
- Produces: `mask_number(value: object) -> str`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.log_mask import mask_number


@pytest.mark.parametrize(
    "value, expected",
    [
        ("+14165551234", "***1234"),
        ("(416) 555-1234", "***1234"),
        ("4:+14165551234", "***1234"),
        ("123", "***"),
        ("", "***"),
        (None, "***"),
    ],
)
def test_mask_number(value, expected):
    assert mask_number(value) == expected


def test_masked_value_never_has_more_than_four_digits():
    assert sum(c.isdigit() for c in mask_number("+14165551234")) == 4
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_log_mask.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.log_mask'`

- [ ] **Step 3: Implement** `server/app/log_mask.py`

```python
import re

_NON_DIGIT = re.compile(r"\D")


def mask_number(value: object) -> str:
    if value is None:
        return "***"
    digits = _NON_DIGIT.sub("", str(value))
    if len(digits) < 4:
        return "***"
    return "***" + digits[-4:]
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `python -m uv run pytest tests/test_log_mask.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add server/app/log_mask.py server/tests/test_log_mask.py
git commit -m "feat: add last-4-digit phone number masking for logs"
```

---

### Task 2: Routing primitives

**Files:**
- Create: `server/app/routing.py`
- Test: `server/tests/test_routing.py`

**Interfaces:**
- Produces:
  - `AgentRoute(project: str, agent: str, version: str)`, a frozen dataclass
  - `normalize_number(raw: object) -> str`
  - `is_valid_e164(number: str) -> bool`
  - `called_number_from_event(data: Mapping) -> str | None`
  - `caller_number_from_event(data: Mapping) -> str | None`
  - `resolve_route(routes: Mapping[str, AgentRoute], called_number: object) -> AgentRoute | None`

- [ ] **Step 1: Write the failing test**

```python
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


@pytest.mark.parametrize("raw, expected", [(None, ""), ("", ""), ("abc", ""), ("416555123", "416555123")])
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_routing.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routing'`

- [ ] **Step 3: Implement** `server/app/routing.py`

```python
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
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `python -m uv run pytest tests/test_routing.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add server/app/routing.py server/tests/test_routing.py
git commit -m "feat: add called-number normalization and agent route lookup"
```

---

### Task 3: Bridge configuration and validation

**Files:**
- Create: `server/app/bridge_config.py`
- Test: `server/tests/test_bridge_config.py`

**Interfaces:**
- Consumes: `AgentRoute`, `normalize_number`, `is_valid_e164` (Task 2), `mask_number` (Task 1)
- Produces:
  - `BridgeConfigError(ValueError)`
  - `BridgeConfig` (a frozen dataclass whose fields are listed below)
  - `parse_routing(raw: str) -> dict[str, AgentRoute]`
  - `load_bridge_config(env: Mapping[str, str], *, acs_active: bool) -> BridgeConfig`
  - `DEFAULT_FALLBACK_MESSAGE`, `DEFAULT_GOODBYE_MESSAGE`

`BridgeConfig` fields:

| Field | Type |
| --- | --- |
| `routes` | `Mapping[str, AgentRoute]` |
| `media_ws_token` | `str` |
| `acs_cognitive_services_endpoint` | `str` |
| `fallback_message` | `str` |
| `goodbye_message` | `str` |
| `tts_voice` | `str` |
| `max_call_seconds` | `int` |
| `voice_live_endpoint` | `str \| None` |
| `media_connect_timeout` | `float` |
| `voice_live_connect_timeout` | `float` |
| `media_lost_grace` | `float` |
| `enable_web_client` | `bool` |
| `interim_response` | `Mapping \| None` |
| `voice_live_api_version` | `str \| None` |
| `callback_jwt_audience` | `str \| None` |

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.bridge_config import (
    DEFAULT_FALLBACK_MESSAGE,
    BridgeConfigError,
    load_bridge_config,
    parse_routing,
)
from app.routing import AgentRoute
from tests.conftest import TOKEN, VALID_ROUTING, acs_env


def test_valid_acs_config():
    cfg = load_bridge_config(acs_env(), acs_active=True)
    assert cfg.routes == {"+14165551234": AgentRoute("proj", "agent-a", "10")}
    assert cfg.media_ws_token == TOKEN
    assert cfg.fallback_message == DEFAULT_FALLBACK_MESSAGE
    assert cfg.max_call_seconds == 600
    assert cfg.enable_web_client is False


def test_routing_keys_are_normalized():
    routes = parse_routing('{"(416) 555-1234": {"project": "p", "agent": "a", "version": "10"}}')
    assert list(routes) == ["+14165551234"]


def test_raw_duplicate_keys_rejected():
    raw = (
        '{"+14165551234": {"project": "p", "agent": "a", "version": "10"},'
        ' "+14165551234": {"project": "p", "agent": "a", "version": "11"}}'
    )
    with pytest.raises(BridgeConfigError, match="duplicate"):
        parse_routing(raw)


def test_normalized_duplicate_keys_rejected():
    raw = (
        '{"+14165551234": {"project": "p", "agent": "a", "version": "10"},'
        ' "416-555-1234": {"project": "p", "agent": "a", "version": "10"}}'
    )
    with pytest.raises(BridgeConfigError, match="two keys"):
        parse_routing(raw)


@pytest.mark.parametrize("key", ["416555123", "+1416555123", "not-a-number"])
def test_invalid_e164_key_rejected(key):
    with pytest.raises(BridgeConfigError, match="E.164"):
        parse_routing('{"%s": {"project": "p", "agent": "a", "version": "10"}}' % key)


@pytest.mark.parametrize("version", ['"latest"', '"Latest"', '" latest "', '""', "10", "null"])
def test_unpinned_versions_rejected(version):
    with pytest.raises(BridgeConfigError, match="version"):
        parse_routing('{"+14165551234": {"project": "p", "agent": "a", "version": %s}}' % version)


def test_version_is_stripped():
    routes = parse_routing('{"+14165551234": {"project": "p", "agent": "a", "version": " 10 "}}')
    assert routes["+14165551234"].version == "10"


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"+14165551234": "x"}'])
def test_malformed_routing_rejected(raw):
    with pytest.raises(BridgeConfigError):
        parse_routing(raw)


def test_missing_project_rejected():
    with pytest.raises(BridgeConfigError, match="project"):
        parse_routing('{"+14165551234": {"agent": "a", "version": "10"}}')


def test_error_messages_mask_numbers():
    with pytest.raises(BridgeConfigError) as exc:
        parse_routing('{"+1416555123": {"project": "p", "agent": "a", "version": "10"}}')
    assert "416555123" not in str(exc.value)


@pytest.mark.parametrize(
    "missing", ["AGENT_ROUTING_JSON", "MEDIA_WS_TOKEN", "ACS_COGNITIVE_SERVICES_ENDPOINT"]
)
def test_acs_required_keys(missing):
    with pytest.raises(BridgeConfigError, match=missing):
        load_bridge_config(acs_env(**{missing: None}), acs_active=True)


def test_short_token_rejected():
    with pytest.raises(BridgeConfigError, match="MEDIA_WS_TOKEN"):
        load_bridge_config(acs_env(MEDIA_WS_TOKEN="short"), acs_active=True)


def test_acs_inactive_needs_nothing():
    cfg = load_bridge_config({}, acs_active=False)
    assert cfg.routes == {}
    assert cfg.media_ws_token == ""


def test_spec_names_win_over_accelerator_names():
    cfg = load_bridge_config(
        acs_env(
            MAX_CALL_SECONDS="60",
            MAX_CALL_DURATION="3600",
            VOICE_LIVE_ENDPOINT="https://spec.example",
            AZURE_VOICE_LIVE_ENDPOINT="https://accel.example",
        ),
        acs_active=True,
    )
    assert cfg.max_call_seconds == 60
    assert cfg.voice_live_endpoint == "https://spec.example"


def test_accelerator_names_used_as_fallback():
    cfg = load_bridge_config(
        acs_env(MAX_CALL_DURATION="900", AZURE_VOICE_LIVE_ENDPOINT="https://accel.example"),
        acs_active=True,
    )
    assert cfg.max_call_seconds == 900
    assert cfg.voice_live_endpoint == "https://accel.example"


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_bad_numbers_rejected(value):
    with pytest.raises(BridgeConfigError, match="MAX_CALL_SECONDS"):
        load_bridge_config(acs_env(MAX_CALL_SECONDS=value), acs_active=True)


def test_optional_fields():
    cfg = load_bridge_config(
        acs_env(
            ENABLE_WEB_CLIENT="TRUE",
            INTERIM_RESPONSE_JSON='{"type": "llm_interim_response"}',
            VOICE_LIVE_API_VERSION="2026-07-15",
            ACS_CALLBACK_JWT_AUDIENCE="acs-resource-id",
        ),
        acs_active=True,
    )
    assert cfg.enable_web_client is True
    assert cfg.interim_response == {"type": "llm_interim_response"}
    assert cfg.voice_live_api_version == "2026-07-15"
    assert cfg.callback_jwt_audience == "acs-resource-id"


def test_interim_response_must_be_object():
    with pytest.raises(BridgeConfigError, match="INTERIM_RESPONSE_JSON"):
        load_bridge_config(acs_env(INTERIM_RESPONSE_JSON="[1]"), acs_active=True)


def test_routes_mapping_is_read_only():
    cfg = load_bridge_config(acs_env(), acs_active=True)
    with pytest.raises(TypeError):
        cfg.routes["+19999999999"] = AgentRoute("x", "y", "1")  # type: ignore[index]


def test_valid_routing_constant_is_valid():
    assert parse_routing(VALID_ROUTING)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_bridge_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.bridge_config'`

- [ ] **Step 3: Implement** `server/app/bridge_config.py`

```python
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.log_mask import mask_number
from app.routing import AgentRoute, is_valid_e164, normalize_number

DEFAULT_FALLBACK_MESSAGE = "Sorry, we're having trouble right now. Please call back in a few minutes."
DEFAULT_GOODBYE_MESSAGE = "We've reached the time limit for this call. Thank you for calling, goodbye."
DEFAULT_TTS_VOICE = "en-US-JennyNeural"
MIN_TOKEN_LENGTH = 32
_VERSION = re.compile(r"^\d+$")


class BridgeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BridgeConfig:
    routes: Mapping[str, AgentRoute]
    media_ws_token: str
    acs_cognitive_services_endpoint: str
    fallback_message: str
    goodbye_message: str
    tts_voice: str
    max_call_seconds: int
    voice_live_endpoint: str | None
    media_connect_timeout: float
    voice_live_connect_timeout: float
    media_lost_grace: float
    enable_web_client: bool
    interim_response: Mapping | None
    voice_live_api_version: str | None
    callback_jwt_audience: str | None


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeConfigError(f"AGENT_ROUTING_JSON has a duplicate key for {mask_number(key)}")
        result[key] = value
    return result


def parse_routing(raw: str) -> dict[str, AgentRoute]:
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise BridgeConfigError(f"AGENT_ROUTING_JSON is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict) or not data:
        raise BridgeConfigError("AGENT_ROUTING_JSON must be a non-empty JSON object")

    routes: dict[str, AgentRoute] = {}
    for raw_key, entry in data.items():
        label = mask_number(raw_key)
        number = normalize_number(raw_key)
        if not is_valid_e164(number):
            raise BridgeConfigError(f"AGENT_ROUTING_JSON key {label} is not a valid E.164 phone number")
        if number in routes:
            raise BridgeConfigError(f"AGENT_ROUTING_JSON has two keys for {label}")
        if not isinstance(entry, dict):
            raise BridgeConfigError(f"AGENT_ROUTING_JSON entry for {label} must be an object")
        for field in ("project", "agent"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise BridgeConfigError(f"AGENT_ROUTING_JSON entry for {label} needs a non-empty '{field}'")
        version = entry.get("version")
        if not isinstance(version, str) or not _VERSION.match(version.strip()):
            raise BridgeConfigError(
                f"AGENT_ROUTING_JSON entry for {label} must pin 'version' as a string of digits"
            )
        routes[number] = AgentRoute(entry["project"].strip(), entry["agent"].strip(), version.strip())
    return routes


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if value not in (None, "") else None


def _positive(env: Mapping[str, str], name: str, default, cast):
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise BridgeConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise BridgeConfigError(f"{name} must be positive")
    return value


def load_bridge_config(env: Mapping[str, str], *, acs_active: bool) -> BridgeConfig:
    routing_raw = _get(env, "AGENT_ROUTING_JSON")
    token = _get(env, "MEDIA_WS_TOKEN") or ""
    cognitive = _get(env, "ACS_COGNITIVE_SERVICES_ENDPOINT") or ""

    if acs_active:
        for name, value in (
            ("AGENT_ROUTING_JSON", routing_raw),
            ("MEDIA_WS_TOKEN", token),
            ("ACS_COGNITIVE_SERVICES_ENDPOINT", cognitive),
        ):
            if not value:
                raise BridgeConfigError(f"{name} is required when ACS is configured")
        if len(token) < MIN_TOKEN_LENGTH:
            raise BridgeConfigError(f"MEDIA_WS_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")

    routes = parse_routing(routing_raw) if routing_raw else {}

    max_name = "MAX_CALL_SECONDS" if _get(env, "MAX_CALL_SECONDS") else "MAX_CALL_DURATION"

    interim = None
    interim_raw = _get(env, "INTERIM_RESPONSE_JSON")
    if interim_raw:
        try:
            interim = json.loads(interim_raw)
        except json.JSONDecodeError as exc:
            raise BridgeConfigError("INTERIM_RESPONSE_JSON is not valid JSON") from exc
        if not isinstance(interim, dict):
            raise BridgeConfigError("INTERIM_RESPONSE_JSON must be a JSON object")

    return BridgeConfig(
        routes=MappingProxyType(routes),
        media_ws_token=token,
        acs_cognitive_services_endpoint=cognitive,
        fallback_message=_get(env, "FALLBACK_MESSAGE") or DEFAULT_FALLBACK_MESSAGE,
        goodbye_message=_get(env, "GOODBYE_MESSAGE") or DEFAULT_GOODBYE_MESSAGE,
        tts_voice=_get(env, "ACS_TTS_VOICE") or DEFAULT_TTS_VOICE,
        max_call_seconds=_positive(env, max_name, 600, int),
        voice_live_endpoint=_get(env, "VOICE_LIVE_ENDPOINT") or _get(env, "AZURE_VOICE_LIVE_ENDPOINT"),
        media_connect_timeout=_positive(env, "MEDIA_CONNECT_TIMEOUT_SECONDS", 10.0, float),
        voice_live_connect_timeout=_positive(env, "VOICE_LIVE_CONNECT_TIMEOUT_SECONDS", 8.0, float),
        media_lost_grace=_positive(env, "MEDIA_LOST_GRACE_SECONDS", 5.0, float),
        enable_web_client=(env.get("ENABLE_WEB_CLIENT") or "").strip().lower() == "true",
        interim_response=interim,
        voice_live_api_version=_get(env, "VOICE_LIVE_API_VERSION"),
        callback_jwt_audience=_get(env, "ACS_CALLBACK_JWT_AUDIENCE"),
    )
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `python -m uv run pytest tests/test_bridge_config.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add server/app/bridge_config.py server/tests/test_bridge_config.py
git commit -m "feat: add validated bridge config with pinned-version routing and spec name aliases"
```

---

### Task 4: Wire the config into `server.py`, and guard the web client

**Files:**
- Modify: `server/server.py`
- Test: `server/tests/test_server.py`

**Interfaces:**
- Consumes: `load_bridge_config`, `BridgeConfigError` (Task 3)
- Produces: `app.config["BRIDGE"]: BridgeConfig`, `app.config["VOICE_LIVE_API_VERSION"]`, `app.config["INTERIM_RESPONSE"]`. `/web/ws` and `/` are registered only when `bridge.enable_web_client` is true.

- [ ] **Step 1: Write the failing test** `server/tests/test_server.py`

```python
import pytest


async def test_web_client_disabled_by_default(load_server):
    server = load_server()
    client = server.app.test_client()
    assert (await client.get("/")).status_code == 404
    assert (await client.get("/health")).status_code == 200


async def test_web_client_enabled_explicitly(load_server):
    server = load_server(ENABLE_WEB_CLIENT="true")
    client = server.app.test_client()
    assert (await client.get("/")).status_code == 200


def test_spec_cap_name_reaches_call_manager(load_server):
    server = load_server(MAX_CALL_SECONDS="60", MAX_CALL_DURATION="3600")
    assert server.call_manager.get_stats()["max_call_duration_s"] == 60


def test_spec_endpoint_name_reaches_app_config(load_server):
    server = load_server(VOICE_LIVE_ENDPOINT="https://spec.example")
    assert server.app.config["AZURE_VOICE_LIVE_ENDPOINT"] == "https://spec.example"
    assert server.app.config["BRIDGE"].voice_live_endpoint == "https://spec.example"


def test_bad_bridge_config_exits(load_server):
    with pytest.raises(SystemExit):
        load_server(ACS_CONNECTION_STRING="endpoint=https://x/;accesskey=eA==")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_server.py -q`
Expected: FAIL. `/` returns 200 when it should be disabled, and `BRIDGE` is a KeyError.

- [ ] **Step 3: Edit `server/server.py`**

Add `import sys` to the imports at the top (next to `import os`), and add this import after `from app.call_manager import CallManager`:
```python
from app.bridge_config import BridgeConfigError, load_bridge_config
```

Directly after the line `logger = logging.getLogger(__name__)`, insert:
```python
try:
    bridge = load_bridge_config(os.environ, acs_active=bool(os.getenv("ACS_CONNECTION_STRING")))
except BridgeConfigError as exc:
    logger.error("Bridge configuration error: %s", exc)
    sys.exit(1)
```

Replace:
```python
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = os.getenv("AZURE_VOICE_LIVE_ENDPOINT")
```
with:
```python
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = bridge.voice_live_endpoint
app.config["BRIDGE"] = bridge
app.config["VOICE_LIVE_API_VERSION"] = bridge.voice_live_api_version
app.config["INTERIM_RESPONSE"] = bridge.interim_response
```

In the `CallManager(...)` call, replace
`max_duration=int(os.getenv("MAX_CALL_DURATION", "3600")),` with:
```python
    max_duration=bridge.max_call_seconds,
```

Delete the decorator line `@app.websocket("/web/ws")` above `async def web_ws():`, and the decorator line `@app.route("/")` above `async def index():`. Directly after the `index` function body, insert:
```python
if bridge.enable_web_client:
    app.websocket("/web/ws")(web_ws)
    app.route("/")(index)
    logger.warning("Web debug client ENABLED (/web/ws is unauthenticated; local use only)")
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `python -m uv run pytest tests/test_server.py -q`
Expected: `5 passed`

- [ ] **Step 5: Run the full suite**

Run: `python -m uv run pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add server/server.py server/tests/test_server.py
git commit -m "feat: load bridge config at startup and disable web debug client by default"
```

---

### Task 5: Expiry reasons and cap/idle hooks

**Files:**
- Modify: `server/app/call_manager.py` (`is_expired`), `server/app/call_loop.py`, `server/app/handler/voicelive_media_handler.py` (add two no-op hooks)
- Test: `server/tests/test_call_loop.py`

**Interfaces:**
- Produces:
  - `CallManager.is_expired(call_id) -> str | None`, which returns `"duration"`, `"idle"` or `None`
  - `VoiceLiveMediaHandler.on_call_cap()` and `on_idle()`, both async and no-ops in the base class
  - `run_call_loop`, which calls `on_call_cap()` for `"duration"` and `on_idle()` for `"idle"`, then breaks

- [ ] **Step 1: Write the failing test** `server/tests/test_call_loop.py`

```python
import asyncio

from app.call_loop import run_call_loop
from app.call_manager import CallManager


async def test_is_expired_reasons():
    mgr = CallManager(max_concurrent=5, max_duration=10, idle_timeout=100)
    await mgr.acquire("c", "acs")
    assert mgr.is_expired("c") is None
    mgr._calls["c"].started_at -= 11
    assert mgr.is_expired("c") == "duration"
    mgr._calls["c"].started_at += 11
    mgr._calls["c"].last_activity -= 101
    assert mgr.is_expired("c") == "idle"
    assert mgr.is_expired("unknown") is None


class FakeManager:
    receive_timeout = 0.01

    def __init__(self, reasons):
        self._reasons = list(reasons)

    def is_expired(self, call_id):
        return self._reasons.pop(0) if self._reasons else None

    def touch(self, call_id):
        pass


class FakeWs:
    async def receive(self):
        await asyncio.sleep(1)


class FakeHandler:
    def __init__(self):
        self.calls = []

    async def connect_voicelive(self):
        await asyncio.sleep(10)

    async def on_message(self, msg):
        pass

    async def on_call_cap(self):
        self.calls.append("cap")

    async def on_idle(self):
        self.calls.append("idle")


async def test_duration_expiry_calls_cap_hook():
    handler = FakeHandler()
    await run_call_loop(call_manager=FakeManager(["duration"]), call_id="c", ws=FakeWs(), handler=handler)
    assert handler.calls == ["cap"]


async def test_idle_expiry_calls_idle_hook():
    handler = FakeHandler()
    await run_call_loop(call_manager=FakeManager([None, "idle"]), call_id="c", ws=FakeWs(), handler=handler)
    assert handler.calls == ["idle"]


async def test_base_handler_hooks_are_noops():
    from app.handler.voicelive_media_handler import VoiceLiveMediaHandler

    assert await VoiceLiveMediaHandler.on_call_cap(object()) is None
    assert await VoiceLiveMediaHandler.on_idle(object()) is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_call_loop.py -q`
Expected: FAIL. `is_expired` returns `True` instead of `"duration"`, and the hooks don't exist.

- [ ] **Step 3: Edit `server/app/call_manager.py` → `is_expired`**

Change the signature to `def is_expired(self, call_id: str) -> str | None:`. Replace the first `return False` (the missing-session case) with `return None`. Replace the `return True` in the max-duration branch with `return "duration"`, the `return True` in the idle branch with `return "idle"`, and the final `return False` with `return None`.

- [ ] **Step 4: Edit `server/app/call_loop.py`**

In the `CallHandler` Protocol, add:
```python
    async def on_call_cap(self) -> None: ...
    async def on_idle(self) -> None: ...
```
Replace:
```python
            if call_manager.is_expired(call_id):
                logger.warning("Call expired, disconnecting: call_id=%s", call_id)
                break
```
with:
```python
            expired = call_manager.is_expired(call_id)
            if expired:
                logger.warning("Call expired (%s), disconnecting: call_id=%s", expired, call_id)
                if expired == "duration":
                    await handler.on_call_cap()
                else:
                    await handler.on_idle()
                break
```

- [ ] **Step 5: Edit `server/app/handler/voicelive_media_handler.py`**

Directly above the `# Audio output to client` section banner, add a new section:
```python
    # ------------------------------------------------------------------
    # Lifecycle hooks — no-ops here; telephony subclasses override
    # ------------------------------------------------------------------

    async def on_call_cap(self):
        return None

    async def on_idle(self):
        return None
```

- [ ] **Step 6: Run the tests**

Run: `python -m uv run pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add server/app/call_manager.py server/app/call_loop.py server/app/handler/voicelive_media_handler.py server/tests/test_call_loop.py
git commit -m "feat: report expiry reason and route call-cap/idle through handler hooks"
```

---

### Task 6: Voice Live handler — agent mode, id logging, hooks

**Files:**
- Modify: `server/app/handler/voicelive_media_handler.py`
- Test: `server/tests/test_voicelive_handler.py`

**Interfaces:**
- Consumes: `AgentRoute` (Task 2), and the config keys `VOICE_LIVE_API_VERSION` and `INTERIM_RESPONSE` (Task 4)
- Produces:
  - `VoiceLiveMediaHandler(config, route: AgentRoute | None = None)`
  - Attributes: `route`, `session_id`, `conversation_id`
  - Property: `log_context -> str` (the base class returns `""`)
  - `connect_voicelive()`, which uses agent mode when `route` is set
  - `async on_voicelive_ended()`. The base class closes the client WebSocket with 1001, as upstream does.
  - `stop_forwarding_agent_audio() -> None`
  - `force_close() -> None`

- [ ] **Step 1: Write the failing test** `server/tests/test_voicelive_handler.py`

```python
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handler.voicelive_media_handler as vmh
from app.routing import AgentRoute

ROUTE = AgentRoute("proj", "agent-a", "10")


def handler_config(**overrides):
    cfg = {
        "AZURE_VOICE_LIVE_ENDPOINT": "https://vl.example",
        "VOICE_LIVE_MODEL": "gpt-4o-mini",
        "AZURE_VOICE_LIVE_API_KEY": "key",
        "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID": "cid",
        "AMBIENT_PRESET": "none",
    }
    cfg.update(overrides)
    return cfg


class FakeConn:
    def __init__(self, events=None, block=True):
        self.session = SimpleNamespace(update=AsyncMock())
        self.response = SimpleNamespace(create=AsyncMock())
        self._events = list(events or [])
        self._block = block

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        if self._block:
            await asyncio.sleep(3600)
        raise StopAsyncIteration


class FakeCtx:
    def __init__(self, conn):
        self.conn = conn
        self.exited = False

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        self.exited = True


class FakeCredential:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def close(self):
        pass


@pytest.fixture
def fake_sdk(monkeypatch):
    captured = {}
    conn = FakeConn()

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeCtx(captured.setdefault("_conn", conn))

    monkeypatch.setattr(vmh, "voicelive_connect", fake_connect)
    monkeypatch.setattr(vmh, "DefaultAzureCredential", FakeCredential)
    return captured


async def test_agent_mode_connects_with_pinned_route(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=ROUTE)
    await handler.connect_voicelive()
    assert fake_sdk["agent_name"] == "agent-a"
    assert fake_sdk["project_name"] == "proj"
    assert fake_sdk["agent_version"] == "10"
    assert "model" not in fake_sdk
    assert isinstance(fake_sdk["credential"], FakeCredential)
    assert fake_sdk["credential"].kwargs == {"managed_identity_client_id": "cid"}
    await handler.cleanup()


async def test_agent_mode_session_update_has_no_behavior_fields(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(), route=ROUTE)
    await handler.connect_voicelive()
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    for forbidden in ("instructions", "voice", "turn_detection", "input_audio_noise_reduction",
                      "input_audio_echo_cancellation", "interim_response"):
        assert forbidden not in sent
    assert sent["input_audio_format"] == "pcm16"
    assert sent["output_audio_format"] == "pcm16"
    fake_sdk["_conn"].response.create.assert_awaited_once()
    await handler.cleanup()


async def test_interim_response_only_when_configured(fake_sdk):
    interim = {"type": "llm_interim_response"}
    handler = vmh.VoiceLiveMediaHandler(handler_config(INTERIM_RESPONSE=interim), route=ROUTE)
    await handler.connect_voicelive()
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    assert sent["interim_response"] == interim
    await handler.cleanup()


async def test_api_version_passed_when_set(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(VOICE_LIVE_API_VERSION="2026-07-15"), route=ROUTE)
    await handler.connect_voicelive()
    assert fake_sdk["api_version"] == "2026-07-15"
    await handler.cleanup()


async def test_model_mode_unchanged_without_route(fake_sdk):
    handler = vmh.VoiceLiveMediaHandler(handler_config(AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID=""))
    await handler.connect_voicelive()
    assert fake_sdk["model"] == "gpt-4o-mini"
    assert "agent_name" not in fake_sdk
    sent = fake_sdk["_conn"].session.update.call_args.kwargs["session"].as_dict()
    assert "instructions" in sent
    await handler.cleanup()


async def test_stop_forwarding_drops_agent_audio():
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    handler._send_audio_to_client = AsyncMock()
    handler.stop_forwarding_agent_audio()
    await handler.on_audio_delta(b"\x00\x00")
    handler._send_audio_to_client.assert_not_called()


class RecordingHandler(vmh.VoiceLiveMediaHandler):
    ended = 0

    async def on_voicelive_ended(self):
        self.ended += 1


async def test_ended_hook_not_called_on_intentional_cleanup():
    handler = RecordingHandler(handler_config())
    handler.conn = FakeConn(block=True)
    handler._receiver_task = asyncio.create_task(handler._receiver_loop())
    await asyncio.sleep(0)
    await handler.cleanup()
    assert handler.ended == 0


async def test_ended_hook_called_when_voicelive_drops():
    handler = RecordingHandler(handler_config())
    handler.conn = FakeConn(block=False)
    await handler._receiver_loop()
    assert handler.ended == 1


async def test_session_and_conversation_ids_recorded():
    events = [
        SimpleNamespace(type=vmh.ServerEventType.SESSION_CREATED, session=SimpleNamespace(id="sess-1")),
        SimpleNamespace(type=vmh.ServerEventType.RESPONSE_DONE,
                        response=SimpleNamespace(id="r1", conversation_id="conv-9")),
    ]
    handler = RecordingHandler(handler_config())
    handler.conn = FakeConn(events=events, block=False)
    await handler._receiver_loop()
    assert handler.session_id == "sess-1"
    assert handler.conversation_id == "conv-9"


async def test_cleanup_and_force_close_safe_when_never_connected():
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    handler.force_close()
    await handler.cleanup()


def test_force_close_closes_underlying_response():
    closed = []
    handler = vmh.VoiceLiveMediaHandler(handler_config())
    handler.conn = SimpleNamespace(_connection=SimpleNamespace(_response=SimpleNamespace(close=lambda: closed.append(1))))
    handler.force_close()
    assert closed == [1]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_voicelive_handler.py -q`
Expected: FAIL. `__init__` doesn't accept `route`, and `DefaultAzureCredential` isn't imported.

- [ ] **Step 3: Edit the imports**

Replace `from azure.identity.aio import ManagedIdentityCredential` with:
```python
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
```

- [ ] **Step 4: Edit `__init__`**

Change the signature to `def __init__(self, config, route=None):`. Directly after `self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]`, add:
```python
        self.route = route
        self.api_version = config.get("VOICE_LIVE_API_VERSION")
        self.interim_response = config.get("INTERIM_RESPONSE")
        self.session_id = None
        self.conversation_id = None
        self._forward_agent_audio = True
```

- [ ] **Step 5: Split the session config**

Rename the existing `_session_config` method to `_model_session_config` without changing its body, then add above it:
```python
    @property
    def log_context(self) -> str:
        return ""

    def _session_config(self) -> RequestSession:
        if self.route is not None:
            return self._agent_session_config()
        return self._model_session_config()

    def _agent_session_config(self) -> RequestSession:
        fields = {"input_audio_format": "pcm16", "output_audio_format": "pcm16"}
        if self.interim_response:
            fields["interim_response"] = dict(self.interim_response)
        return RequestSession(fields)
```

- [ ] **Step 6: Replace the body of `connect_voicelive`**

```python
    async def connect_voicelive(self):
        """Connect to Azure Voice Live API using the SDK (agent mode when a route is set)."""
        t0 = time.perf_counter()

        if self.route is not None:
            self._credential = DefaultAzureCredential(managed_identity_client_id=self.client_id or None)
            connect_kwargs = {
                "endpoint": self.endpoint,
                "credential": self._credential,
                "agent_name": self.route.agent,
                "project_name": self.route.project,
                "agent_version": self.route.version,
            }
            logger.info(
                "[VoiceLive] Agent mode project=%s agent=%s version=%s %s",
                self.route.project, self.route.agent, self.route.version, self.log_context,
            )
        else:
            if self.client_id:
                self._credential = ManagedIdentityCredential(client_id=self.client_id)
                credential = self._credential
            else:
                credential = AzureKeyCredential(self.api_key)
            connect_kwargs = {"endpoint": self.endpoint, "credential": credential, "model": self.model.strip()}

        if self.api_version:
            connect_kwargs["api_version"] = self.api_version

        t1 = time.perf_counter()
        logger.info("[VoiceLive] Credential prepared in %.2fs", t1 - t0)

        self._conn_ctx = voicelive_connect(**connect_kwargs)
        self.conn = await self._conn_ctx.__aenter__()

        t2 = time.perf_counter()
        logger.info("[VoiceLive] SDK connected in %.2fs (total %.2fs)", t2 - t1, t2 - t0)
        self._voicelive_connected = True

        await self.conn.session.update(session=self._session_config())
        await self.conn.response.create()

        self._receiver_task = asyncio.create_task(self._receiver_loop())
```

- [ ] **Step 7: Edit `_receiver_loop`**

Replace the `SESSION_CREATED` case body with:
```python
                        self.session_id = event.session.id if hasattr(event, "session") else None
                        logger.info("[VoiceLive] Session ID: %s %s", self.session_id, self.log_context)
```
Replace the `RESPONSE_DONE` case body with:
```python
                        response = getattr(event, "response", None)
                        logger.info("[VoiceLive] Response done: id=%s", getattr(response, "id", None))
                        conversation_id = getattr(response, "conversation_id", None)
                        if conversation_id and conversation_id != self.conversation_id:
                            self.conversation_id = conversation_id
                            logger.info(
                                "[VoiceLive] conversation_id=%s session_id=%s %s",
                                conversation_id, self.session_id, self.log_context,
                            )
```
Replace the whole `finally:` block of `_receiver_loop` with:
```python
        finally:
            self._voicelive_connected = False
            if not cancelled:
                await self.on_voicelive_ended()
```
Add this method directly after `_receiver_loop`:
```python
    async def on_voicelive_ended(self):
        """Voice Live dropped unexpectedly: close the client WebSocket so the caller-side loop exits."""
        if self.client_ws:
            try:
                logger.warning("[VoiceLive] Voice Live disconnected — closing client WebSocket")
                await self.client_ws.close(1001)
            except Exception:
                pass
```

- [ ] **Step 8: Add forwarding control and force close**

At the top of `on_audio_delta`, before its first line, add:
```python
        if not self._forward_agent_audio:
            return
```
Add to the lifecycle hooks section from Task 5:
```python
    def stop_forwarding_agent_audio(self) -> None:
        self._forward_agent_audio = False

    def force_close(self) -> None:
        """Abort the Voice Live socket without waiting for a close handshake."""
        if self._receiver_task:
            self._receiver_task.cancel()
        ws = getattr(self.conn, "_connection", None)
        response = getattr(ws, "_response", None)
        if response is not None:
            response.close()
        self._voicelive_connected = False
```

- [ ] **Step 9: Run the tests**

Run: `python -m uv run pytest -q`
Expected: all pass

- [ ] **Step 10: Commit**

```bash
git add server/app/handler/voicelive_media_handler.py server/tests/test_voicelive_handler.py
git commit -m "feat: connect Voice Live in pinned agent mode without overriding agent behavior"
```

---

### Task 7: Signing and the call session lifecycle

**Files:**
- Create: `server/app/providers/acs/signing.py`, `server/app/providers/acs/call_session.py`
- Test: `server/tests/test_signing.py`, `server/tests/test_call_session.py`

**Interfaces:**
- Consumes: `AgentRoute` (Task 2), and the handler methods `stop_forwarding_agent_audio()`, `cleanup()` and `force_close()` (Task 6)
- Produces:
  - `sign(secret: str, purpose: str, call_key: str) -> str`
  - `verify(secret: str, purpose: str, call_key: str, signature: str | None) -> bool`
  - `SessionSettings`, a frozen dataclass: `fallback_message`, `goodbye_message`, `tts_voice`, `media_connect_timeout=10.0`, `voice_live_connect_timeout=8.0`, `media_lost_grace=5.0`, `wait_timeout=5.0`, `close_timeout=5.0`, `play_safety_timeout=15.0`
  - `async close_voicelive(handler, timeout: float, log_context: str) -> None`
  - `CallSessionRegistry`: `add(s)`, `index_connection(s)`, `get(call_key)`, `get_by_connection(conn_id)`, `remove(s)`, `sweep(max_age, now=None) -> int`, `__len__`
  - `CallSession(*, call_key, route, masked_caller, masked_called, settings, acs_client, registry)`, with:
    - attributes `call_key`, `route`, `settings`, `call_connection_id`, `handler`, `ws_used`, `terminated_reason`, `disconnected`, `hangup_after_play`, `created_at`, `log_context`
    - methods `set_answered(conn_id)`, `mark_connected()`, `mark_answer_failed()`, `request_end(reason, message)`, `ensure_voicelive_closed() -> Task | None`, `on_media_ws_closed()`, `on_call_disconnected()`, `on_play_done(failed: bool)`

`acs_client` is duck-typed. It needs `get_call_connection(id)`, which returns an object with async `play_media(play_source=, play_to=, operation_context=)` and async `hang_up(is_for_everyone=)`.

- [ ] **Step 1: Write the failing signing test** `server/tests/test_signing.py`

```python
import pytest

from app.providers.acs.signing import sign, verify

SECRET = "s" * 40


def test_sign_is_hex_and_purpose_bound():
    ws = sign(SECRET, "ws", "abc")
    cb = sign(SECRET, "cb", "abc")
    assert ws != cb
    assert all(c in "0123456789abcdef" for c in ws) and len(ws) == 64


def test_verify_round_trip():
    assert verify(SECRET, "ws", "abc", sign(SECRET, "ws", "abc"))


@pytest.mark.parametrize("sig", [None, "", "00", "zz" * 32, "é" * 64, sign(SECRET, "cb", "abc")])
def test_verify_rejects_bad_signatures_without_raising(sig):
    assert verify(SECRET, "ws", "abc", sig) is False


def test_verify_rejects_other_call_key():
    assert verify(SECRET, "ws", "other", sign(SECRET, "ws", "abc")) is False
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_signing.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement** `server/app/providers/acs/signing.py`

```python
import hashlib
import hmac


def sign(secret: str, purpose: str, call_key: str) -> str:
    return hmac.new(secret.encode(), f"{purpose}:{call_key}".encode(), hashlib.sha256).hexdigest()


def verify(secret: str, purpose: str, call_key: str, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign(secret, purpose, call_key).encode()
    return hmac.compare_digest(expected, signature.encode("utf-8", "replace"))
```

- [ ] **Step 4: Confirm the signing tests pass**

Run: `python -m uv run pytest tests/test_signing.py -q`
Expected: all pass

- [ ] **Step 5: Write the failing session test** `server/tests/test_call_session.py`

```python
import asyncio

import pytest
from azure.core.exceptions import HttpResponseError

from app.providers.acs.call_session import CallSession, CallSessionRegistry, SessionSettings
from app.routing import AgentRoute

ROUTE = AgentRoute("proj", "agent-a", "10")
FALLBACK = "fallback text"
GOODBYE = "goodbye text"
FAST = SessionSettings(
    fallback_message=FALLBACK,
    goodbye_message=GOODBYE,
    tts_voice="en-US-JennyNeural",
    media_connect_timeout=0.05,
    voice_live_connect_timeout=0.05,
    media_lost_grace=0.05,
    wait_timeout=0.05,
    close_timeout=0.05,
    play_safety_timeout=0.1,
)


class FakeHandler:
    def __init__(self, cleanup_delay=0.0):
        self.stopped = False
        self.cleanups = 0
        self.cleanup_done = 0
        self.forced = False
        self._delay = cleanup_delay

    def stop_forwarding_agent_audio(self):
        self.stopped = True

    async def cleanup(self):
        self.cleanups += 1
        await asyncio.sleep(self._delay)
        self.cleanup_done += 1

    def force_close(self):
        self.forced = True


def make(fake_acs, route=ROUTE, registry=None):
    registry = registry or CallSessionRegistry()
    session = CallSession(
        call_key="k" * 32, route=route, masked_caller="***9876", masked_called="***1234",
        settings=FAST, acs_client=fake_acs, registry=registry,
    )
    registry.add(session)
    return session, registry


async def settle(seconds=0.02):
    await asyncio.sleep(seconds)


def connected(session):
    session.set_answered("conn-1")
    session.mark_connected()


async def test_request_end_is_idempotent_first_reason_wins(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    assert session.request_end("call_cap", GOODBYE) is None
    session.request_end("idle", FALLBACK)
    await settle()
    assert session.terminated_reason == "call_cap"
    assert fake_acs.log == [("play", GOODBYE)]


async def test_media_watchdog_plays_fallback_and_hangs_up_after_play(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    await settle(0.1)
    assert session.terminated_reason == "media_timeout"
    assert fake_acs.log == [("play", FALLBACK)]
    session.on_play_done(failed=False)
    await settle()
    assert fake_acs.log == [("play", FALLBACK), ("hang_up", True)]


async def test_media_ws_before_call_connected_disarms_watchdog(fake_acs):
    session, _ = make(fake_acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.mark_connected()
    await settle(0.1)
    assert session.terminated_reason is None


async def test_caller_hangup_never_plays_and_removes_session(fake_acs):
    session, registry = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.handler = FakeHandler()
    session.on_call_disconnected()
    await settle()
    assert session.terminated_reason == "caller_hangup"
    assert fake_acs.log == []
    assert len(registry) == 0
    assert session.handler.cleanups == 1


@pytest.mark.parametrize("reason, message", [("call_cap", GOODBYE), ("voicelive_dropped", FALLBACK), ("idle", FALLBACK)])
async def test_every_reason_is_removed_on_disconnect(fake_acs, reason, message):
    session, registry = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end(reason, message)
    await settle()
    session.on_play_done(failed=False)
    session.on_call_disconnected()
    await settle()
    assert len(registry) == 0


async def test_safety_timer_hangs_up_without_play_event(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle(0.2)
    assert fake_acs.log == [("play", GOODBYE), ("hang_up", True)]


async def test_play_done_cancels_safety_timer_single_hangup(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle()
    session.on_play_done(failed=False)
    await settle(0.2)
    assert fake_acs.log.count(("hang_up", True)) == 1


async def test_play_media_failure_still_hangs_up_without_raising(fake_acs):
    fake_acs.play_error = HttpResponseError("call gone")
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("voicelive_dropped", FALLBACK)
    await settle()
    assert fake_acs.log == [("hang_up", True)]


async def test_connected_wait_timeout_hangs_up_without_play(fake_acs):
    session, _ = make(fake_acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.request_end("voicelive_connect_failed", FALLBACK)
    await settle(0.1)
    assert fake_acs.log == [("hang_up", True)]


async def test_goodbye_plays_without_waiting_for_voicelive_close(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.handler = FakeHandler(cleanup_delay=1.0)
    session.request_end("call_cap", GOODBYE)
    await settle()
    assert fake_acs.log == [("play", GOODBYE)]
    assert session.handler.stopped is True
    assert session.handler.cleanup_done == 0


async def test_voicelive_closed_once_when_terminate_and_disconnect_both_ask(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.handler = FakeHandler(cleanup_delay=0.01)
    session.request_end("call_cap", GOODBYE)
    session.on_call_disconnected()
    await settle(0.1)
    assert session.handler.cleanups == 1


async def test_hung_cleanup_is_force_closed(fake_acs):
    session, _ = make(fake_acs)
    session.handler = FakeHandler(cleanup_delay=1.0)
    session.on_call_disconnected()
    await settle(0.15)
    assert session.handler.forced is True


async def test_disconnect_during_connected_wait_means_no_play(fake_acs):
    session, _ = make(fake_acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle(0.01)
    session.on_call_disconnected()
    await settle(0.2)
    assert fake_acs.log == []


async def test_disconnect_within_grace_window_means_no_fallback(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.on_media_ws_closed()
    session.on_call_disconnected()
    await settle(0.1)
    assert session.terminated_reason == "caller_hangup"
    assert fake_acs.log == []


async def test_media_lost_after_grace(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.on_media_ws_closed()
    await settle(0.1)
    assert session.terminated_reason == "media_lost"
    assert fake_acs.log == [("play", FALLBACK)]


async def test_route_miss_plays_fallback_on_connected(fake_acs):
    session, _ = make(fake_acs, route=None)
    session.set_answered("conn-1")
    session.mark_connected()
    await settle()
    assert session.terminated_reason == "route_miss"
    assert fake_acs.log == [("play", FALLBACK)]


async def test_callback_before_answer_returns_still_plays(fake_acs):
    session, _ = make(fake_acs, route=None)
    session.mark_connected()
    await settle(0.01)
    session.set_answered("conn-1")
    await settle()
    assert fake_acs.log == [("play", FALLBACK)]


async def test_answer_failed_removes_session(fake_acs):
    session, registry = make(fake_acs)
    session.mark_answer_failed()
    assert session.terminated_reason == "answer_failed"
    assert len(registry) == 0


async def test_terminated_reason_logged_exactly_once(fake_acs, logs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    session.request_end("idle", FALLBACK)
    session.on_call_disconnected()
    await settle(0.1)
    assert logs.text.count("call_ended") == 1
    assert "9876" not in logs.text.replace("***9876", "")


async def test_sweep_ends_and_removes_stale_sessions(fake_acs):
    session, registry = make(fake_acs)
    session.set_answered("conn-1")
    assert registry.get_by_connection("conn-1") is session
    removed = registry.sweep(max_age=10, now=session.created_at + 11)
    await settle()
    assert removed == 1
    assert session.terminated_reason == "stale"
    assert len(registry) == 0
    assert registry.get_by_connection("conn-1") is None
```

- [ ] **Step 6: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_call_session.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.acs.call_session'`

- [ ] **Step 7: Implement** `server/app/providers/acs/call_session.py`

```python
"""Per-call state for ACS calls. Every end path goes through CallSession.request_end()."""

import asyncio
import logging
import time
from dataclasses import dataclass

from azure.communication.callautomation import TextSource
from azure.core.exceptions import HttpResponseError

from app.routing import AgentRoute

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionSettings:
    fallback_message: str
    goodbye_message: str
    tts_voice: str
    media_connect_timeout: float = 10.0
    voice_live_connect_timeout: float = 8.0
    media_lost_grace: float = 5.0
    wait_timeout: float = 5.0
    close_timeout: float = 5.0
    play_safety_timeout: float = 15.0


async def close_voicelive(handler, timeout: float, log_context: str) -> None:
    cleanup = asyncio.ensure_future(handler.cleanup())
    try:
        await asyncio.wait_for(asyncio.shield(cleanup), timeout)
    except TimeoutError:
        logger.warning("voicelive_force_closed %s", log_context)
        try:
            handler.force_close()
        except Exception:
            logger.exception("force_close failed %s", log_context)
    except Exception:
        logger.exception("voicelive cleanup failed %s", log_context)


class CallSessionRegistry:
    def __init__(self):
        self._by_key: dict[str, "CallSession"] = {}
        self._by_conn: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._by_key)

    def add(self, session: "CallSession") -> None:
        self._by_key[session.call_key] = session

    def index_connection(self, session: "CallSession") -> None:
        if session.call_connection_id:
            self._by_conn[session.call_connection_id] = session.call_key

    def get(self, call_key: str) -> "CallSession | None":
        return self._by_key.get(call_key)

    def get_by_connection(self, call_connection_id: str) -> "CallSession | None":
        key = self._by_conn.get(call_connection_id)
        return self._by_key.get(key) if key else None

    def remove(self, session: "CallSession") -> None:
        if self._by_key.get(session.call_key) is session:
            del self._by_key[session.call_key]
        conn = session.call_connection_id
        if conn and self._by_conn.get(conn) == session.call_key:
            del self._by_conn[conn]

    def sweep(self, max_age: float, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        stale = [s for s in self._by_key.values() if now - s.created_at > max_age]
        for session in stale:
            session.request_end("stale", None)
            self.remove(session)
        return len(stale)


class CallSession:
    def __init__(self, *, call_key: str, route: AgentRoute | None, masked_caller: str, masked_called: str,
                 settings: SessionSettings, acs_client, registry: CallSessionRegistry):
        self.call_key = call_key
        self.route = route
        self.masked_caller = masked_caller
        self.masked_called = masked_called
        self.settings = settings
        self._acs_client = acs_client
        self._registry = registry

        self.call_connection_id: str | None = None
        self.handler = None
        self.ws_used = False
        self.terminated_reason: str | None = None
        self.ended_at: float | None = None
        self.disconnected = False
        self.hangup_after_play = False
        self.created_at = time.monotonic()

        self._hung_up = False
        self._answered = asyncio.Event()
        self._connected = asyncio.Event()
        self._timers: set[asyncio.Task] = set()
        self._safety_timer: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def log_context(self) -> str:
        return f"call_key={self.call_key[:8]} conn={self.call_connection_id}"

    # --- task helpers -------------------------------------------------

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_timer(self, delay: float, action) -> None:
        async def _run():
            await asyncio.sleep(delay)
            action()

        task = self._spawn(_run())
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    def _cancel_timers(self) -> None:
        for task in list(self._timers):
            task.cancel()
        self._timers.clear()

    # --- ACS lifecycle inputs ------------------------------------------

    def set_answered(self, call_connection_id: str) -> None:
        self.call_connection_id = call_connection_id
        self._registry.index_connection(self)
        self._answered.set()
        if self.route is not None and self.terminated_reason is None:
            self._start_timer(self.settings.media_connect_timeout, self._media_watchdog_fired)

    def mark_connected(self) -> None:
        self._connected.set()
        if self.route is None:
            self.request_end("route_miss", self.settings.fallback_message)

    def mark_answer_failed(self) -> None:
        if self.terminated_reason is None:
            self.terminated_reason = "answer_failed"
            self.ended_at = time.monotonic()
            logger.error(
                "call_ended reason=answer_failed caller=%s called=%s %s",
                self.masked_caller, self.masked_called, self.log_context,
            )
        self._cancel_timers()
        self._registry.remove(self)

    def on_media_ws_closed(self) -> None:
        if self.terminated_reason is None and not self.disconnected:
            self._start_timer(self.settings.media_lost_grace, self._media_grace_expired)

    def on_call_disconnected(self) -> None:
        self.disconnected = True
        self.request_end("caller_hangup", None)
        self._cancel_timers()
        if self._safety_timer is not None:
            self._safety_timer.cancel()
        self._spawn(self._finalize())

    def on_play_done(self, failed: bool) -> None:
        if failed:
            logger.warning("play_failed %s", self.log_context)
        if self.hangup_after_play:
            if self._safety_timer is not None:
                self._safety_timer.cancel()
            self._spawn(self._safe_hang_up())

    # --- timers -------------------------------------------------------

    def _media_watchdog_fired(self) -> None:
        if self.terminated_reason is None and not self.ws_used:
            self.request_end("media_timeout", self.settings.fallback_message)

    def _media_grace_expired(self) -> None:
        if self.terminated_reason is None and not self.disconnected:
            self.request_end("media_lost", self.settings.fallback_message)

    async def _play_safety_timeout(self) -> None:
        await asyncio.sleep(self.settings.play_safety_timeout)
        if self.disconnected or self._registry.get(self.call_key) is not self:
            return
        logger.warning("hangup_timeout %s", self.log_context)
        await self._safe_hang_up()

    # --- ending -------------------------------------------------------

    def request_end(self, reason: str, message: str | None) -> None:
        if self.terminated_reason is not None:
            return
        self.terminated_reason = reason
        self.ended_at = time.monotonic()
        self._spawn(self._terminate(message))

    def ensure_voicelive_closed(self) -> asyncio.Task | None:
        if self._close_task is None and self.handler is not None:
            self._close_task = self._spawn(self._close_and_log())
        return self._close_task

    async def _close_and_log(self) -> None:
        await close_voicelive(self.handler, self.settings.close_timeout, self.log_context)
        since = self.ended_at if self.ended_at is not None else time.monotonic()
        logger.info("voicelive_closed_ms=%.0f %s", (time.monotonic() - since) * 1000, self.log_context)

    async def _wait(self, event: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(event.wait(), self.settings.wait_timeout)
            return True
        except TimeoutError:
            return False

    async def _terminate(self, message: str | None) -> None:
        try:
            logger.info(
                "call_ended reason=%s caller=%s called=%s %s",
                self.terminated_reason, self.masked_caller, self.masked_called, self.log_context,
            )
            self._cancel_timers()
            if self.handler is not None:
                self.handler.stop_forwarding_agent_audio()
                self.ensure_voicelive_closed()
            if self.disconnected:
                return
            if not message:
                await self._safe_hang_up()
                return
            ready = await self._wait(self._answered) and await self._wait(self._connected)
            if self.disconnected:
                return
            if not ready:
                await self._safe_hang_up()
                return
            self.hangup_after_play = True
            self._safety_timer = self._spawn(self._play_safety_timeout())
            try:
                await self._acs_client.get_call_connection(self.call_connection_id).play_media(
                    play_source=TextSource(text=message, voice_name=self.settings.tts_voice),
                    play_to="all",
                    operation_context=f"bridge-{self.terminated_reason}",
                )
            except Exception:
                logger.warning("play_media failed %s", self.log_context, exc_info=True)
                self._safety_timer.cancel()
                await self._safe_hang_up()
        except Exception:
            logger.exception("terminate failed %s", self.log_context)

    async def _safe_hang_up(self) -> None:
        if self.disconnected or self._hung_up or not self.call_connection_id:
            return
        self._hung_up = True
        try:
            await self._acs_client.get_call_connection(self.call_connection_id).hang_up(is_for_everyone=True)
        except HttpResponseError as exc:
            logger.info("hang_up ignored status=%s %s", exc.status_code, self.log_context)
        except Exception:
            logger.warning("hang_up failed %s", self.log_context, exc_info=True)

    async def _finalize(self) -> None:
        try:
            task = self.ensure_voicelive_closed()
            if task is not None:
                await task
        finally:
            self._registry.remove(self)
```

- [ ] **Step 8: Run the tests**

Run: `python -m uv run pytest tests/test_signing.py tests/test_call_session.py -q`
Expected: all pass. If a timing-based test flakes on a slow machine, double the `settle()` value in that test; do not change the production defaults.

- [ ] **Step 9: Commit**

```bash
git add server/app/providers/acs/signing.py server/app/providers/acs/call_session.py server/tests/test_signing.py server/tests/test_call_session.py
git commit -m "feat: add single-path idempotent call session lifecycle with bounded Voice Live close"
```

---

### Task 8: IncomingCall answer and callback dispatch

**Files:**
- Create: `server/app/providers/acs/bridge_calls.py`
- Test: `server/tests/test_bridge_calls.py`

**Interfaces:**
- Consumes: `BridgeConfig` (Task 3), routing helpers (Task 2), `mask_number` (Task 1), `sign` (Task 7), `CallSession`, `CallSessionRegistry` and `SessionSettings` (Task 7)
- Produces:
  - `settings_from_bridge(bridge: BridgeConfig) -> SessionSettings`
  - `BridgeCallController(*, acs_client, bridge, registry, public_base_url_override: str = "")`, with:
    - `async handle_incoming(events: list | None, host_url: str) -> tuple[dict | str, int]`
    - `handle_callbacks(call_key: str, events: list | None) -> None`, which is synchronous and never awaits the end of a call

- [ ] **Step 1: Write the failing test** `server/tests/test_bridge_calls.py`

```python
import asyncio

from app.bridge_config import load_bridge_config
from app.providers.acs.bridge_calls import BridgeCallController
from app.providers.acs.call_session import CallSessionRegistry
from app.providers.acs.signing import sign
from tests.conftest import TOKEN, acs_env

HOST = "https://bridge.example"


def incoming(to_number="+14165551234", context="ctx-1", to=None):
    data = {
        "to": to if to is not None else {"kind": "phoneNumber", "phoneNumber": {"value": to_number}},
        "from": {"kind": "phoneNumber", "phoneNumber": {"value": "+16475559876"}},
    }
    if context is not None:
        data["incomingCallContext"] = context
    return [{"eventType": "Microsoft.Communication.IncomingCall", "data": data}]


def make(fake_acs):
    bridge = load_bridge_config(acs_env(), acs_active=True)
    registry = CallSessionRegistry()
    return BridgeCallController(acs_client=fake_acs, bridge=bridge, registry=registry), registry


async def test_subscription_validation(fake_acs):
    controller, _ = make(fake_acs)
    body, status = await controller.handle_incoming(
        [{"eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "abc"}}], HOST
    )
    assert (body, status) == ({"validationResponse": "abc"}, 200)


async def test_route_hit_answers_with_signed_media_and_no_numbers_in_urls(fake_acs):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(), HOST)
    assert status == 200
    (session,) = [registry.get(k) for k in list(registry._by_key)]
    kwargs = fake_acs.answer_kwargs
    ws_url = kwargs["media_streaming"].transport_url
    assert ws_url == f"wss://bridge.example/acs/ws/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}"
    assert kwargs["callback_url"] == f"{HOST}/acs/callbacks/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}"
    assert kwargs["cognitive_services_endpoint"] == "https://cog.example"
    for url in (ws_url, kwargs["callback_url"]):
        assert "4165551234" not in url and "6475559876" not in url and TOKEN not in url
    assert session.call_connection_id == "conn-1"
    assert session.route.agent == "agent-a"


async def test_session_inserted_before_answer_call(fake_acs):
    controller, registry = make(fake_acs)
    seen = []
    fake_acs.on_answer = lambda: seen.append(len(registry))
    await controller.handle_incoming(incoming(), HOST)
    assert seen == [1]


async def test_route_miss_answers_without_media_and_plays_fallback(fake_acs):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(to_number="+14165550000"), HOST)
    assert "media_streaming" not in fake_acs.answer_kwargs
    (key,) = list(registry._by_key)
    controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallConnected", "data": {}}])
    await asyncio.sleep(0.02)
    assert fake_acs.log == [("play", controller.bridge.fallback_message)]


async def test_non_phone_called_identity_is_route_miss(fake_acs):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(to={"kind": "communicationUser", "rawId": "8:acs:res_user"}), HOST)
    (session,) = list(registry._by_key.values())
    assert session.route is None
    assert "media_streaming" not in fake_acs.answer_kwargs


async def test_missing_incoming_call_context_is_400_without_session(fake_acs):
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(context=None), HOST)
    assert status == 400
    assert len(registry) == 0
    assert fake_acs.answer_kwargs is None


async def test_answer_failure_removes_session(fake_acs):
    fake_acs.answer_error = RuntimeError("boom")
    controller, registry = make(fake_acs)
    _, status = await controller.handle_incoming(incoming(), HOST)
    assert status == 200
    assert len(registry) == 0


async def test_unknown_event_type_is_400(fake_acs):
    controller, _ = make(fake_acs)
    _, status = await controller.handle_incoming([{"eventType": "Other", "data": {}}], HOST)
    assert status == 400


async def test_callbacks_for_unknown_key_or_empty_body_do_not_raise(fake_acs):
    controller, _ = make(fake_acs)
    controller.handle_callbacks("nope", [{"type": "Microsoft.Communication.CallConnected"}])
    controller.handle_callbacks("nope", None)


async def test_callback_dispatch(fake_acs):
    controller, registry = make(fake_acs)
    await controller.handle_incoming(incoming(), HOST)
    (key,) = list(registry._by_key)
    session = registry.get(key)
    controller.handle_callbacks(key, None)
    controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallConnected", "data": {}}])
    assert session._connected.is_set()
    controller.handle_callbacks(key, [{"type": "Microsoft.Communication.CallDisconnected", "data": {}}])
    await asyncio.sleep(0.02)
    assert session.terminated_reason == "caller_hangup"
    assert len(registry) == 0


async def test_logs_never_contain_full_numbers(fake_acs, logs):
    controller, _ = make(fake_acs)
    await controller.handle_incoming(incoming(), HOST)
    assert "4165551234" not in logs.text
    assert "6475559876" not in logs.text
    assert "***1234" in logs.text


async def test_dev_tunnel_override(fake_acs):
    bridge = load_bridge_config(acs_env(), acs_active=True)
    controller = BridgeCallController(
        acs_client=fake_acs, bridge=bridge, registry=CallSessionRegistry(),
        public_base_url_override="https://tunnel.example/",
    )
    await controller.handle_incoming(incoming(), HOST)
    assert fake_acs.answer_kwargs["callback_url"].startswith("https://tunnel.example/acs/callbacks/")
    assert fake_acs.answer_kwargs["media_streaming"].transport_url.startswith("wss://tunnel.example/acs/ws/")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_bridge_calls.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement** `server/app/providers/acs/bridge_calls.py`

```python
"""Answers ACS IncomingCall events and dispatches Call Automation callbacks to CallSessions."""

import logging
import uuid
from urllib.parse import urlparse

from azure.communication.callautomation import (
    AudioFormat,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    StreamingTransportType,
)

from app.bridge_config import BridgeConfig
from app.log_mask import mask_number
from app.providers.acs.call_session import CallSession, CallSessionRegistry, SessionSettings
from app.providers.acs.signing import sign
from app.routing import called_number_from_event, caller_number_from_event, resolve_route

logger = logging.getLogger(__name__)

VALIDATION_EVENT = "Microsoft.EventGrid.SubscriptionValidationEvent"
INCOMING_CALL_EVENT = "Microsoft.Communication.IncomingCall"


def settings_from_bridge(bridge: BridgeConfig) -> SessionSettings:
    return SessionSettings(
        fallback_message=bridge.fallback_message,
        goodbye_message=bridge.goodbye_message,
        tts_voice=bridge.tts_voice,
        media_connect_timeout=bridge.media_connect_timeout,
        voice_live_connect_timeout=bridge.voice_live_connect_timeout,
        media_lost_grace=bridge.media_lost_grace,
    )


class BridgeCallController:
    def __init__(self, *, acs_client, bridge: BridgeConfig, registry: CallSessionRegistry,
                 public_base_url_override: str = ""):
        self.acs_client = acs_client
        self.bridge = bridge
        self.registry = registry
        self.public_base_url_override = public_base_url_override
        self.settings = settings_from_bridge(bridge)

    async def handle_incoming(self, events: list | None, host_url: str) -> tuple[dict | str, int]:
        for event in events or []:
            event_type = event.get("eventType")
            if event_type == VALIDATION_EVENT:
                return {"validationResponse": event["data"]["validationCode"]}, 200
            if event_type == INCOMING_CALL_EVENT:
                return await self._answer(event.get("data") or {}, host_url)
        return "", 400

    async def _answer(self, data: dict, host_url: str) -> tuple[str, int]:
        context = data.get("incomingCallContext")
        called = called_number_from_event(data)
        caller = caller_number_from_event(data)
        if not context:
            logger.warning("incoming_call missing incomingCallContext called=%s", mask_number(called))
            return "", 400

        route = resolve_route(self.bridge.routes, called)
        session = CallSession(
            call_key=uuid.uuid4().hex,
            route=route,
            masked_caller=mask_number(caller),
            masked_called=mask_number(called),
            settings=self.settings,
            acs_client=self.acs_client,
            registry=self.registry,
        )
        self.registry.add(session)

        base = (self.public_base_url_override or host_url).rstrip("/")
        secret = self.bridge.media_ws_token
        kwargs = {
            "incoming_call_context": context,
            "callback_url": f"{base}/acs/callbacks/{session.call_key}/{sign(secret, 'cb', session.call_key)}",
            "cognitive_services_endpoint": self.bridge.acs_cognitive_services_endpoint,
            "operation_context": "bridge",
        }
        if route is not None:
            netloc = urlparse(base).netloc
            kwargs["media_streaming"] = MediaStreamingOptions(
                transport_url=f"wss://{netloc}/acs/ws/{session.call_key}/{sign(secret, 'ws', session.call_key)}",
                transport_type=StreamingTransportType.WEBSOCKET,
                content_type=MediaStreamingContentType.AUDIO,
                audio_channel_type=MediaStreamingAudioChannelType.MIXED,
                start_media_streaming=True,
                enable_bidirectional=True,
                audio_format=AudioFormat.PCM24_K_MONO,
            )

        logger.info(
            "incoming_call route=%s caller=%s called=%s %s",
            "hit" if route else "miss", session.masked_caller, session.masked_called, session.log_context,
        )
        try:
            result = await self.acs_client.answer_call(**kwargs)
        except Exception:
            logger.exception("answer_call failed %s", session.log_context)
            session.mark_answer_failed()
            return "", 200
        session.set_answered(result.call_connection_id)
        return "", 200

    def handle_callbacks(self, call_key: str, events: list | None) -> None:
        session = self.registry.get(call_key)
        if session is None:
            logger.info("callback for unknown call_key=%s", call_key[:8])
            return
        for event in events or []:
            event_type = (event or {}).get("type", "")
            if event_type == "Microsoft.Communication.CallConnected":
                session.mark_connected()
            elif event_type == "Microsoft.Communication.CallDisconnected":
                session.on_call_disconnected()
            elif event_type == "Microsoft.Communication.PlayCompleted":
                session.on_play_done(failed=False)
            elif event_type == "Microsoft.Communication.PlayFailed":
                session.on_play_done(failed=True)
            elif event_type == "Microsoft.Communication.MediaStreamingFailed":
                logger.warning("media_streaming_failed %s", session.log_context)
```

- [ ] **Step 4: Run the tests**

Run: `python -m uv run pytest tests/test_bridge_calls.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add server/app/providers/acs/bridge_calls.py server/tests/test_bridge_calls.py
git commit -m "feat: answer ACS calls by called-number route with signed callback and media URLs"
```

---

### Task 9: `ACSMediaHandler` bound to a `CallSession`

**Files:**
- Modify: `server/app/providers/acs/media_handler.py`
- Test: `server/tests/test_acs_media_handler.py`

**Interfaces:**
- Consumes: the `VoiceLiveMediaHandler` hooks (Tasks 5 and 6), and the `CallSession` methods `request_end`, `ensure_voicelive_closed`, `terminated_reason`, `settings` and `log_context` (Task 7)
- Produces: `ACSMediaHandler(config, session=None)`. It overrides `connect_voicelive` (adding a timeout and turning failures into `request_end`), `on_voicelive_ended`, `on_call_cap`, `on_idle`, `stop_forwarding_agent_audio` (which also sends ACS `StopAudio`) and `log_context`.

- [ ] **Step 1: Write the failing test** `server/tests/test_acs_media_handler.py`

```python
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handler.voicelive_media_handler as vmh
from app.providers.acs.call_session import SessionSettings
from app.providers.acs.media_handler import ACSMediaHandler
from app.routing import AgentRoute
from tests.test_voicelive_handler import handler_config

SETTINGS = SessionSettings(fallback_message="fb", goodbye_message="bye", tts_voice="v", voice_live_connect_timeout=0.05)


class FakeSession:
    def __init__(self):
        self.route = AgentRoute("proj", "agent-a", "10")
        self.settings = SETTINGS
        self.terminated_reason = None
        self.log_context = "call_key=abc conn=conn-1"
        self.ends = []
        self.closes = 0

    def request_end(self, reason, message):
        self.ends.append((reason, message))
        self.terminated_reason = self.terminated_reason or reason

    def ensure_voicelive_closed(self):
        self.closes += 1


@pytest.fixture
def session():
    return FakeSession()


def patch_base_connect(monkeypatch, coro):
    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", coro)


async def test_uses_session_route(session):
    handler = ACSMediaHandler(handler_config(), session=session)
    assert handler.route == session.route
    assert handler.log_context == session.log_context


async def test_connect_failure_requests_fallback_and_reraises(monkeypatch, session):
    async def boom(self):
        raise RuntimeError("bad agent version")

    patch_base_connect(monkeypatch, boom)
    handler = ACSMediaHandler(handler_config(), session=session)
    with pytest.raises(RuntimeError):
        await handler.connect_voicelive()
    assert session.ends == [("voicelive_connect_failed", "fb")]


async def test_connect_timeout_requests_fallback(monkeypatch, session):
    async def hang(self):
        await asyncio.sleep(10)

    patch_base_connect(monkeypatch, hang)
    handler = ACSMediaHandler(handler_config(), session=session)
    with pytest.raises(TimeoutError):
        await handler.connect_voicelive()
    assert session.ends == [("voicelive_connect_failed", "fb")]


async def test_connect_completing_after_termination_closes_voicelive(monkeypatch, session):
    async def slow_ok(self):
        session.terminated_reason = "caller_hangup"

    patch_base_connect(monkeypatch, slow_ok)
    handler = ACSMediaHandler(handler_config(), session=session)
    await handler.connect_voicelive()
    assert session.closes == 1


@pytest.mark.parametrize(
    "hook, expected",
    [("on_voicelive_ended", ("voicelive_dropped", "fb")), ("on_call_cap", ("call_cap", "bye")), ("on_idle", ("idle", "fb"))],
)
async def test_hooks_route_to_request_end(session, hook, expected):
    handler = ACSMediaHandler(handler_config(), session=session)
    await getattr(handler, hook)()
    assert session.ends == [expected]


async def test_stop_forwarding_sends_acs_stop_audio(session):
    handler = ACSMediaHandler(handler_config(), session=session)
    ws = SimpleNamespace(send=AsyncMock())
    await handler.init_websocket(ws)
    handler.stop_forwarding_agent_audio()
    await asyncio.sleep(0)
    sent = json.loads(ws.send.call_args.args[0])
    assert sent["Kind"] == "StopAudio"
    assert handler._forward_agent_audio is False


async def test_without_session_behaves_like_upstream(monkeypatch):
    calls = []

    async def ok(self):
        calls.append("connected")

    patch_base_connect(monkeypatch, ok)
    handler = ACSMediaHandler(handler_config())
    await handler.connect_voicelive()
    assert calls == ["connected"]
    assert handler.route is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_acs_media_handler.py -q`
Expected: FAIL. `ACSMediaHandler.__init__` doesn't accept `session`.

- [ ] **Step 3: Edit `server/app/providers/acs/media_handler.py`**

Replace the imports block with:
```python
import asyncio
import base64
import json
import logging

from app.handler.voicelive_media_handler import DEFAULT_CHUNK_SIZE, VoiceLiveMediaHandler
```
Directly after the class docstring of `ACSMediaHandler`, insert:
```python
    def __init__(self, config, session=None):
        super().__init__(config, route=session.route if session is not None else None)
        self.session = session
        self._pending_sends: set[asyncio.Task] = set()

    @property
    def log_context(self) -> str:
        return self.session.log_context if self.session is not None else ""

    # ------------------------------------------------------------------
    # Call lifecycle — every end goes through CallSession.request_end()
    # ------------------------------------------------------------------

    async def connect_voicelive(self):
        if self.session is None:
            await super().connect_voicelive()
            return
        try:
            await asyncio.wait_for(super().connect_voicelive(), self.session.settings.voice_live_connect_timeout)
        except Exception:
            logger.exception("voicelive_connect_failed %s", self.log_context)
            self.session.request_end("voicelive_connect_failed", self.session.settings.fallback_message)
            raise
        if self.session.terminated_reason is not None:
            self.session.ensure_voicelive_closed()

    async def on_voicelive_ended(self):
        if self.session is None:
            await super().on_voicelive_ended()
            return
        self.session.request_end("voicelive_dropped", self.session.settings.fallback_message)

    async def on_call_cap(self):
        if self.session is not None:
            self.session.request_end("call_cap", self.session.settings.goodbye_message)

    async def on_idle(self):
        if self.session is not None:
            self.session.request_end("idle", self.session.settings.fallback_message)

    def stop_forwarding_agent_audio(self) -> None:
        super().stop_forwarding_agent_audio()
        if self.client_ws is None:
            return
        stop = json.dumps({"Kind": "StopAudio", "AudioData": None, "StopAudio": {}})
        task = asyncio.get_running_loop().create_task(self.send_message(stop))
        self._pending_sends.add(task)
        task.add_done_callback(self._pending_sends.discard)
```

- [ ] **Step 4: Run the tests**

Run: `python -m uv run pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add server/app/providers/acs/media_handler.py server/tests/test_acs_media_handler.py
git commit -m "feat: bind ACS media handler to call session for fallback, cap and idle ends"
```

---

### Task 10: Callback JWT check and ACS routes

**Files:**
- Create: `server/app/providers/acs/callback_auth.py`
- Modify: `server/app/providers/acs/__init__.py` (replace the body of `register_acs_routes`)
- Test: `server/tests/test_callback_auth.py`, `server/tests/test_acs_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 3–9
- Produces:
  - `CallbackJwtVerifier(audience: str, jwks_client=None, issuer=ACS_CALLBACK_ISSUER)` with `async verify(authorization: str | None) -> bool`
  - Routes: `POST /acs/incomingcall`, `POST /acs/callbacks/<call_key>/<sig>`, `WS /acs/ws/<call_key>/<sig>`
  - `app.config["ACS_CALL_REGISTRY"]`, and a stale-session sweeper started in `before_serving`

**Open check 5:** confirm the ACS callback JWT issuer, JWKS URL and audience against Microsoft's "Secure webhook endpoints" page for Call Automation before setting `ACS_CALLBACK_JWT_AUDIENCE` at deploy time. The values below are the documented ones as we understand them. JWT checking is off unless that variable is set, and the signed callback URL always applies.

- [ ] **Step 1: Write the failing JWT test** `server/tests/test_callback_auth.py`

```python
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.providers.acs.callback_auth import ACS_CALLBACK_ISSUER, CallbackJwtVerifier

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


def token(key=KEY, **claims):
    body = {"aud": "acs-id", "iss": ACS_CALLBACK_ISSUER, "exp": int(time.time()) + 60}
    body.update(claims)
    return jwt.encode(body, key, algorithm="RS256")


@pytest.fixture
def verifier():
    return CallbackJwtVerifier("acs-id", jwks_client=FakeJwks())


async def test_valid_token(verifier):
    assert await verifier.verify(f"Bearer {token()}") is True


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic abc", "Bearer not-a-jwt"],
)
async def test_malformed_headers_rejected(verifier, header):
    assert await verifier.verify(header) is False


@pytest.mark.parametrize(
    "claims", [{"aud": "other"}, {"iss": "https://evil.example"}, {"exp": int(time.time()) - 10}]
)
async def test_bad_claims_rejected(verifier, claims):
    assert await verifier.verify(f"Bearer {token(**claims)}") is False


async def test_wrong_signing_key_rejected(verifier):
    assert await verifier.verify(f"Bearer {token(key=OTHER_KEY)}") is False
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_callback_auth.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement** `server/app/providers/acs/callback_auth.py`

```python
"""Optional verification of the JWT that ACS Call Automation attaches to callback requests."""

import asyncio
import logging

import jwt

logger = logging.getLogger(__name__)

ACS_CALLBACK_ISSUER = "https://acscallautomation.communication.azure.com"
ACS_CALLBACK_JWKS_URL = "https://acscallautomation.communication.azure.com/calling/keys"


class CallbackJwtVerifier:
    def __init__(self, audience: str, jwks_client=None, issuer: str = ACS_CALLBACK_ISSUER):
        self._audience = audience
        self._issuer = issuer
        self._jwks = jwks_client or jwt.PyJWKClient(ACS_CALLBACK_JWKS_URL, cache_keys=True)

    async def verify(self, authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        token = authorization[len("Bearer "):].strip()
        try:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            jwt.decode(token, signing_key.key, algorithms=["RS256"], audience=self._audience, issuer=self._issuer)
            return True
        except jwt.PyJWTError as exc:
            logger.warning("callback JWT rejected: %s", type(exc).__name__)
            return False
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `python -m uv run pytest tests/test_callback_auth.py -q`
Expected: all pass

- [ ] **Step 5: Write the failing routes test** `server/tests/test_acs_routes.py`

```python
import asyncio

import pytest
from azure.communication.callautomation.aio import CallAutomationClient
from quart.testing import WebsocketResponseError

import app.handler.voicelive_media_handler as vmh
from app.providers.acs.call_session import CallSession
from app.providers.acs.signing import sign
from app.routing import AgentRoute
from tests.conftest import TOKEN, acs_env

SILENT_FRAME = '{"kind": "AudioData", "audioData": {"data": "", "silent": true}}'


@pytest.fixture
def acs_server(load_server, monkeypatch, fake_acs):
    monkeypatch.setattr(CallAutomationClient, "from_connection_string", classmethod(lambda cls, cs: fake_acs))
    server = load_server(**acs_env())
    registry = server.app.config["ACS_CALL_REGISTRY"]
    bridge = server.app.config["BRIDGE"]

    def new_session():
        from app.providers.acs.bridge_calls import settings_from_bridge

        session = CallSession(
            call_key="a" * 32, route=AgentRoute("proj", "agent-a", "10"), masked_caller="***9876",
            masked_called="***1234", settings=settings_from_bridge(bridge), acs_client=fake_acs, registry=registry,
        )
        registry.add(session)
        return session

    server.new_session = new_session
    return server


async def test_callback_with_bad_signature_rejected_without_state_change(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{'0' * 64}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
    )
    assert resp.status_code == 403
    assert not session._connected.is_set()


async def test_callback_with_good_signature_dispatches(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}",
        json=[{"type": "Microsoft.Communication.CallConnected", "data": {}}],
    )
    assert resp.status_code == 200
    assert session._connected.is_set()


async def test_media_ws_signature_is_not_a_callback_signature(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    resp = await client.post(
        f"/acs/callbacks/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}", json=[]
    )
    assert resp.status_code == 403


async def _open_ws(client, path):
    async with client.websocket(path) as ws:
        await ws.send(SILENT_FRAME)
        await asyncio.sleep(0.05)


async def test_ws_unknown_key_rejected(acs_server):
    client = acs_server.app.test_client()
    with pytest.raises(WebsocketResponseError) as exc:
        await _open_ws(client, f"/acs/ws/{'b' * 32}/{sign(TOKEN, 'ws', 'b' * 32)}")
    assert exc.value.response.status_code == 403


async def test_ws_bad_signature_rejected(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    with pytest.raises(WebsocketResponseError):
        await _open_ws(client, f"/acs/ws/{session.call_key}/{sign(TOKEN, 'cb', session.call_key)}")
    assert session.ws_used is False


async def test_ws_reused_or_terminated_rejected(acs_server):
    session = acs_server.new_session()
    client = acs_server.app.test_client()
    path = f"/acs/ws/{session.call_key}/{sign(TOKEN, 'ws', session.call_key)}"
    session.ws_used = True
    with pytest.raises(WebsocketResponseError):
        await _open_ws(client, path)
    session.ws_used = False
    session.terminated_reason = "media_timeout"
    with pytest.raises(WebsocketResponseError):
        await _open_ws(client, path)


async def test_ws_valid_signature_attaches_handler_and_never_logs_signature(acs_server, monkeypatch, logs):
    async def idle_connect(self):
        await asyncio.sleep(10)

    monkeypatch.setattr(vmh.VoiceLiveMediaHandler, "connect_voicelive", idle_connect)
    session = acs_server.new_session()
    sig = sign(TOKEN, "ws", session.call_key)
    client = acs_server.app.test_client()
    await _open_ws(client, f"/acs/ws/{session.call_key}/{sig}")
    assert session.ws_used is True
    assert session.handler is not None
    assert sig not in logs.text
    assert TOKEN not in logs.text


async def test_incoming_call_route_answers(acs_server, fake_acs):
    client = acs_server.app.test_client()
    resp = await client.post(
        "/acs/incomingcall",
        json=[{
            "eventType": "Microsoft.Communication.IncomingCall",
            "data": {
                "to": {"kind": "phoneNumber", "phoneNumber": {"value": "+14165551234"}},
                "from": {"kind": "phoneNumber", "phoneNumber": {"value": "+16475559876"}},
                "incomingCallContext": "ctx",
            },
        }],
    )
    assert resp.status_code == 200
    assert fake_acs.answer_kwargs["callback_url"].startswith("https://")
```

- [ ] **Step 6: Run it and confirm it fails**

Run: `python -m uv run pytest tests/test_acs_routes.py -q`
Expected: FAIL. There is no `ACS_CALL_REGISTRY`, and the routes are the upstream ones.

- [ ] **Step 7: Replace `server/app/providers/acs/__init__.py`**

```python
"""ACS (Azure Communication Services) provider route registration."""

import asyncio
import logging

from quart import request, websocket

from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.logging_config import new_correlation_id
from app.provider_registry import register_provider

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60


@register_provider(
    name="acs",
    display_name="Azure Communication Services",
    detect_key="ACS_CONNECTION_STRING",
    required_config=["ACS_CONNECTION_STRING"],
)
def register_acs_routes(app, call_manager: CallManager):
    """Register ACS webhook and WebSocket routes."""
    import os

    from azure.communication.callautomation.aio import CallAutomationClient

    from app.providers.acs.bridge_calls import BridgeCallController
    from app.providers.acs.call_session import CallSessionRegistry
    from app.providers.acs.callback_auth import CallbackJwtVerifier
    from app.providers.acs.media_handler import ACSMediaHandler
    from app.providers.acs.signing import verify

    app.config["ACS_CONNECTION_STRING"] = os.getenv("ACS_CONNECTION_STRING")
    # ACS_DEV_TUNNEL: local dev only — overrides the public base URL for devtunnel/ngrok.
    app.config["ACS_DEV_TUNNEL"] = os.getenv("ACS_DEV_TUNNEL", "")

    bridge = app.config["BRIDGE"]
    registry = CallSessionRegistry()
    app.config["ACS_CALL_REGISTRY"] = registry
    acs_client = CallAutomationClient.from_connection_string(app.config["ACS_CONNECTION_STRING"])
    controller = BridgeCallController(
        acs_client=acs_client, bridge=bridge, registry=registry,
        public_base_url_override=app.config["ACS_DEV_TUNNEL"],
    )
    jwt_verifier = CallbackJwtVerifier(bridge.callback_jwt_audience) if bridge.callback_jwt_audience else None
    if jwt_verifier is None:
        logger.warning("ACS callback JWT check disabled (ACS_CALLBACK_JWT_AUDIENCE unset); signed URLs only")

    @app.route("/acs/incomingcall", methods=["POST"])
    async def incoming_call_handler():
        new_correlation_id()
        events = await request.get_json(silent=True)
        host_url = request.host_url.replace("http://", "https://", 1).rstrip("/")
        body, status = await controller.handle_incoming(events, host_url)
        return body, status

    @app.route("/acs/callbacks/<call_key>/<sig>", methods=["POST"])
    async def acs_event_callbacks(call_key, sig):
        new_correlation_id()
        if not verify(bridge.media_ws_token, "cb", call_key, sig):
            logger.warning("callback_rejected reason=bad_signature call_key=%s", call_key[:8])
            return "", 403
        if jwt_verifier is not None and not await jwt_verifier.verify(request.headers.get("Authorization")):
            logger.warning("callback_rejected reason=bad_jwt call_key=%s", call_key[:8])
            return "", 403
        controller.handle_callbacks(call_key, await request.get_json(silent=True))
        return "", 200

    @app.websocket("/acs/ws/<call_key>/<sig>")
    async def acs_ws(call_key, sig):
        new_correlation_id()
        session = registry.get(call_key)
        reason = None
        if session is None:
            reason = "unknown_call"
        elif not verify(bridge.media_ws_token, "ws", call_key, sig):
            reason = "bad_signature"
        elif session.ws_used:
            reason = "reused"
        elif session.terminated_reason is not None:
            reason = "terminated"
        if reason is not None:
            logger.warning("media_ws_rejected reason=%s call_key=%s", reason, call_key[:8])
            return "", 403

        session.ws_used = True
        if not await call_manager.acquire(call_key, "acs"):
            session.request_end("at_capacity", bridge.fallback_message)
            await websocket.close(4429, "Too Many Connections")
            return

        handler = ACSMediaHandler(app.config, session=session)
        session.handler = handler
        await handler.init_websocket(websocket)
        logger.info("media_ws_accepted %s", session.log_context)
        try:
            await run_call_loop(call_manager=call_manager, call_id=call_key, ws=websocket, handler=handler)
        except asyncio.CancelledError:
            logger.info("media_ws_closed %s", session.log_context)
        except Exception:
            logger.exception("media_ws_error %s", session.log_context)
        finally:
            await call_manager.release(call_key)
            session.on_media_ws_closed()
            session.ensure_voicelive_closed()

    async def _sweep_forever():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            removed = registry.sweep(bridge.max_call_seconds + 120)
            if removed:
                logger.warning("stale_calls_swept count=%d", removed)

    @app.before_serving
    async def _start_sweeper():
        app.config["ACS_SWEEPER"] = asyncio.get_running_loop().create_task(_sweep_forever())

    @app.after_serving
    async def _stop_sweeper():
        task = app.config.get("ACS_SWEEPER")
        if task is not None:
            task.cancel()
```

- [ ] **Step 8: Run the full suite**

Run: `python -m uv run pytest -q`
Expected: all pass. If `quart.testing.WebsocketResponseError` doesn't exist in the installed Quart, find it with `python -m uv run python -c "import quart.testing as t; print(dir(t))"` and adjust the import only.

- [ ] **Step 9: Commit**

```bash
git add server/app/providers/acs/__init__.py server/app/providers/acs/callback_auth.py server/tests/test_callback_auth.py server/tests/test_acs_routes.py
git commit -m "feat: signed ACS callback and media routes with optional callback JWT and stale-call sweep"
```

---

### Task 11: Documentation and config sample

**Files:**
- Modify: `README.md`, `server/.env.sample`

- [ ] **Step 1: Add these entries to `server/.env.sample`, after `ACS_CONNECTION_STRING=...`**

```bash
# --- Bridge (pilot) — see README "Bridge configuration" ---
# Called number -> Foundry agent. Version MUST be a pinned string of digits, never "latest".
AGENT_ROUTING_JSON={"+1XXXXXXXXXX": {"project": "<foundry project>", "agent": "<agent name>", "version": "<n>"}}
MEDIA_WS_TOKEN=<random 32+ chars, from Key Vault>
ACS_COGNITIVE_SERVICES_ENDPOINT=<AI Services endpoint linked to the ACS resource, for spoken fallback/goodbye>
# VOICE_LIVE_ENDPOINT=<overrides AZURE_VOICE_LIVE_ENDPOINT>
# MAX_CALL_SECONDS=600
# FALLBACK_MESSAGE=Sorry, we're having trouble right now. Please call back in a few minutes.
# GOODBYE_MESSAGE=We've reached the time limit for this call. Thank you for calling, goodbye.
# ACS_TTS_VOICE=en-US-JennyNeural
# ACS_CALLBACK_JWT_AUDIENCE=<ACS immutable resource id; enables callback JWT check>
# INTERIM_RESPONSE_JSON=<only if acceptance test 4 fails>
# VOICE_LIVE_API_VERSION=<pin; SDK default is 2026-07-15>
# ENABLE_WEB_CLIENT=false   # local debugging only; never in a deployed environment
```

- [ ] **Step 2: Append a "Bridge implementation notes" section to `README.md`**

````markdown
## Bridge implementation notes

**Upstream:** imported from [Azure-Samples/call-center-voice-agent-accelerator](https://github.com/Azure-Samples/call-center-voice-agent-accelerator) at `<SHA from Task 0 Step 3>`. The accelerator's own README is at [docs/ACCELERATOR_README.md](docs/ACCELERATOR_README.md). To pull Microsoft's fixes:

```bash
git fetch upstream
git merge upstream/main
```

Our changes are mostly in new files (`server/app/bridge_config.py`, `routing.py`, `log_mask.py` and `server/app/providers/acs/{signing,call_session,bridge_calls,callback_auth}.py`). Upstream files only get small hooks. `providers/acs/event_handler.py` is upstream code that we no longer use.

**Voice Live:**
- The bridge uses `azure-ai-voicelive` 1.3.x in **agent mode** (`agent_name`, `project_name`, pinned `agent_version`), with authentication through `DefaultAzureCredential` only.
- The API version is the SDK default, `2026-07-15`, unless `VOICE_LIVE_API_VERSION` is set. Before production, confirm in Microsoft's docs whether agent mode is GA or still preview.
- In agent mode the bridge sends only PCM16 audio formats in `session.update`. It sends no instructions, voice or turn detection; Foundry is the only source of the agent's behavior.
- `interim_response` is sent only if `INTERIM_RESPONSE_JSON` is set, and that is only done if acceptance test 4 fails. **Current state: not set.**

**Environment variable aliases (the spec name wins):**

| Spec | Accelerator fallback |
| --- | --- |
| `MAX_CALL_SECONDS` | `MAX_CALL_DURATION` |
| `VOICE_LIVE_ENDPOINT` | `AZURE_VOICE_LIVE_ENDPOINT` |

**Security:**
- The media WebSocket and callback URLs carry a separate per-call HMAC in the path. Phone numbers and secrets never appear in URLs.
- The ACS callback JWT check is on only when `ACS_CALLBACK_JWT_AUDIENCE` is set. Verify its issuer and JWKS values at deploy time.
- `ENABLE_WEB_CLIENT` exposes an unauthenticated `/web/ws`. Keep it unset in any deployed environment.

**Logs:**
- Phone numbers appear as `***1234`.
- Each call logs `call_ended reason=<...>` once, plus `call_key`, `conn`, the Voice Live `session_id` and `conversation_id`.
- `voicelive_closed_ms` measures cleanup time, for acceptance test 6.

**Tests:**

```bash
cd server
python -m uv sync --extra acs --group dev
python -m uv run pytest -q
```
````

Replace `<SHA from Task 0 Step 3>` with the actual SHA.

- [ ] **Step 3: Run the full suite one last time**

Run: `cd server && python -m uv run pytest -q`
Expected: all pass

- [ ] **Step 4: Grep for forbidden content**

```bash
cd ..
git grep -n -i -E "real.?estate|re-intake|alex|hireastra" -- server/app server/server.py || echo "clean"
git grep -n -E "\+1[0-9]{10}" -- server/app server/server.py || echo "clean"
```
Expected: `clean` twice. Test files may contain `+14165551234`, but `server/app` must not.

- [ ] **Step 5: Commit**

```bash
git add README.md server/.env.sample
git commit -m "docs: document bridge config, agent-mode API version, aliases and security notes"
```

---

## Spec coverage map

| Spec § | Task |
| --- | --- |
| §1 decisions, name aliases | 3, 4 |
| §2 import | 0 |
| §3.1 config and validation | 3 |
| §3.2 routing and masking | 1, 2 |
| §3.3 CallSession lifecycle, trigger table | 7 (core), 8 (answer/callbacks), 9 (hooks), 10 (media WS close → grace) |
| §3.4 bounded close | 7 (`close_voicelive`), 6 (`force_close`) |
| §3.5 media and callback security, JWT | 7 (signing), 8 (URLs), 10 (routes, JWT) |
| §3.6 agent mode, session config, ids, hooks | 5, 6, 9 |
| §3.7 web client guard | 4 |
| §3.8 logging | 1, 6, 7, 8, 10 |
| §3.9 ambient | 11 (`.env.sample` keeps `AMBIENT_PRESET=none`) |
| §5 README | 11 |
| §6 error table | 7, 8, 9 tests |
| §7 tests | every task |
| §8 open checks | 1 and 4 answered while planning (SDK 1.3.0 agent-mode `connect`; force close through the aiohttp response). 2 is handled by sending only PCM16 formats. 3: path segments in `transport_url` are covered by the Task 8 URL test, and need a live check at deploy. 5 is noted in Task 10. |
