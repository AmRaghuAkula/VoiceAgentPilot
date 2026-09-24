# Telephony Bridge — Design (Step 3 code changes)

Date: 2026-09-25 · Status: draft for review · Source requirements: [TELEPHONY_BRIDGE_SPEC.md](../../../TELEPHONY_BRIDGE_SPEC.md) §5, [HANDOFF.md](../../../HANDOFF.md)

## 1. Scope

**In scope (this work unit):** import Microsoft's Call Center Voice Agent Accelerator with git history, then make the code changes for spec §5 modifications 1–10. Anything not yet known (the ACS connection string, the test phone number, the Voice Live endpoint) is read from config and has no real value yet.

**Out of scope (blocked or deferred):**
- Anything under spec §6 (Bicep changes, Key Vault secrets, Event Grid subscription, `azd up`). This is blocked because the ACS resource and test number don't exist yet (HANDOFF §5).
- Everything in spec §7 "Do not build".

**Decisions already made:**
- Phone number provider: Option B, a separate pay-as-you-go subscription for ACS in the same Entra tenant.
- Import method: `upstream` remote plus a merge that keeps history, so later `git pull upstream main` stays clean.
- Environment variable names: keep the accelerator's names wherever one already exists (e.g. `MAX_CALL_DURATION`, not spec's `MAX_CALL_SECONDS`). The mapping is recorded in the README (§5 below).

## 2. Import

1. `git remote add upstream https://github.com/Azure-Samples/call-center-voice-agent-accelerator.git`
2. `git fetch upstream` then `git merge upstream/main --allow-unrelated-histories`
3. Resolve the merge conflict in `README.md`: keep our pilot README at the top and link to the accelerator's README (move theirs to `docs/ACCELERATOR_README.md`).
4. Record in the README which upstream commit SHA was imported.

What the import brings in, used as-is:

| Path | What it is |
| --- | --- |
| `server/server.py` | Quart app, provider detection, `/web/ws`, `/health` |
| `server/app/call_manager.py`, `call_loop.py` | Concurrency, max-duration and idle limits, receive loop |
| `server/app/handler/voicelive_media_handler.py` | Voice Live SDK connection and event loop (base class) |
| `server/app/providers/acs/` | ACS IncomingCall handling, callbacks, media WebSocket, audio conversion |
| `server/app/providers/{twilio,bandwidth,genesys,infobip,sinch}/` | Left in place and unused (spec §7). Inactive unless their credential environment variable is set. |
| `infra/`, `hooks/`, `azure.yaml` | azd/Bicep deploy. Not changed in this work unit. |

## 3. Code changes (modifications 1–10)

Rule for keeping upstream merges clean: new logic goes into **new files** (`app/routing.py`, `app/bridge_config.py`, `app/log_mask.py`). Edits to upstream files should be small calls into those new files.

### 3.1 New files

**`server/app/bridge_config.py`** loads and validates the pilot-specific configuration once, at startup:

| Variable | Type / default | Purpose |
| --- | --- | --- |
| `AGENT_ROUTING_JSON` | JSON object, required when ACS is active | Called number → `{project, agent, version}` |
| `FALLBACK_MESSAGE` | str, default `"Sorry, we're having trouble right now. Please call back in a few minutes."` | Played on failure or when the called number has no routing entry |
| `GOODBYE_MESSAGE` | str, default `"We've reached the time limit for this call. Thank you for calling, goodbye."` | Played at the call cap |
| `MEDIA_WS_TOKEN` | str, required when ACS is active, ≥32 chars | Media WebSocket auth |
| `INTERIM_RESPONSE_JSON` | JSON, optional, unset by default | Only used if acceptance test 4 fails (mod 3 exception) |
| `VOICE_LIVE_API_VERSION` | str, optional | Pins the API version and is recorded in the README |

Validation (hard failure at startup when the ACS provider is active):
- the routing JSON parses;
- every entry has `project`, `agent` and `version`;
- `version` is a non-empty string and not `"latest"` (mod 2);
- the token is at least 32 characters.

