# Twilio Pilot — Implementation Plan

Last updated: 2026-09-27 (rev 3 — a focused Opus re-review of rev 2 found 2 remaining blocking issues in the plan text, both fixed inline below; verdict was READY once fixed, no third full review round needed) · Supersedes nothing; new work inserted ahead of M6 for a committed Friday (2026-10-02) live demo · Spec: [TELEPHONY_BRIDGE_SPEC.md](../../TELEPHONY_BRIDGE_SPEC.md) · Decisions: D-037 (to be added alongside this plan) · Live status: [STATUS.md](../../STATUS.md)

**Rev 3 note:** the focused re-review confirmed rev 2's B1-B5 fixes were sound in substance, but found two remaining plan-text errors that would have broken UT01a as written: (1) `/twilio/ws` constructs `TwilioMediaHandler` *before* authentication runs, so a route can't be passed into the constructor — the route must instead be resolved and attached post-construction, after `authenticate_and_start()` succeeds; (2) `load_bridge_config()` runs before provider packages are registered, so `detect_provider()` would return `None` at that point and silently disable the `AGENT_ROUTING_JSON`-required gate — fixed with a direct env-var check that mirrors the real registration-order outcome instead. Both are fixed inline in UT01a below, marked "correction from the focused re-review."

---

## Why this exists

The founder committed to a live demo call with a real real-estate agent on Friday, 2026-10-02. Scope is a single demo call, not ongoing production use. This is a **priority shift, not a scope expansion**: the ACS/M6 deploy track (U10–U13 merged, U14a queued) stays exactly as it is — nothing is discarded, just not this week's critical path.

**Target:** one successful end-to-end test call (Twilio number → bridge → Voice Live → `re-intake-pilot-agent` v10) by Wednesday, 2026-10-01, leaving a buffer day before Friday's demo.

**Why Twilio for this pilot specifically, not ACS:** ACS requires a full Azure infra deploy (M6, still mid-plan) before any call can reach the bridge at all. Twilio only needs the bridge process reachable over HTTPS (a dev tunnel or a minimal deploy) plus a purchased number — no Bicep, no Container App, no cross-subscription role assignments. This is the fast path to a demo, not a replacement for the M6 work.

---

## What already exists (verified by reading the actual files — CLAUDE.md §8)

Every claim below was confirmed by reading the file. **Rev 1 of this plan had inaccurate or incomplete claims about several of these; rev 2's Opus design review caught them — corrections are marked.**

