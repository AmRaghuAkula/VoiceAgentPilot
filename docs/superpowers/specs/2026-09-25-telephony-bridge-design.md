# Telephony Bridge — Design (Step 3 code changes)

Date: 2026-09-25 · Status: rev 2 (after Opus design review) · Source requirements: [TELEPHONY_BRIDGE_SPEC.md](../../../TELEPHONY_BRIDGE_SPEC.md) §5, [HANDOFF.md](../../../HANDOFF.md)

## 1. Scope

**In scope (this work unit):** import Microsoft's Call Center Voice Agent Accelerator with git history, then make the code changes for spec §5 modifications 1–10. Anything not yet known (the ACS connection string, the test phone number, the endpoints) is read from config and has no real value yet.

**Out of scope (blocked or deferred):**
- Spec §6 (Bicep, Key Vault secrets, Event Grid subscription, `azd up`). This is blocked until the ACS resource and test number exist (HANDOFF §5).
- Everything in spec §7 "Do not build".

**Decisions already made:**
- Phone number provider: Option B, a separate pay-as-you-go subscription for ACS in the same Entra tenant.
- Import method: `upstream` remote plus a merge that keeps history.
- Environment variable names: keep the accelerator's variables. Where spec §6 or §8 names a variable, the bridge **also reads the spec's name, and the spec's name wins**. Spec §6 lists its names as ours, and acceptance test 8 sets `MAX_CALL_SECONDS`. The aliases live in `bridge_config.py`, so upstream files aren't renamed.

| Spec name (wins) | Accelerator name (fallback) |
| --- | --- |
| `MAX_CALL_SECONDS` | `MAX_CALL_DURATION` |
| `VOICE_LIVE_ENDPOINT` | `AZURE_VOICE_LIVE_ENDPOINT` |

## 2. Import

1. `git remote add upstream https://github.com/Azure-Samples/call-center-voice-agent-accelerator.git`
2. `git fetch upstream` then `git merge upstream/main --allow-unrelated-histories`
3. Resolve the merge conflict in `README.md`: keep our pilot README at the top and move the accelerator's README to `docs/ACCELERATOR_README.md`.
4. Record the imported upstream commit SHA in the README.

What the import brings in, used as-is:

| Path | What it is |
| --- | --- |
| `server/server.py` | Quart app, provider detection, `/web/ws`, `/health` |
| `server/app/call_manager.py`, `call_loop.py` | Concurrency, max-duration and idle limits, receive loop |
| `server/app/handler/voicelive_media_handler.py` | Voice Live SDK connection and event loop (base class) |
| `server/app/providers/acs/` | ACS IncomingCall handling, callbacks, media WebSocket, audio conversion |
| `server/app/providers/{twilio,bandwidth,genesys,infobip,sinch}/` | Left in place and unused (spec §7). Inactive unless their credential environment variable is set. |
| `infra/`, `hooks/`, `azure.yaml` | azd/Bicep. Not changed in this work unit. |

## 3. Design

Rule for keeping upstream merges clean: new logic goes into **new files**. Edits to upstream files should be small calls into those new files.

### 3.1 New files