**`server/app/routing.py`**
- `normalize_number(raw: str) -> str` converts a number to E.164. It strips spaces, dashes and parentheses. A bare 10-digit number gets `+1` added.
- `resolve_route(called_number) -> AgentRoute | None` returns a frozen dataclass `AgentRoute(project, agent, version)`.

**`server/app/log_mask.py`**
- `mask_number(n) -> "***1234"` keeps the last 4 digits only (mod 9, PIPEDA). It handles `None`/empty input and ACS `rawId` strings.

### 3.2 Modified upstream files

| # | File | Change |
| --- | --- | --- |
| 1 | `handler/voicelive_media_handler.py` → `connect_voicelive()` | If a route was supplied, connect in **agent mode** with `agent_name`, `project_name` and `agent_version` from the route, per the Voice Live agents quickstart. Otherwise fall back to the current model mode, so the `/web/ws` debug client keeps working. First check what connect parameters the installed `azure-ai-voicelive` version supports for agent mode, and pin that SDK version in `pyproject.toml`. Agent mode only accepts Entra ID, so no API key is used on the agent path. |
| 1 | same → credential | Agent mode uses `DefaultAzureCredential` (see §4, Identity). |
| 2 | same | The version always comes from `AgentRoute.version`, never a default. |
| 3 | same → `_session_config()` | In agent mode, send a `session.update` that contains **only** audio format fields (`input_audio_format`, `output_audio_format` PCM16), because the ACS media stream needs them. Send no `instructions`, `voice`, `turn_detection`, noise or echo settings. If `INTERIM_RESPONSE_JSON` is set, add only `interim_response`. Model mode keeps the upstream config unchanged. **Open check:** confirm against the SDK/docs whether audio format is allowed and required in agent mode. If the agent's own metadata already sets PCM16 at 24 kHz, send nothing at all. |
| 1, 9 | same → `_receiver_loop()` | On `SESSION_CREATED`, store `self.conversation_id` and log it with the call id. This lets a phone call be matched to its Foundry trace. |
| 7 | same → `_receiver_loop()` `finally` | Before closing the client WebSocket after an **unexpected** drop, call a new hook `on_voicelive_failure()`. The base class does nothing; the ACS subclass plays the fallback message. `connect_voicelive()` exceptions go through the same hook. |
| 4 | `providers/acs/event_handler.py` → `process_incoming_call` | Read `event.data["to"]["phoneNumber"]["value"]` and call `resolve_route()`. **Match:** answer with media streaming and put a `route` key (the routing entry's number, not the agent details) in the callback and WebSocket URL query. **No match:** answer *without* media streaming, remember the call connection id as "fallback-only", and on `CallConnected` play `FALLBACK_MESSAGE` through `play_media` with `TextSource`, then hang up when `PlayCompleted`/`PlayFailed` arrives. Log it with the masked number. |
| 5 | same | Add `token=<MEDIA_WS_TOKEN>` to the query string of the `wss://…/acs/ws` transport URL. |
| 5 | `providers/acs/__init__.py` → `/acs/ws` route | Before accepting, compare `websocket.args["token"]` to the configured token using `hmac.compare_digest`. On mismatch, close with code 4401 and log it; never log the token. The Event Grid validation handshake already exists upstream and is reused. |
| 9 | `providers/acs/event_handler.py` | Every log line that prints caller or called numbers uses `mask_number()`. Remove the full `event.data` dump from the "Incoming call received" log, because it contains the full numbers. |
| 6 | `call_manager.py` / `call_loop.py` | Reuse the existing `max_duration` (`MAX_CALL_DURATION`, pilot value 600). When `is_expired()` is true because of **max duration**, `call_loop` calls a new handler hook `on_call_cap()` instead of just closing. The ACS subclass cancels the Voice Live response, plays `GOODBYE_MESSAGE` through ACS text-to-speech, then hangs up. Idle expiry keeps the upstream behavior. `is_expired()` is extended to return a reason (`"duration"` / `"idle"` / `None`); the existing truthiness is kept. |
| 8 | `providers/acs/event_handler.py` → `CallDisconnected` | Find the active media handler for this call through a small registry in the ACS provider (a dict from call connection id to handler, filled when the WebSocket connects) and call `handler.cleanup()` with `asyncio.wait_for(…, 5)`. Log the time between the disconnect event and the end of cleanup (acceptance test 6). |
| 10 | `.env.sample` | `AMBIENT_PRESET=none`. This is already the upstream default, so no code change is needed. |

### 3.3 Linking the ACS callback, the WebSocket and the handler

The ACS media WebSocket does not carry the call connection id by default. Plan:
- Pass `route` and a bridge-generated `call_key` (a UUID) as query parameters on both the callback URL and the media transport URL.
- The WebSocket handler registers `call_key → handler`.
- The callback handler maps `call_connection_id → call_key` when `CallConnected` arrives.
- The ACS side (`CallDisconnected`, playing TTS on cap or failure) looks the handler up through these maps.

All of this lives inside `providers/acs/`. The maps are in-memory, which is fine because the pilot runs max 1 replica (spec §6).

## 4. Identity

The upstream code uses `ManagedIdentityCredential(client_id=AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID)`, and its Bicep creates a **user-assigned** identity. Spec §6 says **system-assigned**. Decisions for this work unit:
- Code uses `DefaultAzureCredential`. It picks up `AZURE_CLIENT_ID` when set (user-assigned), falls back to system-assigned, and uses `az login` locally.
- Bicep is not touched now. At deploy time (step 4) we decide: keep the user-assigned identity and grant it Foundry User on `hireastra-resource`, or switch to system-assigned as the spec says. This will be raised with Raghu then; it doesn't block the code.

## 5. README additions

- The imported upstream commit SHA and how to pull updates from Microsoft's copy.
- The Voice Live API version and SDK version in use, and whether agent mode is GA or preview (spec mod 1 and the risk table).
- Environment variable mapping: spec `MAX_CALL_SECONDS` → `MAX_CALL_DURATION`; spec `VOICE_LIVE_ENDPOINT` → `AZURE_VOICE_LIVE_ENDPOINT`.
- Whether `interim_response` is set from config (only after acceptance test 4).

## 6. Error handling summary

| Failure | Caller experience | Log |
| --- | --- | --- |
| Called number has no routing entry | Fallback message, hang-up | `route_miss` + masked number |
| Voice Live connect fails (e.g. wrong agent version, acceptance test 7) | Fallback message, hang-up | Exception + call id |
| Voice Live drops mid-call | Fallback message, hang-up | Drop + call id + conversation id |
| Media WebSocket bad or missing token | (the WebSocket is not ACS's; no caller impact) | Reject 4401 |
| Call reaches `MAX_CALL_DURATION` | Goodbye message, hang-up | `call_cap` + duration |
| Caller hangs up | — | Cleanup duration in ms |
| TTS playback itself fails | Hang-up anyway | `PlayFailed` details |

## 7. Testing

Unit tests (pytest, new `server/tests/`), with no Azure access needed:
- `routing`: normalization variants, hit, miss, rejection of `"latest"`, bad JSON.
- `log_mask`: normal numbers, short strings, `None`, `rawId` input.
- `bridge_config`: required keys when ACS is active, token length.
- WebSocket token check: correct, wrong and missing token (Quart test client).
- `call_manager.is_expired` reason values.
- `_session_config()` in agent mode contains no `instructions`/`voice`/`turn_detection`.
- ACS `process_incoming_call` with a mocked `CallAutomationClient`: the route hit answers with media streaming, and the transport URL contains the token. The route miss answers without streaming.

Things that can't be tested until deploy are the live acceptance tests 1–9 in spec §8. They will run after step 4.

## 8. Open checks before or while implementing

1. What the installed `azure-ai-voicelive` SDK's agent-mode connect signature looks like, and whether it is GA or preview. Record the answer in the README.
2. Whether agent mode accepts or requires `input_audio_format`/`output_audio_format` in `session.update`.
3. Whether ACS `answer_call` without media streaming, followed by `play_media` with `TextSource`, needs a Cognitive Services link on the ACS resource (`cognitive_services_endpoint` in `answer_call`). If it does, that's a deploy-time config item, `ACS_COGNITIVE_SERVICES_ENDPOINT` pointing at `hireastra-resource`.