| Component | State | Evidence |
| --- | --- | --- |
| `server/app/providers/twilio/__init__.py` | Unmodified upstream. Registers `/voice` (validates `X-Twilio-Signature`, returns TwiML pointing at the WS) and `/twilio/ws`. | `git diff upstream/main HEAD --stat -- server/app/providers/twilio` is empty. |
| `server/app/providers/twilio/event_handler.py` | Unmodified upstream. `TwilioEventHandler.validate_request()` checks the Twilio signature; `generate_stream_twiml()` builds the TwiML that connects the call to the media stream, with a **60-second HMAC token covering only a timestamp** (not the called number, not the call itself) as a WS auth parameter; `resp.say("Please wait while we connect you to our AI assistant.")` plays **before** the stream connects. **Rev 1's plan omitted this file from its evidence table entirely, even though the route-passing fix and the pre-connect greeting both live here.** | Read in full: lines 21-54. |
| `server/app/providers/twilio/media_handler.py` (`TwilioMediaHandler`) | Unmodified upstream. Extends `VoiceLiveMediaHandler` — **that base class itself is upstream code we modified in U07 (a ~140-line diff), not a class we wrote from scratch, correcting rev 1's "our own" phrasing.** Already has mulaw↔PCM8k↔PCM24k conversion, a WS token auth scheme, barge-in handling. | `git diff upstream/main` on `media_handler.py` is empty. `git diff upstream/main -- server/app/handler/voicelive_media_handler.py` is a real, non-empty diff (U07). |
| Agent routing | **Missing**, confirmed. `TwilioMediaHandler.__init__(self, config)` calls `super().__init__(config)` with no `route` (`media_handler.py:28-29`). `VoiceLiveMediaHandler.connect_voicelive()` only builds the agent-mode payload `if self.route is not None` (`voicelive_media_handler.py:111, 142-161`). Today a Twilio call connects to Voice Live in **non-agent mode** — a D-004 violation. This is the core gap UT01 fixes. | Read both files in full. |
| **The route-passing problem itself (new in rev 2 — rev 1 missed this entirely)** | `/voice` (where the called number is available, in the `To` form field) and `/twilio/ws` (where `TwilioMediaHandler` is actually constructed) are **two separate HTTP requests**. The called number is not available in the WS handler unless it's explicitly carried across. Twilio's Media Streams `start` message only carries `callSid`, `streamSid` and `customParameters` — not `To`/`From`. **Rev 1's plan silently assumed the route could just be "passed into `TwilioMediaHandler(config, route=route)`" without saying how it crosses this request boundary — this would have been a real, undetected gap in the builder's hands.** | `providers/twilio/__init__.py:34-51` (`/voice`) vs. `:53-84` (`/twilio/ws`) are separate route handlers; the WS handler never touches `request.form`. |
| `routing.py` | **Partially provider-agnostic, correcting rev 1's "fully agnostic" claim.** `normalize_number()` and `resolve_route()` are generic and directly reusable. `called_number_from_event()`/`caller_number_from_event()`/`_number_from_identifier()` parse ACS's specific `{"phoneNumber": {"value": ...}}`/`rawId` event shape and are **not** used for Twilio — Twilio's `To`/`From` are plain E.164 strings from a form field, no ACS-shaped parsing needed. | Read `routing.py` in full: lines 28-48 (generic) vs. 51-70 (ACS-event-shaped). |
| `CallSession`/`CallSessionRegistry` (`providers/acs/call_session.py`) | **Not directly reusable**, confirmed. `CallSession.__init__` requires `acs_client` (lines 83-90); hang-up/play-media call `self._acs_client.get_call_connection(...).play_media()`/`.hang_up()` (lines 266, 296) — ACS's Call Automation REST API. | Read in full. |
| `close_voicelive()` (module-level function in the same file) | Reusable in principle, but **importing it pulls in `azure.communication.callautomation`/`azure.core.exceptions` (top-of-file imports, lines 8-9)** — a hard dependency a Twilio-only deploy shouldn't need. Must be moved to a neutral module, not imported directly from the ACS file (see UT01 below). | Read in full; confirmed the top-of-file imports. |
| `bridge_config.py` | `routes`, `fallback_message`, `goodbye_message`, `media_ws_token` are provider-agnostic. `acs_active: bool` (lines 132, 137) gates ACS-only required fields. The same pattern extends to a `twilio_active` gate — **but rev 1 said this gate should "gate nothing new," which the design review correctly flagged as a real gap: without gating `AGENT_ROUTING_JSON` as required when Twilio is active, every call would silently route-miss with no startup error.** Fixed in UT01 below. | Read in full: lines 132-190. |
| `log_mask.py` (`mask_number`) | Fully reusable, provider-agnostic. | Unchanged since U02. |
| `signing.py` (`sign`) | The HMAC primitive is generic, but **not directly reused for Twilio's inbound path** — Twilio's own `X-Twilio-Signature` covers `/voice`, and the WS token (see below) is extended, not replaced. | Read `providers/acs/signing.py`. |
| Provider detection / activation (`provider_registry.py`) | `TWILIO_AUTH_TOKEN` is Twilio's `detect_key` (`providers/twilio/__init__.py:16-21`). **Correction from the design review: if both `ACS_CONNECTION_STRING` and `TWILIO_AUTH_TOKEN` are set, ACS wins** — `provider_registry.py` returns the first registered match, and ACS registers first. `server.py`'s `twilio_active` must be computed as `detect_provider() == "twilio"`, not a bare `bool(TWILIO_AUTH_TOKEN)` check, or the two could disagree. | Read `provider_registry.py`'s match logic and `server.py`'s existing `acs_active` computation. |
| D-028 (web-client/telephony mutual exclusion) | Already provider-agnostic — `server.py`'s guard checks `bridge.enable_web_client and _telephony_client` generically, not ACS specifically. No change needed. | `server.py:89`. |
| `server/.env.sample` | **Real deployment trap the design review caught, not in rev 1:** line 9 has `ACS_CONNECTION_STRING=<…>` **uncommented**. Anyone who copies the sample and adds `TWILIO_AUTH_TOKEN` on top gets ACS detected instead of Twilio (per the point above), and `load_bridge_config` then refuses to start because ACS-only fields are missing — a confusing failure with no obvious cause. UT01 must comment this out. | Read `server/.env.sample` line 9. |
| `twilio` Python package | **Not installed in the dev environment today** — `uv run python -c "import twilio"` fails with `ModuleNotFoundError`. It's an optional extra in `pyproject.toml` (`[project.optional-dependencies] twilio`), and this repo's bootstrap only syncs `--extra acs`. Any test importing `event_handler.py` would fail without first syncing the Twilio extra. | Confirmed via direct import check; `pyproject.toml`'s optional-dependencies table. |