**`server/app/bridge_config.py`** loads and validates the pilot configuration once, at startup.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_ROUTING_JSON` | required when ACS is active | Called number → `{project, agent, version}` |
| `MEDIA_WS_TOKEN` | required when ACS is active, ≥32 chars | Secret used to sign the token for each call (§3.4) |
| `ACS_COGNITIVE_SERVICES_ENDPOINT` | required when ACS is active | Passed to **every** `answer_call` so ACS text-to-speech (goodbye, fallback) works on every call |
| `FALLBACK_MESSAGE` | "Sorry, we're having trouble right now. Please call back in a few minutes." | Failure path and number with no route |
| `GOODBYE_MESSAGE` | "We've reached the time limit for this call. Thank you for calling, goodbye." | Call cap |
| `MAX_CALL_SECONDS` | falls back to `MAX_CALL_DURATION`, then 600 | Passed into the existing `CallManager(max_duration=…)` |
| `MEDIA_CONNECT_TIMEOUT_SECONDS` | 10 | Watchdog: the call is connected but no media WebSocket arrives → fallback |
| `INTERIM_RESPONSE_JSON` | unset | Only used if acceptance test 4 fails (mod 3 exception) |
| `VOICE_LIVE_API_VERSION` | unset (SDK default) | Pinned and recorded in the README |

Validation (hard failure at startup when ACS is active):
- the routing JSON parses;
- every **key** is normalized with `normalize_number()`, and duplicates after normalizing are rejected;
- every entry has `project`, `agent` and `version`;
- `version` is not empty and not `"latest"` (mod 2);
- the token is at least 32 characters;
- the Cognitive Services endpoint is set.

**`server/app/routing.py`**
- `normalize_number(raw) -> str` converts to E.164. It strips everything except digits and a leading `+`. A 10-digit number becomes `+1XXXXXXXXXX`, and an 11-digit number starting with `1` becomes `+1XXXXXXXXXX`. A value that already starts with `+` is kept. Anything else is returned unchanged, so it won't match any route.
- `resolve_route(called_number) -> AgentRoute | None` returns a frozen `AgentRoute(project, agent, version)`.

**`server/app/log_mask.py`**
- `mask_number(value) -> "***1234"` keeps the last 4 digits (mod 9, PIPEDA). It handles `None`/empty input, short strings and ACS `rawId` (`4:+1…`).

**`server/app/providers/acs/call_registry.py`** is the single source of per-call state. It is in-memory, which is fine because the pilot runs max 1 replica.

```
CallState:
  call_key: str                 # bridge-generated uuid4 hex
  route: AgentRoute | None      # None = route miss
  call_connection_id: str       # set from answer_call() return value, synchronously
  masked_caller, masked_called: str
  handler: AcsMediaHandler | None   # attached when the media WS is accepted
  ws_used: bool                 # media token is single-use
  hangup_after_play: bool       # set before any goodbye/fallback play_media
  closing_reason: None | "caller_hangup" | "call_cap" | "failure" | "route_miss"
  created_at: float
Registry: by_key: dict[call_key, CallState]; by_conn: dict[call_connection_id, call_key]
remove(call_key): drops both indexes
```

Entries are added in `process_incoming_call` just after `answer_call()` returns. They are removed after `CallDisconnected` cleanup finishes. A sweep also removes any entry older than `MAX_CALL_SECONDS + 120`, as a safety net against leaks.

### 3.2 Call flows

**Route hit (normal call)**
1. Handle the IncomingCall event. Mask the numbers, then call `resolve_route(to)`, which finds a match.
2. Create a `call_key`. The callback URL is `/acs/callbacks/{call_key}`, with **no caller number in the query**. This removes the upstream `callerId` query parameter, which put the full number into access logs. The media URL is `wss://…/acs/ws/{call_key}/{sig}`, where `sig = HMAC-SHA256(MEDIA_WS_TOKEN, call_key)` as hex (§3.4).
3. `answer_call(..., media_streaming=…, cognitive_services_endpoint=ACS_COGNITIVE_SERVICES_ENDPOINT)`. Save the returned `call_connection_id` in the registry right away.
4. Start the media-connect watchdog task.
5. When the media WebSocket connects: verify the signature, check `ws_used` is false, then set it true. Attach the handler, cancel the watchdog, and call `connect_voicelive(route=state.route)`.

**Route miss**
1. Answer **without** media streaming, but with `cognitive_services_endpoint`. Set `closing_reason="route_miss"` and save the connection id.
2. On `CallConnected`, set `hangup_after_play=True` and `play_media(TextSource(FALLBACK_MESSAGE))`.
3. On `PlayCompleted`/`PlayFailed`, hang up. Log `route_miss` with the masked called number.

**Voice Live failure (connect raises, or an unexpected drop mid-call)**
- The handler calls `on_voicelive_failure()` **only when `closing_reason is None`**. Any intentional close (caller hang-up or call cap) sets `closing_reason` *before* it closes Voice Live, so an intentional close is never treated as a failure.
- The ACS subclass sets `closing_reason="failure"`, stops forwarding agent audio, sets `hangup_after_play=True` and plays `FALLBACK_MESSAGE`. It looks up `call_connection_id` in the registry, which is always present because it's saved at answer time.
- The same path is used when the media-connect watchdog fires.

