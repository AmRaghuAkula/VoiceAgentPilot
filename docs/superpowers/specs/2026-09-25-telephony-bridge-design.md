# Telephony Bridge — Design (Step 3 code changes)

Date: 2026-09-25 · Status: rev 4 (after three Opus design reviews) · Source requirements: [TELEPHONY_BRIDGE_SPEC.md](../../../TELEPHONY_BRIDGE_SPEC.md) §5, [HANDOFF.md](../../../HANDOFF.md)

## 1. Scope

**In scope (this work unit):** import Microsoft's Call Center Voice Agent Accelerator with git history, then make the code changes for spec §5 modifications 1–10. Anything not yet known (the ACS connection string, the test phone number, the endpoints) is read from config and has no real value yet.

**Out of scope (blocked or deferred):**
- Spec §6 (Bicep, Key Vault secrets, Event Grid subscription, `azd up`). This is blocked until the ACS resource and test number exist (HANDOFF §5).
- Everything in spec §7 "Do not build".

**Decisions already made:**
- Phone number provider: Option B, a separate pay-as-you-go subscription for ACS in the same Entra tenant.
- Import method: `upstream` remote plus a merge that keeps history.
- Environment variable names: keep the accelerator's variables. Where spec §6 or §8 names a variable, the bridge also reads the spec's name, **and the spec's name wins**. `bridge_config.py` resolves both names, and `server.py` passes the resolved value into `app.config` and `CallManager`. This is a small edit, and upstream code only ever sees the resolved value.

| Spec name (wins) | Accelerator name (fallback) | Where the resolved value is used |
| --- | --- | --- |
| `MAX_CALL_SECONDS` | `MAX_CALL_DURATION` | `CallManager(max_duration=…)` in `server.py` |
| `VOICE_LIVE_ENDPOINT` | `AZURE_VOICE_LIVE_ENDPOINT` | `app.config["AZURE_VOICE_LIVE_ENDPOINT"]` in `server.py` |

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
| `server/app/providers/{twilio,bandwidth,genesys,infobip,sinch}/` | Left in place and unused (spec §7) |
| `infra/`, `hooks/`, `azure.yaml` | azd/Bicep. Not changed in this work unit. |

## 3. Design

Rule for keeping upstream merges clean: new logic goes into **new files**. Edits to upstream files should be small calls into those new files.

