# UT02 — Live End-to-End Test Call: Spec

Last updated: 2026-09-27 · Not a code unit — a verification unit, per the Twilio pilot plan · Spec: [`2026-09-27-twilio-pilot-plan.md`](2026-09-27-twilio-pilot-plan.md) (UT02's original placeholder) · Decisions: D-037 · Prerequisites: UT01a (PR #24), UT01b (PR #25), both merged, `main` @ 9a44aa6, 348/348 tests passing

**Input:** drafted from a Claude Desktop session's spec proposal, reviewed against the actual code before being finalized here (two factual corrections made — see "Corrections from the draft" below). This is not delegated planning — every claim below was checked against the current repo state per CLAUDE.md §8, not carried over from the draft unverified.

---

## Goal

One successful, observed, end-to-end call: caller dials **+1 (226) 741-3885** → Twilio → local bridge (via tunnel) → Voice Live → `re-intake-pilot-agent` v10 → audio flows both directions → call ends cleanly.

**Timeline:** target first successful call **Wednesday 2026-10-01**, a second successful call **Thursday 2026-10-02** (confirms the first wasn't luck), live demo **Friday 2026-10-03**. If the founder's own dependency (credentials + tunnel access) slips past today or tomorrow, that risk is flagged back immediately — not absorbed silently.

---

## Corrections from the draft (verified against actual code, not assumed)

1. **Webhook path is `/voice`, not `/twilio/voice`.** Confirmed: `server/app/providers/twilio/__init__.py:48`, `@app.route("/voice", methods=["POST"])`. The Twilio console's webhook URL must be `<tunnel-url>/voice`.
2. **`voice_live_connect_timeout` defaults to 8 seconds, not 5.** Confirmed: `server/app/bridge_config.py:198`, `_positive(env, "VOICE_LIVE_CONNECT_TIMEOUT_SECONDS", 8.0, ...)`. This is the actual value UT01b's `asyncio.wait_for` wraps around. The "connects within N seconds" acceptance criterion is set to **10 seconds** below (8s code timeout + headroom for real Twilio/network latency on top of it), not 5s — 5s would fail even fully correct, expected behavior.
3. **Local bridge listens on port 8000.** Confirmed: `server/server.py:183`, `app.run(..., port=8000)`.
4. **The "hang up within 2 seconds" edge case is already unit-tested**, not untested ground. `server/tests/test_twilio_call_session.py::test_caller_hangup_mid_connect_leaves_no_orphaned_connection` exercises exactly this scenario against a fake SDK. UT02's version of this check verifies the same logic holds under *real* Twilio timing and network behavior — it is a real-world confirmation of already-proven logic, not a first-time test of an unverified path. Framed this way in the acceptance criteria below.

---

## New finding not in the draft: no `PUBLIC_BASE_URL`-equivalent for Twilio

ACS has an explicit `ACS_DEV_TUNNEL` override (`bridge_calls.py`'s `public_base_url_override`) for exactly this local-testing-behind-a-tunnel scenario. **Twilio has no equivalent.** Confirmed by reading the actual code: `server/app/providers/twilio/__init__.py:56` validates the Twilio signature against `request.url` directly, and line 73 builds the WebSocket URL from `request.host_url`. Both are derived from whatever `Host`/request-line data the tunnel actually forwards — there is no config override if the tunnel rewrites or mangles it.

**Why this matters for the tunnel choice below:** if the tunnel doesn't pass the original `Host` header through unchanged, `TwilioEventHandler._reconstruct_url()` (`event_handler.py:21-24`, which forces `https` and derives the host from the request) could build a URL that doesn't match what Twilio actually signed against — causing every webhook call to fail signature validation with a 403, before the bridge ever sees the call. This must be verified as a first step, not assumed to work.

---

## Tunnel: Cloudflare named tunnel (not ngrok)

**Decision, not still-open:** use `cloudflared` with a named tunnel, not ngrok, for the reasons below. `cloudflared` is confirmed available via `winget install Cloudflare.cloudflared` (verified present in the winget catalog on this machine, not yet installed).

| | ngrok free tier | Cloudflare named tunnel |
| --- | --- | --- |
| Cost | Free | Free |
| URL stability across restarts | New random URL every restart, unless paid tier | Fixed hostname, permanent |
| Setup | No account needed for a random URL | One-time: free Cloudflare account + a domain (a free `*.cfargotunnel.com`-backed setup works, or use an existing/cheap domain if the founder has one) |
| Webhook re-pointing needed | Every tunnel restart, across Wed/Thu/Fri | Once, ever |

Given the plan needs at least 2 separate test-call sessions (Wed, Thu) plus the live demo (Fri), a fixed hostname avoids re-pointing the Twilio console webhook 3+ times and avoids the specific failure mode of testing against a stale URL because the webhook wasn't updated after a tunnel restart.

**Setup steps (once):**
1. `winget install Cloudflare.cloudflared`
2. `cloudflared tunnel login` (opens a browser, authorizes against a free Cloudflare account — founder does this once)
3. `cloudflared tunnel create voiceagentpilot-pilot` (creates a named tunnel, gets a stable tunnel ID)
4. `cloudflared tunnel route dns voiceagentpilot-pilot <chosen-subdomain>.<a domain the founder controls, or a free Cloudflare-provided one>`
5. Run `cloudflared tunnel run voiceagentpilot-pilot` alongside the local bridge, pointed at `http://localhost:8000`

**First-step verification (per the "no PUBLIC_BASE_URL" finding above):** before pointing the real Twilio webhook at the tunnel, send one manual `curl -X POST https://<tunnel-host>/voice` (or open it in a browser) and confirm the bridge receives a request with the correct `Host` header — this single check either confirms the tunnel is safe to proceed with or surfaces a `Host`-header mismatch immediately, before it costs a wasted real test call.

---

## Prerequisites checklist (from the founder, in order)

1. **Foundry auth for local testing** — resolve explicitly, don't assume: either (a) the local machine's `az login` identity has Foundry User on the `hireastra` project and pinned agent v10 (agent mode's `DefaultAzureCredential` path, per D-004's Entra-ID-only intent — no key needed), or (b) a Voice Live API key/endpoint is issued separately. **Path (a) is checked first** (lower setup cost, matches D-004's preference) — verify with a direct API call or the SDK's own auth check before assuming it's sufficient; if it fails, fall back to (b).
2. **Cloudflare tunnel set up** per the steps above, verified reachable.
3. **Webhook configured** in the Twilio console: Phone Numbers → Manage → Active Numbers → **+1 (226) 741-3885** → Voice Configuration → "A call comes in" → webhook = `POST https://<tunnel-host>/voice`.

---

## Acceptance criteria

1. **Connects within 10 seconds.** From call pickup (Twilio `start` event) to the agent-mode Voice Live connection succeeding, measured from bridge logs, not wall-clock guessing on the call itself.
2. **Routes to `re-intake-pilot-agent` v10 specifically, not "latest."** Verified via the bridge's own log line showing the actual `agent_name`/`project_name`/`agent_version` sent to Voice Live (the same fields UT01a's own regression test asserts against, now observed in a real run) — not inferred from the agent's behavior alone, since a wrong-but-similar agent could sound plausible.
3. **Audio flows both directions**, confirmed audibly by the founder on the call itself.
4. **Call ends cleanly on hangup**, leveraging UT01b's hardening — confirmed via bridge logs showing the clean `request_end`/session-close path, not a timeout or force-close path. If the log instead shows a `voice_live_force_closed` or a timeout-driven close, that's a finding, not a pass.
5. **A deliberate quick-hangup check**, framed as confirming already-tested logic under real conditions (see Correction #4 above): the founder calls and hangs up within ~2 seconds of connecting, and the bridge log confirms a clean, immediate session end with no orphaned connection and no stuck call slot — matching what `test_caller_hangup_mid_connect_leaves_no_orphaned_connection` already proves in isolation.

**All five criteria are checked from bridge log output, not solely from what the call "sounded like."** The founder's live audio confirmation (#3) is necessary but not sufficient — every criterion needs the corresponding log evidence before UT02 is declared complete.

---

## Explicitly out of scope for UT02 (carried in from the draft, still correct)

- **Fixing Q-029** (the WS stream token not bound to Twilio's `CallSid`, no used-once check) — logged, deferred, not this unit's job. Do not let a fix here balloon UT02's timeline.
- **The pre-existing flaky test** (`test_connected_wait_timeout_hangs_up_without_play`, Q-030) — if it's the only failure in a suite run and it's the known, already-logged flake, it does not block anything in UT02 (there's no merge gate here, since this is a live call, not a PR, but it also shouldn't be mistaken for a new regression if it shows up during a pre-call sanity `pytest` run).
- Any Bicep/Azure infra work — UT02 runs the bridge locally against the tunnel, not a deployed environment.

---

## Log masking during this unit (D-021 still applies)

Any ad-hoc debug logging added for this test session must still mask phone numbers per `mask_number()` — including the founder's own real number if it appears as the caller ID on the test call. No real number appears in plaintext in any log line, temporary or otherwise, and nothing from this test session's logs is committed to the repo.

---

## Auth Token rotation — explicit spec step, not an afterthought

The current Twilio Auth Token was shared over chat (not out-of-band as originally planned), so it is treated as potentially exposed. **Immediately after the first successful test call** (end of the Wednesday session), before any second test call:

1. Generate a new Auth Token in the Twilio console.
2. Update the local `.env` (`TWILIO_AUTH_TOKEN=`) with the new value.
3. Restart the local bridge process so it picks up the new token (env vars are read at process start, not live-reloaded).
4. Confirm the new token works with one more manual signature-validated request before relying on it for Thursday's test call.

This step is not optional or deferrable — it happens between Wednesday's call and Thursday's, not "at some point before Friday."

---

## Risk flagging

If either of the founder's two dependencies (Foundry auth confirmation, tunnel set up and verified reachable) is not resolved by end of day 2026-09-28 (tomorrow), that is flagged back to the founder immediately as a real risk to the Wednesday target — not absorbed or worked around silently. The code side of this pilot (UT01a, UT01b) is done; nothing about Wednesday's timeline depends on further code work, only on these two external dependencies landing in time.