**Call cap (mod 6)**
- `is_expired()` returns a reason (`"duration"` / `"idle"` / `None`), and existing truthiness checks keep working.
- On `"duration"`, `call_loop` calls `handler.on_call_cap()`. The **base class** does the upstream behavior (close), so `/web/ws` is still capped.
- The ACS subclass:
  1. sets `closing_reason="call_cap"`;
  2. **closes the Voice Live session first**, so no new agent response can play over the goodbye;
  3. sets `hangup_after_play=True` and plays `GOODBYE_MESSAGE`;
  4. hangs up on `PlayCompleted`/`PlayFailed`.
- A 15-second safety timer hangs up even if no play event arrives.

**Caller hang-up (mod 8)**
- On `CallDisconnected`:
  1. set `closing_reason="caller_hangup"`;
  2. `await asyncio.wait_for(handler.cleanup(), 5)`;
  3. log the time from the event to the end of cleanup in ms (acceptance test 6);
  4. remove the registry entry.
- If no handler is attached (route miss, or the WebSocket never came), just remove the entry.

**Hang-up after play**
- The callback handler checks `state.hangup_after_play` on `PlayCompleted`/`PlayFailed` and calls `hang_up(is_for_everyone=True)`.

### 3.3 Voice Live handler changes (`handler/voicelive_media_handler.py`)