### 3.1 Configuration: `server/app/bridge_config.py` (new)

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_ROUTING_JSON` | required when ACS is active | Called number → `{project, agent, version}` |
| `MEDIA_WS_TOKEN` | required when ACS is active, ≥32 chars | Secret used to sign the media URL for each call (§3.5) |
| `ACS_COGNITIVE_SERVICES_ENDPOINT` | required when ACS is active | Passed to **every** `answer_call` so ACS text-to-speech works on every call |
| `FALLBACK_MESSAGE` | "Sorry, we're having trouble right now. Please call back in a few minutes." | Every failure-type end, and a number with no route |
| `GOODBYE_MESSAGE` | "We've reached the time limit for this call. Thank you for calling, goodbye." | Call cap |
| `MAX_CALL_SECONDS` | falls back to `MAX_CALL_DURATION`, then 600 | Call cap |
| `VOICE_LIVE_ENDPOINT` | falls back to `AZURE_VOICE_LIVE_ENDPOINT` | Voice Live / Foundry resource |
| `MEDIA_CONNECT_TIMEOUT_SECONDS` | 10 | Watchdog for the media WebSocket |
| `ENABLE_WEB_CLIENT` | `false` | Registers `/web/ws` and `/` only when `true` (§3.7) |
| `INTERIM_RESPONSE_JSON` | unset | Only used if acceptance test 4 fails (mod 3 exception) |
| `VOICE_LIVE_API_VERSION` | unset (SDK default) | Pinned and recorded in the README |

Validation (hard failure at startup when ACS is active):
- the routing JSON parses with `object_pairs_hook`, and **raw duplicate keys are rejected** (plain `json.loads` silently keeps the last one);
- every key normalizes to `^\+\d{8,15}$`, or startup fails; this catches typos such as a dropped digit;
- duplicates *after* normalizing are rejected;
- every entry has `project` and `agent` as non-empty strings;
- `version` must be a **string of digits only** (`^\d+$` after stripping). This rejects `"latest"`, `"Latest"`, `" latest "`, empty strings and JSON integers, which enforces mod 2's pinning strictly;
- the token is at least 32 characters;
- the Cognitive Services endpoint is set.

### 3.2 Routing and masking (new files)

**`app/routing.py`**
- `normalize_number(raw) -> str`, in order:
  1. if the value contains `:` (an ACS `rawId` such as `4:+14165551234`), keep only the part after the last `:`;
  2. strip everything except digits and a leading `+`;
  3. a 10-digit number becomes `+1…`, an 11-digit number starting with `1` becomes `+…`, and a value starting with `+` is kept;
  4. anything else is returned unchanged, so it won't match any route.
- The event parser reads `to.phoneNumber.value` first and falls back to `to.rawId`.
- `resolve_route(number) -> AgentRoute | None` returns a frozen `AgentRoute(project, agent, version)`.

**`app/log_mask.py`**
- `mask_number(value) -> "***1234"`. It handles `None`/empty input, short strings and `rawId`.

### 3.3 Per-call lifecycle: `app/providers/acs/call_session.py` (new)

The one idea behind this design: **every way a call can end goes through `CallSession.request_end()`, which acts once and then no-ops.** The session is the only place per-call state lives, including `terminated_reason`; the media handler holds a reference to its session and never keeps its own copy.

```
CallSession:
  call_key            uuid4 hex, created before answer_call
  route               AgentRoute | None
  call_connection_id  set when answer_call returns (may be None briefly)
  masked_caller, masked_called
  handler             AcsMediaHandler | None
  ws_used             bool (media URL is single-use)
  terminated_reason   None until request_end() runs; set once, never changed
  disconnected        bool, set on CallDisconnected
  hangup_after_play   bool
  _timers             set of asyncio tasks (media watchdog, play safety timer, grace timer)
  _connected          asyncio.Event, set on CallConnected
  _answered           asyncio.Event, set when call_connection_id is known

request_end(reason, message) -> None:            # the ONLY entry point; never blocks
  if terminated_reason is not None: return          # first reason wins
  terminated_reason = reason                        # set synchronously, before any await
  create_task(_terminate(message))                  # runs in its own task, never inside a timer or webhook

_terminate(message):                              # never raises; every step is try/except and logged
  cancel all _timers                                # safe: this task is not one of the timers
  if handler:
      handler.stop_forwarding_agent_audio()
      create_task(close_voicelive(handler))         # §3.4, runs ALONGSIDE the play, so no dead air
  if disconnected: return                           # caller already gone, nothing to play or hang up
  if message:
      ok = await wait(_answered, 5 s) and await wait(_connected, 5 s)
      if not ok: await _safe_hang_up(); return      # no connection to play on → just hang up
      hangup_after_play = True
      start 15 s safety timer → _safe_hang_up       # started BEFORE play_media
      try: play_media(TextSource(message))
      except (call gone / 404 / 8522): log; await _safe_hang_up()
  else:
      await _safe_hang_up()

_safe_hang_up():  if not disconnected and call_connection_id: hang_up(is_for_everyone=True), swallowing 404/8522

on_call_disconnected():                           # the CallDisconnected callback
  disconnected = True
  request_end("caller_hangup", None)                # no-op if another reason already won
  cancel all _timers, including the play safety timer
  (background) await close_voicelive if still open, then registry.remove(self)   # ALWAYS removed here

on_play_done():                                   # PlayCompleted / PlayFailed
  if hangup_after_play: cancel the play safety timer; await _safe_hang_up()