**Bottom line, corrected from rev 1:** the gap is real code work, not config. Beyond agent routing and a lightweight session, this unit also needs: a way to carry the called number across the `/voice` → `/twilio/ws` request boundary, a Voice-Live-drop detection path (Twilio's handler doesn't share ACS's `client_ws` close mechanism), a connect timeout (the base handler has none on its own), and two small dependency/environment fixes (moving `close_voicelive` out of the ACS-only import chain, syncing the `twilio` extra).

---

## Units

Same discipline as the rest of this repo: one unit = one branch = one PR, founder "go" before the builder starts, Opus review (`cso` where there's a security surface) before merge, STATUS.md updated inside the PR.

**Rev 2 splits rev 1's single UT01 into two units, per the design review's scope finding:** UT01a gets a matched-route call reaching the agent in agent mode — the actual thing Wednesday's test depends on. UT01b hardens the call-ending paths (Voice Live drop, connect timeout, idempotent end) against the same race classes U08/U11 already found and fixed for ACS. Under the one-branch rule, UT01a merges and can be live-tested while UT01b is still in review, rather than blocking the first test call on hardening work that matters most for a *sustained* pilot, not a single demo call.

### UT01a — Twilio agent routing: get a matched call into agent mode

**Branch:** `feat/tb-ut01a-twilio-routing` · **Model:** Opus (per D-034).

**Prerequisites:** none blocking — everything consumed (`routing.py`'s generic functions, `bridge_config.py`, `log_mask.py`, `VoiceLiveMediaHandler`) is already merged (M0-M5 complete).

**Files:**
- Modify: `server/app/providers/twilio/event_handler.py` — small hook per D-002 (this file's diff from upstream must stay minimal and is logged in STATUS.md's upstream-divergence tracking, same discipline as D-032's M6 exception):
  - `generate_stream_twiml()` takes the called number and adds it as a second `<Parameter>` on the `<Stream>` element (alongside the existing `token` parameter), so it's delivered in the WS `start` message's `customParameters`.
  - `_generate_ws_token()` binds the called number into the HMAC input (not just the timestamp), so the token can't be replayed against a different called number. Keep the 60s TTL.
- Modify: `server/app/providers/twilio/__init__.py`:
  - `/voice` (POST only — see DoD): read `To` from the form, resolve the route via `resolve_route(bridge.routes, normalize_number(to))` (both from `routing.py`, both confirmed generic above), pass the called number into `generate_stream_twiml(ws_url, called_number)`.
  - On a route hit: TwiML connects to the stream as today (plus the new parameter).
  - On a route miss: return TwiML that speaks `bridge.fallback_message` via `<Say>` and never opens `<Connect><Stream>` at all — simpler than ACS's route-miss path since Twilio's TwiML response model means the WebSocket is never opened for a call we're going to reject anyway. (Answers the open question from rev 1 — see "TTS for route-miss" below; this is now decided, not open.)
  - `/twilio/ws`: **correction from the focused re-review — `/twilio/ws` builds `TwilioMediaHandler` *before* authentication runs, because `authenticate_and_start()` is a method on the handler itself (confirmed: `providers/twilio/__init__.py:59`, handler constructed before `:63`'s auth call). The route cannot be passed into the constructor.** Sequence instead:
    1. `/twilio/ws` constructs `TwilioMediaHandler(config)` (no route yet) as today.
    2. `TwilioMediaHandler.authenticate_and_start()` (modified in this unit) reads the called number from `start.customParameters` (alongside the existing `token` parameter), verifies the HMAC over `timestamp + called_number` (both bound together, closing the original replay concern), and on success stores `self.called_number`.
    3. Back in `/twilio/ws`, *after* `authenticate_and_start()` returns `True`, resolve the route fresh — `handler.route = resolve_route(bridge.routes, handler.called_number)` — matching this repo's pattern of re-deriving trust at each boundary rather than passing a trusted object across a request boundary.
    4. If the route resolves to `None` at this stage (e.g. routing changed between `/voice` and the WS connecting), close the socket rather than falling into non-agent mode — non-agent mode is a D-004 violation, not an acceptable fallback.
    5. This is safe because `route` is only read later, inside `connect_voicelive()` (confirmed: `voicelive_media_handler.py:111, 142`), which `run_call_loop` starts after this point (`call_loop.py:43`) — setting `handler.route` post-construction, before the call loop starts, is not a race.
- Modify: `server/app/handler/voicelive_media_handler.py` is **not** touched by this unit — `route` is already an accepted constructor parameter (confirmed: `voicelive_media_handler.py:74`) and, per the corrected sequencing above, is instead set as a post-construction attribute once resolved (`handler.route = ...`), not passed to `__init__`.
- Modify: `server/app/bridge_config.py` — add `twilio_active: bool` parameter to `load_bridge_config`, mirroring `acs_active`. When `twilio_active` is `True`, `AGENT_ROUTING_JSON` becomes a hard startup requirement (matching how ACS requires it), closing the design review's S1 gap — a misconfigured deploy fails loudly at startup, not silently on every call.
- Modify: `server/server.py` — **correction from the focused re-review:** `load_bridge_config()` runs at `server.py:28`, *before* the provider packages are imported and registered (`server.py:76-82`) — `detect_provider()` would return `None` at that point, silently disabling the `AGENT_ROUTING_JSON`-required gate this unit exists to add. Use `twilio_active = bool(env.get("TWILIO_AUTH_TOKEN")) and not bool(env.get("ACS_CONNECTION_STRING"))` instead — this mirrors the real registration-order outcome ("ACS wins when both are set") without needing to reorder `server.py`'s import/load sequence. **The DoD test for this gate must exercise the actual `server.py` startup path (e.g. via `load_server()`, matching this repo's existing test-harness pattern), not call `load_bridge_config()` directly** — a direct call would pass even if the wiring between `server.py` and `bridge_config.py` were broken.
- Modify: `server/.env.sample` — comment out `ACS_CONNECTION_STRING` (closing the deployment trap the review found), document `TWILIO_AUTH_TOKEN` as the Twilio activation key.
- Modify: `server/pyproject.toml` — no dependency change needed (the `twilio` extra already exists); this unit's own DoD must confirm the test/dev environment syncs `--extra twilio` (see DoD).
- Test: `server/tests/test_twilio_routing.py` — covers: a matched route reaches agent mode (assert the `agent_name`/`project_name`/`agent_version` payload), a route miss never opens the WebSocket and returns `<Say>` TwiML, `/voice` rejects GET, the WS token check rejects a token whose bound called-number doesn't match what's presented, `twilio_active=True` with no `AGENT_ROUTING_JSON` fails startup the same way ACS does.

**DoD:**
- [ ] `/voice` is POST-only (closes the design review's S5 — GET requests today read params as `{}` and would silently miss the `To` lookup, plus GET puts `To`/`From` in the URL, which Quart's access log would then write unmasked, the same exposure class as D-036).
- [ ] A called number with a matching route connects to Voice Live in agent mode — proven by a test asserting the actual `session.update`/connect payload carries `agent_name`/`project_name`/`agent_version`, not just that a route object exists somewhere.
- [ ] A called number with no matching route never opens `/twilio/ws`'s underlying media session; the caller hears the fallback message via TwiML `<Say>` and the call ends there.
- [ ] The WS auth token is bound to the called number, not just a timestamp — a token replayed with a different called number is rejected.
- [ ] `twilio_active=True` with `AGENT_ROUTING_JSON` unset fails startup with a clear error (mirrors ACS's existing behavior).
- [ ] `server.py` computes `twilio_active` via the direct env-var check (`bool(TWILIO_AUTH_TOKEN) and not acs_active`), per the rev 3 correction above — **not** `detect_provider()`, which returns `None` at the point `load_bridge_config()` runs. This mirrors the real registration-order outcome (ACS wins when both are set) without needing to reorder `server.py`'s startup sequence. (Rev 3 fix note: an earlier draft of this DoD line still said `detect_provider()`, left over from before the correction above — this line is now consistent with the rest of the unit's design.)
- [ ] `server/.env.sample`'s `ACS_CONNECTION_STRING` is commented out.
- [ ] Test environment syncs `--extra twilio` (confirm the bootstrap command used for this unit's own test run, and note in the PR whether `server/pyproject.toml`'s dev sync instructions need updating for future sessions).
- [ ] No real phone numbers, agent names, or "real-estate" words in committed code (masked numbers only in logs, matching D-021).
- [ ] No secrets committed — `TWILIO_AUTH_TOKEN` stays an env var only.
- [ ] Full test suite passes (298 today; this unit adds new tests, nothing regresses).
- [ ] Opus code-review + `cso` (this touches inbound webhook auth and a new HMAC binding scheme — real security surface, not docs-only).
- [ ] STATUS.md/DECISIONS.md updated inside this PR, including a small upstream-divergence note for `event_handler.py` (D-002/D-016 discipline, same as D-032's M6 tracking — this is a small hook, not a scoped exception, so no D-032-style entry is needed, just a normal audit-log note of which upstream file changed and why).

### UT01b — Call-ending hardening: Voice Live drop detection, connect timeout, idempotent end

**Branch:** `feat/tb-ut01b-twilio-session` · **Model:** Opus (per D-034 — this is exactly the class of race-condition work D-014 already flagged as needing extra scrutiny for ACS, U08/U11).

**Prerequisite:** UT01a merged (this unit modifies the same `TwilioMediaHandler`/`__init__.py` files UT01a establishes).

**Why this is its own unit, not folded into UT01a:** the design review found that without this hardening, a Voice Live disconnect mid-call would leave the caller in silence for up to `MAX_CALL_SECONDS` (600s default) — Twilio's continuous media stream keeps the idle-timeout watchdog from ever firing, unlike ACS where a dropped media WebSocket triggers a grace-timer path. This matters far more for a *sustained* pilot than for one supervised demo call (where a human on the call would simply redial), so it's sequenced right after UT01a rather than blocking the first test call, but it must land before Friday's actual demo — a silent-hang failure during a live demo in front of a real customer would be far worse than a quick redial during Wednesday's internal test.

**Files:**
- Create: `server/app/handler/voicelive_close.py` — move `close_voicelive()` out of `providers/acs/call_session.py` into this neutral module (closing the design review's B4: the ACS file's top-of-file imports pull in `azure.communication.callautomation`, which a Twilio-only environment shouldn't need to import transitively). `providers/acs/call_session.py` re-exports it (`from app.handler.voicelive_close import close_voicelive`) so no ACS-side caller needs to change.
- Create: `server/app/providers/twilio/call_session.py` — `TwilioCallSession`, a minimal, Twilio-shaped session (**not** a reuse of `providers/acs/call_session.py`'s `CallSession` — confirmed above that class requires an ACS client with no Twilio equivalent). Constructed in `/twilio/ws`, after the route resolves successfully (same place `handler.route` is set in UT01a), and attached to the handler:
  - `request_end(reason, message)` — synchronous, idempotent (first reason wins, matching D-005's guarantee). Records the reason, calls `stop_forwarding_agent_audio()`, schedules the `twilio_ws` close. **Does not itself close Voice Live** — per the design review's B5 fix, ending the call and closing Voice Live are kept as two separate, sequenced steps to avoid reintroducing U11's mid-connect leak race. State plainly in the code comment: *"request_end never touches Voice Live; the only close is `/twilio/ws`'s `finally`."* (per the focused re-review, confirmed correct but worth stating explicitly so a future reader doesn't assume otherwise). `message` is accepted but not spoken for this pilot (matches B2's silent-close decision) — keep the parameter for interface parity with a possible future `<Say>`-based close, don't silently drop it from the signature.
  - Voice Live is closed exactly once, in `/twilio/ws`'s `finally` block, via `close_voicelive(handler, timeout, log_context)` — and only *after* `run_call_loop` has already cancelled and awaited the connect task (confirmed this ordering already exists generically in `call_loop.py:68-75`), so a connect can never finish after cleanup has already run. This sidesteps U11's entire bug class structurally rather than needing U11's shielded-late-close fix, because Twilio's simpler model (no separate REST hang-up call racing a WS close) doesn't create the same race surface.
- Modify: `server/app/providers/twilio/media_handler.py` (`TwilioMediaHandler`):
  - Override `on_voicelive_ended()` to call `session.request_end("voicelive_dropped", bridge.fallback_message)` and close `twilio_ws` — closes the design review's B3 (today's base `on_voicelive_ended` closes `self.client_ws`, which Twilio's handler never sets, since it uses `twilio_ws` instead — so the hook currently does nothing on this path).
  - Wrap `connect_voicelive()` in `asyncio.wait_for(..., timeout=bridge.voice_live_connect_timeout)`, mirroring `ACSMediaHandler`'s existing pattern (`acs/media_handler.py:44`) — the base handler has no timeout of its own.
  - Add `on_call_cap`/`on_idle` overrides matching the ACS pattern proven in U11 (call `request_end` synchronously, never await ACS/Twilio I/O directly inside the hook — Q-008's concern, already resolved the same way for ACS).
- Test: `server/tests/test_twilio_call_session.py` — regression tests for: `request_end` idempotency (first reason wins, a second call is a no-op), a call ending while Voice Live connect is still in flight (mirrors U11's exact regression test, adapted for Twilio's simpler single-owner close), a Voice Live drop mid-call triggers `request_end` and closes the Twilio WS (not just logs), a connect that exceeds `voice_live_connect_timeout` is treated as a failure and ends the call rather than hanging indefinitely.

**DoD:**
- [ ] A Voice Live disconnect mid-call ends the Twilio call within a bounded time (not silent until `MAX_CALL_SECONDS`), proven by a regression test.
- [ ] A Voice Live connect that never completes is bounded by `voice_live_connect_timeout`, not indefinite.
- [ ] `request_end` is idempotent — a regression test proves a second call with a different reason doesn't override the first or double-close anything.
- [ ] A call ending while Voice Live connect is still in flight leaves no orphaned connection (mirrors U11's test, adapted).
- [ ] `close_voicelive` no longer needs an ACS import to use from a Twilio-only code path (verified by confirming `providers/twilio/` doesn't transitively import `azure.communication.callautomation`).
- [ ] `on_call_cap`/`on_idle` never await Twilio/Voice Live I/O directly (Q-008 pattern, matching U11).
- [ ] Full test suite passes.
- [ ] Opus code-review + `cso`.
- [ ] STATUS.md/DECISIONS.md updated inside this PR.

### UT02 — Live end-to-end test call (not a code unit — a verification unit)

**Prerequisite:** UT01a merged (UT01b strongly recommended before Wednesday's test, required before Friday's demo — see UT01b's own rationale above). A real Twilio number purchased and configured (Account SID/Auth Token provided directly by the founder, never pasted in chat, per his own instruction). The bridge reachable over HTTPS.

**Prerequisites the design review caught that rev 1 didn't list:**
- [ ] The Twilio number's voice webhook is configured in the Twilio console as `POST https://<host>/voice`.
- [ ] Whatever identity `az login` is using locally has a Foundry User role on the actual Foundry project the agent runs in — agent mode authenticates via `DefaultAzureCredential` (`voicelive_media_handler.py:143`), so a local test run needs this, not just the deployed Container App's identity.
- [ ] `config_validator.py` (unmodified upstream) still exits unless `AZURE_VOICE_LIVE_API_KEY` or `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID` is set (line 32-38), even though agent mode doesn't use the key — confirm one of these is set for local testing (matches the same C2-class gap the M6 plan found for ACS, D-016 scope, not fixed by editing the upstream file).
- [ ] If testing behind a dev tunnel (e.g. ngrok), confirm the tunnel passes the `Host` header through unchanged — `TwilioEventHandler._reconstruct_url()` forces `https` and derives the host from the incoming request (`event_handler.py:21-24`), and `/voice`'s WS URL is similarly derived from `request.host_url` (`providers/twilio/__init__.py:48-49`). A tunnel that rewrites `Host` would break the signature check or produce a wrong stream URL. ngrok's default behavior passes `Host` through correctly; verify before relying on it if using a different tunnel tool.

**What it does:** place one real call to the Twilio number, confirm it reaches Voice Live in agent mode and `re-intake-pilot-agent` v10 responds. This is the Wednesday target.

**Not in scope for this pilot (explicitly deferred, matching HANDOFF.md's Stage 3/production framing):**
- Twilio callback/webhook signature verification hardening beyond what upstream + UT01a's called-number binding already provide.
- Any Bicep/infra work for Twilio — this pilot runs from wherever the bridge is already reachable (dev tunnel or existing deploy), not a new Azure resource.
- Concurrent call handling beyond what `call_manager` already provides generically.
- DTMF digit logging at DEBUG-not-INFO (design review's nice-to-have — a caller keying in a PIN/account number would land in logs at INFO today; low risk for a one-call supervised demo, worth a follow-up Q-NNN for the eventual production pass, not blocking this pilot).

---

## Product decision needed from the founder (not blocking UT01a's start)

**The demo caller will hear Twilio's own robotic voice say "Please wait while we connect you to our AI assistant" before the agent picks up** (`event_handler.py:49`, unmodified upstream, plays on every call before the stream connects). This wasn't mentioned in rev 1 of this plan. Options: (a) leave it as-is for the pilot (sets expectations that this is an early/rough build, which matches how the demo was already framed to the agent), or (b) remove it (a one-line change to the same small hook already being touched in UT01a for the called-number parameter). **Recommendation: leave it for the pilot** — the demo is explicitly framed as an early/rough build, and a bridging phrase is normal UX for a phone system, not a bug. Revisit if this pilot converts to sustained use. Not blocking UT01a's start either way; can be decided any time before UT02's live call.

---

## TTS for the route-miss fallback — decided, not open (rev 1 left this open; rev 2 closes it)

Rev 1 posed this as an open question. The design review confirmed it's low-risk to decide now: use Twilio's own `<Say>` TwiML verb for the route-miss fallback message. The demo's actual number will be routed (not a miss), so this path doesn't run during the demo itself, and `<Say>`'s TwiML library escapes the fallback message text safely. Matches Twilio's own request/response model better than trying to route a miss through a WebSocket that would otherwise never need to open. Revisit only if this pilot converts to sustained use and route-misses become a real operational concern.

---

## What changed in rev 2

An Opus design review of rev 1 (same discipline as this repo's other design specs, per D-013/D-014) found 5 blocking gaps rev 1 missed entirely, all fixed above:

1. **B1 — the route-passing mechanism itself was missing.** Rev 1 said to pass a resolved route into `TwilioMediaHandler`, but never addressed that `/voice` and `/twilio/ws` are separate HTTP requests with no shared state — the called number has to be explicitly carried across via a TwiML `<Parameter>`, bound into the existing WS auth token. `event_handler.py` (entirely absent from rev 1's evidence table) is now the central file for this fix.
2. **B2 — call-ending "just close the WebSocket" was incomplete and rested on a wrong claim.** Rev 1 claimed Twilio has no call-control REST API (false — it does, via the Calls resource). The actual design decision needed was simpler: what happens to Voice Live's fallback/goodbye message on each end reason. Resolved by not trying to speak a message on every close type for the pilot (silent close via WS close, matching Twilio's `<Connect>` "no further verbs" behavior).
3. **B3 — a Voice Live drop mid-call would leave the caller in silence for up to 10 minutes.** Rev 1 assumed the existing `on_voicelive_ended` hook already handled this; it doesn't, because it closes `self.client_ws`, which Twilio's handler never sets (it uses `twilio_ws`). Fixed in UT01b with an explicit override.
4. **B4 — dependency/import gaps that would break a Twilio-only environment.** The `twilio` package isn't synced by this repo's default bootstrap, and reusing `close_voicelive` directly from the ACS file would transitively import Azure Communication Services packages into a Twilio-only deploy. Fixed by moving the function to a neutral module and calling out the extra-sync requirement in UT01a's DoD.
5. **B5 — no stated ownership for who closes Voice Live, risking reintroducing U11's exact connection-leak race.** Fixed by making `request_end` and the Voice Live close two separate, explicitly sequenced steps, relying on `call_loop.py`'s existing cancel-then-await ordering rather than needing ACS's more complex shielded-late-close fix (Twilio's simpler call model doesn't create the same race surface once ownership is single and sequenced).

Should-fix items also folded in: `twilio_active` now gates `AGENT_ROUTING_JSON` as required (S1); `server.py` computes `twilio_active` via `detect_provider()` rather than a standalone env check so ACS-and-Twilio-both-configured behaves predictably (S1); `server/.env.sample`'s `ACS_CONNECTION_STRING` is commented out to close a real deployment trap (S2); `/voice` is now POST-only (S5); UT02's prerequisites list now includes the Foundry-role, `config_validator.py`, and dev-tunnel `Host`-header items the review surfaced (S6). Several factual corrections to rev 1's evidence table are marked inline (F1-F5) rather than silently fixed, so future sessions can see what was wrong and why.

The design review judged UT01 (rev 1) too large for one PR under Wednesday's time pressure and recommended a split; rev 2 adopts that as UT01a/UT01b.