| Mod | Change |
| --- | --- |
| 1 | `connect_voicelive(route: AgentRoute \| None = None)`. **With a route: agent mode** (`agent_name`, `project_name`, `agent_version` from the route, following the installed SDK's agent-mode API). **Without a route: upstream model mode.** Only `/web/ws` calls it without a route. The ACS path **always** passes a route and never falls back to model mode; if there's no route, it goes to the route-miss flow. |
| 1 | Credential on the agent path: `DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID") or None)`. This matches the upstream Bicep, which creates a user-assigned identity and sets that variable, and still works with a system-assigned identity or `az login` locally. No API key on the agent path; agent mode only accepts Entra ID. |
| 2 | The version always comes from `AgentRoute.version`. |
| 3 | In agent mode, `_session_config()` sends only the audio-format fields the ACS stream needs (PCM16 in and out). It sends no `instructions`, `voice`, `turn_detection`, noise or echo settings, and adds `interim_response` only when `INTERIM_RESPONSE_JSON` is set. If the SDK or docs show audio format is taken from agent metadata, it sends nothing (see open check 2). Model mode is unchanged. |
| 9 | On `SESSION_CREATED`, store `self.conversation_id` and log it with the call id and `call_key`. This matches a phone call to its Foundry trace. |
| 7 | New `closing_reason` attribute and new hooks `on_voicelive_failure()` and `on_call_cap()`. In the base class, `on_voicelive_failure()` keeps the current behavior (close the client WebSocket) and `on_call_cap()` closes. `cleanup()` sets `closing_reason="cleanup"` if nothing set it already, so the receiver loop's `finally` sees an intentional close. |

### 3.4 Media WebSocket security (mod 5)

- The token is derived per call: `sig = hmac_sha256(MEDIA_WS_TOKEN, call_key).hexdigest()`. The secret itself never goes into a URL. A signature that leaks into access logs is useless because it's tied to one `call_key`, is single-use, and its registry entry is gone after the call.
- The signature and `call_key` go in the **path** (`/acs/ws/{call_key}/{sig}`), not the query string, and they are hex only, so there are no URL-encoding problems.
- The check runs before accept:
  - the `call_key` exists in the registry;
  - `hmac.compare_digest(sig, expected)` passes;
  - `ws_used` is false.
- On any failure, the connection is **not accepted**. Under ASGI, Quart returns an HTTP 403 handshake rejection. We log `media_ws_rejected` with the reason and only a masked `call_key` prefix, never the signature or the secret.
- Any rejection of **ACS's own** media connection leaves the caller without audio. The media-connect watchdog (10 s) turns that into the fallback message and a hang-up, never silence.
- The upstream `/acs/ws` route without a path parameter is replaced. The Event Grid validation handshake on `/acs/incomingcall` already exists upstream and is reused.

### 3.5 Logging (mod 9)

- Every log line that shows caller or called numbers uses `mask_number()`.
- Remove the upstream `logger.info("... data=%s", event.data)` dump of the IncomingCall event and the `caller id` log, because both contain full numbers.
- No phone numbers in any URL (callback or media), so ingress and access logs can't leak them.
- Keep the upstream per-call correlation id. Log `call_key`, `call_connection_id` and the Voice Live `conversation_id` together once they're known.

### 3.6 Ambient (mod 10)

`AMBIENT_PRESET=none` is already the upstream default. It is set explicitly in `.env.sample`, with no code change.

## 4. Identity and deploy-time items (not built now, listed so nothing is lost)

- Upstream Bicep creates a **user-assigned** identity; spec §6 says **system-assigned**. The code works with both (§3.3). At step 4, Raghu decides which one gets **Foundry User** on `hireastra-resource`.
- ACS text-to-speech needs the ACS resource linked to `hireastra-resource` (ACS managed identity with a Cognitive Services role on the AI resource). Deploy-time config.
- Event Grid subscription to `/acs/incomingcall`, filtered to the test number. Key Vault holds `ACS_CONNECTION_STRING` and `MEDIA_WS_TOKEN`.

## 5. README additions

- The imported upstream SHA and how to pull updates from Microsoft's copy.
- The Voice Live API version and SDK version, and whether agent mode is GA or preview.
- The environment variable alias table (§1).
- Whether `interim_response` is set from config (only after acceptance test 4).

## 6. Error handling summary

| Failure | Caller hears | Log |
| --- | --- | --- |
| Called number has no route | Fallback, then hang-up | `route_miss` + masked number |
| Voice Live connect fails (e.g. wrong version, acceptance test 7) | Fallback, then hang-up | Exception + `call_key` |
| Voice Live drops mid-call | Fallback, then hang-up | Drop + `call_key` + `conversation_id` |
| Media WebSocket never arrives or is rejected | Fallback after 10 s, then hang-up | `media_ws_timeout` / `media_ws_rejected` |
| Call reaches `MAX_CALL_SECONDS` | Goodbye, then hang-up (Voice Live already closed) | `call_cap` + duration |
| Caller hangs up | — | Cleanup ms; no fallback attempted |
| TTS playback fails | Hang-up anyway | `PlayFailed` details |
| No play event within 15 s after cap or fallback | Hang-up anyway | `hangup_timeout` |

## 7. Testing

Unit tests (pytest, new `server/tests/`), with no Azure access needed:
- `routing`: normalization of 10-digit, 11-digit `1…`, `+1…`, formatted, `rawId` and garbage input; hit and miss.
- `bridge_config`:
  - routing keys normalized at load and duplicates rejected;
  - `"latest"` rejected;
  - bad JSON rejected;
  - token length checked;
  - spec variable names win over accelerator names.
- `log_mask`: normal, short, `None` and `rawId` input.
- `call_registry`: add, look up by key and by connection id, remove, stale sweep.
- Media WebSocket (Quart test client):
  - a valid signature is accepted;
  - a wrong signature, unknown `call_key` or reused `call_key` is rejected (handshake not accepted);
  - the signature is never logged.
- `is_expired()` reason values; the base `on_call_cap()` closes the web client.
- `closing_reason`: after `cleanup()`, the receiver loop's `finally` does **not** call `on_voicelive_failure()`.
- ACS `process_incoming_call` with a mocked `CallAutomationClient`:
  - a hit answers with streaming plus the Cognitive Services endpoint, and the media URL contains no phone number or secret;
  - a miss answers without streaming;
  - the registry is filled from the `answer_call` return value.
- `agent`-mode `_session_config()` has no `instructions`, `voice` or `turn_detection`.

The live acceptance tests 1–9 (spec §8) run after step 4.

## 8. Open checks during implementation

1. What the installed `azure-ai-voicelive` SDK's agent-mode API looks like, and whether it's GA or preview. Record the answer in the README.
2. Whether agent mode needs, allows or ignores `input_audio_format`/`output_audio_format` in `session.update`.
3. Whether the ACS `MediaStreamingOptions.transport_url` keeps path segments exactly. If it doesn't, fall back to hex query parameters on a route that strips the query from logging.