```

`CallSessionRegistry`: `by_key`, `by_conn`.
- Entries are **added before `answer_call()`** and indexed by connection id once `answer_call` returns. Callbacks and the media WebSocket are keyed by `call_key` in the URL path, so they always find the session even if they arrive before `answer_call` returns.
- **Removal always happens in `on_call_disconnected`**, whichever reason ended the call.
- The sweep is only a safety net for a lost `CallDisconnected`: `request_end("stale", None)`, then remove entries older than `MAX_CALL_SECONDS + 120`.

**Webhook handlers never await the end of a call.** Each ACS callback handler updates the session flags (`_connected.set()` comes *first* on CallConnected), calls `request_end(...)` or `on_*` as needed, and returns 200 right away. That way ACS never times out and retries.

**Every timer checks its own condition when it fires** and does nothing if the session is terminated or removed.

**Every end path goes through `request_end`:**

| Trigger | `request_end(reason, message)` |
| --- | --- |
| `CallDisconnected` callback | via `on_call_disconnected()` → `("caller_hangup", None)` |
| Call cap (`is_expired` → `"duration"`) | `("call_cap", GOODBYE_MESSAGE)` |
| Idle expiry (`is_expired` → `"idle"`) | `("idle", FALLBACK_MESSAGE)` |
| Voice Live connect raises | `("voicelive_connect_failed", FALLBACK_MESSAGE)` |
| Voice Live receiver ends while the session isn't terminated | `("voicelive_dropped", FALLBACK_MESSAGE)` |
| ACS media WebSocket closes while the session isn't terminated | start a **2 s grace timer**; if `CallDisconnected` hasn't arrived by then → `("media_lost", FALLBACK_MESSAGE)`. This avoids trying to play into a call the caller just hung up. |
| Media watchdog: armed when `answer_call` returns (route hit), fires after `MEDIA_CONNECT_TIMEOUT_SECONDS`, **does nothing if `ws_used` is already true**. The media WebSocket can connect before `CallConnected`, so the watchdog is keyed to "has media arrived", not to event order. | `("media_timeout", FALLBACK_MESSAGE)` |
| Route miss, on `CallConnected` | `("route_miss", FALLBACK_MESSAGE)` |
| Stale sweep | `("stale", None)` |
| `PlayCompleted` / `PlayFailed` | `on_play_done()` (the session is already terminated) |

### 3.4 Closing Voice Live, bounded (mod 8)

- `close_voicelive(handler)` runs `await asyncio.wait_for(asyncio.shield(handler.cleanup()), 5)`.
- On timeout it force-aborts the SDK connection's underlying WebSocket, so the session is not billed, logs `voicelive_force_closed`, and lets the shielded cleanup finish in the background.
- It always logs the time from the triggering event to close, in ms (acceptance test 6).

### 3.5 Media WebSocket security (mod 5)

- The media URL is `wss://<host>/acs/ws/{call_key}/{sig_ws}`, where `sig_ws = HMAC-SHA256(MEDIA_WS_TOKEN, "ws:" + call_key)` as hex. The secret itself never goes into a URL. A signature that leaks into access logs is useless: it's tied to one `call_key`, single-use, and dead once the call ends. Everything is hex, so there are no URL-encoding problems.
- The callback URL is `https://<host>/acs/callbacks/{call_key}/{sig_cb}`, where `sig_cb = HMAC-SHA256(MEDIA_WS_TOKEN, "cb:" + call_key)`. The two derivations are separate, so a leaked media URL can't be used to forge callbacks. Callbacks with a missing or wrong `sig_cb` get a 403 and have no effect.
- On top of that, callbacks validate the **ACS-signed JWT** in the `Authorization` header: the signature against the ACS JWKS, and the audience set to the ACS resource. That way even someone who can read the ingress logs can't forge `CallDisconnected` or `PlayCompleted`. If the JWT details can't be confirmed during implementation (open check 5), the HMAC path alone is the pilot control, and that is recorded in the README as a production item.
- There is **no caller number in any URL**; this removes the upstream `callerId` query parameter.
- The check runs before accept:
  - the session exists;
  - `hmac.compare_digest` passes;
  - `ws_used` is false;
  - **`terminated_reason is None`**.
- If any check fails, the connection is not accepted. Quart/ASGI returns an HTTP 403 handshake rejection, and we log `media_ws_rejected` with the reason and a short `call_key` prefix only.
- A rejected ACS media connection is recovered by the media watchdog (fallback message, then hang-up), never silence.
- The Event Grid validation handshake on `/acs/incomingcall` is reused from upstream.

### 3.6 Voice Live handler changes (`handler/voicelive_media_handler.py`)

| Mod | Change |
| --- | --- |
| 1 | `connect_voicelive(route: AgentRoute \| None = None)`. **With a route: agent mode** (`agent_name`, `project_name`, `agent_version`, following the installed SDK's agent-mode API). **Without a route: upstream model mode**, used only by the optional web client. The ACS path always passes a route; a missing route never reaches `connect_voicelive` (it's a route miss). |
| 1 | Credential on the agent path: `DefaultAzureCredential(managed_identity_client_id=os.getenv("AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID") or None)`. This matches the upstream Bicep's user-assigned identity and still works with a system-assigned identity or `az login`. No API key; agent mode only accepts Entra ID. |
| 2 | The version always comes from `AgentRoute.version`. |
| 3 | In agent mode, `_session_config()` sends only the PCM16 audio-format fields the stream needs, plus `interim_response` if `INTERIM_RESPONSE_JSON` is set. No `instructions`, `voice`, `turn_detection`, noise or echo settings. If audio format is taken from agent metadata, send nothing (open check 2). Model mode is unchanged. |
| 9 | On `SESSION_CREATED`, store `conversation_id` and log it with `call_key` and `call_connection_id`. |
| 7 | New hooks with defaults that keep upstream behavior: `on_voicelive_ended()` (base: close the client WebSocket, as upstream does) and `on_call_cap()` / `on_idle()` (base: close). The ACS subclass overrides them to call `session.terminate(...)`. New `stop_forwarding_agent_audio()` sets a flag that `on_audio_delta` checks. |

`call_loop.py`: `is_expired()` returns `"duration"` / `"idle"` / `None`, and existing truthiness checks keep working. The loop calls `handler.on_call_cap()` or `handler.on_idle()` accordingly. When the loop exits because the client (ACS media) WebSocket closed, it calls `handler.on_client_ws_closed()`. Base: no-op. ACS: start the 2 s grace timer.

### 3.7 Web debug client (`/web/ws`)

Spec §7 lets the browser client stay for debugging, but it is unauthenticated and would sit on external ingress. It is **off unless `ENABLE_WEB_CLIENT=true`**, which is a one-line guard in `server.py` around the `/web/ws` and `/` routes. It is meant for local use only and is never set in the deployed pilot.

### 3.8 Logging (mod 9)

- Every log line with a number goes through `mask_number()`.
- Remove the upstream full `event.data` dump and the `caller id` log.
- No phone numbers or secrets in any URL.
- Log `call_key`, `call_connection_id` and `conversation_id` together once they're known, and `terminated_reason` exactly once per call.

### 3.9 Ambient (mod 10)

`AMBIENT_PRESET=none` is the upstream default. It is set explicitly in `.env.sample`.

## 4. Deploy-time items (not built now, listed so nothing is lost)

- Identity: the upstream Bicep creates a **user-assigned** identity; spec §6 says **system-assigned**. The code works with both. At step 4, Raghu decides which one gets **Foundry User** on `hireastra-resource`.
- The ACS resource must be linked to `hireastra-resource` for text-to-speech (ACS managed identity with a Cognitive Services role).
- Event Grid subscription to `/acs/incomingcall`, filtered to the test number. Key Vault holds `ACS_CONNECTION_STRING` and `MEDIA_WS_TOKEN`.
- `ENABLE_WEB_CLIENT` is not set in the deployed environment.

## 5. README additions

The imported upstream SHA and how to pull updates; the Voice Live API and SDK versions and whether agent mode is GA or preview; the environment variable alias table; whether `interim_response` is set from config; the `ENABLE_WEB_CLIENT` warning.

## 6. Error handling summary

| Failure | Caller hears | Log (`terminated_reason`) |
| --- | --- | --- |
| No route | Fallback, then hang-up | `route_miss` |
| Voice Live connect fails (acceptance test 7) | Fallback, then hang-up | `voicelive_connect_failed` |
| Voice Live drops mid-call | Fallback, then hang-up | `voicelive_dropped` |
| Media WebSocket never arrives or is rejected | Fallback after ≤10 s, then hang-up | `media_timeout` |
| ACS media WebSocket drops mid-call | Fallback, then hang-up (after a 2 s grace period) | `media_lost` |
| No audio for the idle timeout | Fallback, then hang-up | `idle` |
| `MAX_CALL_SECONDS` reached | Goodbye, then hang-up (Voice Live already closed) | `call_cap` |
| Caller hangs up | — | `caller_hangup` + Voice Live close ms |
| TTS fails, or no play event within 15 s | Hang-up | `PlayFailed` / `hangup_timeout` |
| Voice Live close takes >5 s | — | `voicelive_force_closed` |

## 7. Testing

Unit tests (pytest, `server/tests/`), no Azure access needed:
- `routing`: 10-digit, 11-digit `1…`, `+1…`, formatted and `rawId` (`4:+1…`) input normalize to the same key; garbage input doesn't match.
- `bridge_config`:
  - keys normalized;
  - raw and normalized duplicates rejected;
  - a key that doesn't normalize to E.164 (e.g. 9 digits) rejected;
  - `version` values `"latest"`, `"Latest"`, `" latest "`, `""` and `10` (an int) rejected, `"10"` accepted;
  - bad JSON rejected;
  - token length checked;
  - spec variable names win.
- `log_mask`: normal, short, `None` and `rawId` input.
- `CallSession`:
  - `request_end` is idempotent (the first reason is kept) and returns without blocking;
  - a timer that triggers `request_end` still completes the play and hang-up (it doesn't cancel itself);
  - `caller_hangup` does not play;
  - a message path plays and then hangs up;
  - the safety timer hangs up when no play event arrives;
  - `PlayCompleted` cancels the safety timer (no second hang-up);
  - the registry entry is removed on `CallDisconnected` for **every** end reason;
  - `play_media` raising "call gone" leads to a hang-up attempt, and nothing escapes;
  - an `_answered` timeout leads to a plain hang-up;
  - goodbye `play_media` starts without waiting for `close_voicelive`.
- Races:
  - a callback arriving before `answer_call` returns still finds the session;
  - `CallDisconnected` during the grace window means no fallback;
  - a WebSocket arriving after `request_end` is rejected;
  - a timer firing after removal is a no-op;
  - a media WebSocket arriving **before** `CallConnected` means the watchdog never fires;
  - webhook handlers return before `_terminate` finishes.
- Callbacks: a missing or wrong `sig_cb` gets a 403 and changes no state.
- Media WebSocket:
  - a valid signature is accepted;
  - a wrong signature, unknown key, reused key or terminated session is rejected;
  - the signature is never logged.
- `close_voicelive` with a cleanup that hangs force-closes within about 5 s.
- `is_expired()` reason values.
- `ENABLE_WEB_CLIENT=false` → `/web/ws` returns 404.
- ACS `process_incoming_call` with a mocked `CallAutomationClient`:
  - a hit answers with streaming plus the Cognitive Services endpoint, and the URLs contain no number or secret;
  - a miss answers without streaming;
  - the session is inserted before `answer_call`.
- Agent-mode `_session_config()` has no `instructions`, `voice` or `turn_detection`.

The live acceptance tests 1–9 (spec §8) run after step 4.

## 8. Open checks during implementation

1. What the installed `azure-ai-voicelive` SDK's agent-mode API looks like, and whether it's GA or preview. Record the answer in the README.
2. Whether agent mode needs, allows or ignores `input_audio_format`/`output_audio_format` in `session.update`.
3. Whether ACS `MediaStreamingOptions.transport_url` keeps path segments exactly.
4. How to force-abort the SDK connection's underlying WebSocket (for §3.4).
5. The ACS Call Automation callback JWT: the header, JWKS URL and expected audience (for §3.5).
