# Live Call Transfer — Backlog Scope Definition (2026-10-01)

**Status: captured, not yet a design spec. Ranked by the founder on 2026-10-01: after call logging (see "Founder answers" below).** This file exists so the requirement is tracked and not lost. It is not a go-ahead to design or build.

**Origin:** "live call transfer" has been on the founder's priority list since 2026-09-29, third after the agent-version cutover and calendar booking (see the sequencing note in `docs/call-logging-and-notifications-scoped-definition-2026-09-29.md`), with an "agent judgment" trigger in mind. On 2026-10-01 the founder asked for it to be written down as a scope definition. The idea: during a call, the agent hands the live caller to a human at the pilot customer's business (the host), instead of only promising a callback.

## What exists today (read on the current checkout, 2026-10-01)

- **How a call is bridged (Twilio, the live path).** `/twilio/voice` answers with TwiML that says a short greeting and then `<Connect><Stream>` to the bridge's `/twilio/ws` media socket (`server/app/providers/twilio/event_handler.py`, `generate_stream_twiml`). There is no verb after `<Connect>` and no `action` URL.
- **How a call ends.** Every end goes through `TwilioCallSession.request_end()` (`server/app/providers/twilio/call_session.py`, D-005). It closes the media socket silently; "the TwiML has no verb after `<Connect>`, so Twilio then hangs up" (module docstring). There is no Twilio REST call-control code in the bridge.
- **The bridge knows the call's identity.** `TwilioMediaHandler` stores `callSid` from the stream's `start` event (`server/app/providers/twilio/media_handler.py`, `on_message`). Nothing passes it anywhere else.
- **How tools work today.** The agent calls an OpenAPI tool attached directly to the Foundry agent (D-059(a), D-062); the tool service acts (sends the SMS, books the slot). The service sees only a Foundry token and the agent's arguments. **It cannot tell which call it is serving.** That is why "one SMS per call" is instructions plus backstops, not a true per-call limit (Q-093: "a true per-call limit would need a future bridge unit").
- **What the bridge may not do.** D-004: the agent, in Foundry, authors behaviour; the bridge sends no content or behaviour overrides (D-038/D-050 neutral markers are the narrow, recovery-only precedent).
- **Timing.** On OpenAPI-direct, the model's time plus the tool's time must stay under the bridge's 15 s response watchdog (D-050, D-059(d)). A non-2xx tool response fails the whole turn (D-059, E10).

## TELEPHONY_BRIDGE_SPEC.md §7 "Do not build" (quoted exactly)

§7 says "Claude Code should stop and ask [the founder] before doing any of these", and lists, among others:
- "Calendar or booking tools, or any OpenAPI tool wiring (separate spec)"
- "Outbound calling"
- "Storing transcripts or caller data anywhere outside Foundry traces and logs"
- "Changing the agent's instructions, voice or speech settings from the bridge"

§1 also says: "Out of scope: calendar tools (separate spec), outbound calls, …". D-063 kept outbound calling banned explicitly: "Every other §7 item stands, including outbound calling: an SMS is not a call."

**What this means for transfer:** a transfer dials a new leg from our Twilio number to the host, which reads as **outbound calling**. It would need its own narrow, per-tool §7 lift, the same form as D-052/D-057 (calendar) and D-063 (SMS): OpenAPI wiring for the transfer tool, outbound dialing limited to configured destinations, and, if a summary or recording is passed to the human, the caller-data item too. Unlike those lifts, this one likely also touches the **bridge** (see the central question below), which no earlier lift did. That lift is a founder decision at spec time; it is not drafted here.

## What this likely needs to cover (not yet designed)

1. **The likely shape.** An agent-callable tool, e.g. `transfer_call(reason)`, whose service asks Twilio to move the live call to a human. On Twilio this means changing the call's TwiML (REST update of the call, or a `<Connect action=…>` URL that returns `<Dial>`). Either way **the media stream to the bridge ends**, so the agent's session ends at the moment of transfer. (Twilio behaviour from its public docs, not yet verified in this repo; the feasibility step below checks it.)
2. **The central architecture question: who knows the CallSid, and who acts?** The tool service cannot see which call it serves (same limit as Q-093). Candidate directions, **none decided**:
   - (a) the bridge acts: it notices the agent's transfer request (if Voice Live exposes the tool call to the bridge, unverified) or is told by the tool service, then ends the stream so Twilio follows a `<Connect action>` URL that returns `<Dial>`;
   - (b) the bridge gives the tool service a per-call reference (not the raw CallSid in the conversation, and never something the agent could alter to redirect another call);
   - (c) the tool service finds the live call itself via Twilio's REST API (only safe with one concurrent call; fragile).
   Each has D-004, security and D-002 (upstream-merge) consequences. This is an Opus design question under D-030, with the §7 part going to the founder.
3. **Trigger.** Agent judgment (the agent decides a human is needed: a hot lead, a question it can't answer, an upset caller) versus explicit caller request only ("let me talk to a person"), or both. The trigger lives in the agent's Foundry instructions (D-004), never in bridge code.
4. **Cold versus warm transfer.** Cold: the caller is put straight through. Warm: the human hears a short summary (a whisper) before being connected, or the agent stays on briefly. Warm is much harder on a bridge whose agent session ends when the stream ends.
5. **Destinations.** Configured per binding (Key Vault or app config, as with `sms-recipients`), never in the prompt and never chosen by the agent's free text; at most a fixed key such as `sales` or `front_desk`. Canada-only numbers, E.164, masked in logs (D-006).
6. **Availability.** Business hours and time zone per binding; what the agent says out of hours (offer a callback, a booking, or an SMS to the host instead).
7. **If nobody answers.** Ring timeout, then one of: voicemail (stores audio, another §7 item), an SMS to the host via the existing tool, a callback promise, or back to the agent (a fresh `<Connect>` means a new Voice Live session that has lost the conversation unless context is passed, which is hard).
8. **Caller ID on the human's phone.** Show the caller's number or our Twilio number; affects whether the host can recognise and call back.
9. **Bridge logging.** Today a transferred call would end like a caller hang-up (no `reason=`). It needs a distinct end reason such as `transferred`.
10. **Cost.** The dialled leg is billed per minute on Twilio on top of the inbound leg.

## Interactions with other work

- **Calendar (D-052, UC track):** offer transfer versus booking; the agent should not do both in one turn. A transfer during a booking call must not leave a half-made booking.
- **Call logging (backlog):** the natural "summary handoff" to the human (whisper or a text/document just before the transfer) and recording the transfer outcome in the call record.
- **SMS (D-062/D-063):** a no-answer fallback could reuse the SMS tool; check its cooldown and hourly cap (D-062) don't block it.
- **15 s watchdog (D-050):** the tool must answer fast, and the agent should speak "connecting you now" **before** calling it, since the stream may end before any reply is spoken.
- **D-049 (unpinned `latest`):** attaching the tool to the production agent goes live on the next call; use a copy agent for testing, as USMS02 did.

## Metrics for the observability matrix (Q-097)

Transfers attempted, answered, no-answer, busy, failed; time to answer; caller hang-ups while ringing; transfer rate per call; fallback outcomes; tool latency against the 15 s watchdog; Twilio dial-leg minutes and cost.

## Questions for Legal (not for this repo to answer)

- Caller consent: must the agent ask before transferring, and disclose that the caller has been talking to an AI?
- If the transferred leg is recorded, or voicemail is used, what notice and consent do Canadian law and the host's obligations (PIPEDA and provincial equivalents) require?
- Is a whisper summary to the host a disclosure that needs notice to the caller?
- Emergencies: the agent should never transfer to emergency services; what must it say instead?

## Open questions for the founder (each with a partner recommendation)

1. **Where does this rank?** Before or after call logging, and before or after observability? *Recommendation:* after call logging, because transfer is much more valuable with a summary handoff, and because it needs a bridge change that should wait for the security audit's scope to be known.
2. **Trigger:** agent judgment, caller request, or both? *Recommendation:* caller request first, then agent judgment as a later setting.
3. **Cold or warm?** *Recommendation:* cold first; warm only after call logging exists.
4. **No-answer fallback?** *Recommendation:* SMS to the host plus a callback promise; no voicemail (avoids a new recording and data store).
5. **Are you willing to consider a narrow §7 lift that touches the bridge** (outbound dialing to configured numbers, plus a small call-control hook)? *Recommendation:* yes in principle, decided only after the feasibility step shows the smallest bridge change needed.
6. **Who answers in the pilot?** A single destination (the host's mobile) for the pilot. *Recommendation:* one destination, business hours only.
7. **Throwaway resources for the feasibility step** (a test Twilio number or a scratch TwiML endpoint, a copy agent): approve when the step is sequenced. *Recommendation:* same rules as UC01 (D-053), torn down in the same session.

## Founder answers (2026-10-01, direct instruction: "OK", meaning all as recommended)

1. **Rank:** after call logging, and before the observability matrix (which is last before the production gates).
2. **Trigger:** the caller asking for a person. The agent's own judgment is a later setting.
3. **Cold or warm:** cold transfer first. Warm only once call logging exists.
4. **If nobody answers:** the agent texts the host with the existing SMS tool and promises a callback. No voicemail.
5. **Section 7:** a narrow lift that touches the bridge (outbound dialing to configured numbers, plus a small call-control hook) is acceptable **in principle**; its details are decided only after the feasibility step shows the smallest bridge change needed.
6. **Pilot destination:** one number, the host's mobile, during business hours only.
7. **Feasibility resources:** the throwaway resources for the feasibility step are approved when the step is scheduled, under the UC01 rules (D-053), and deleted in the same session.

Still not decided: the architecture question (who knows the call ID, and who acts) and the Legal questions above. Neither is a founder decision yet.

## Suggested first small step (not a build)

A **branchless feasibility unit**, like UC01 (D-053): throwaway setup only, scratchpad code only, evidence redacted, resources deleted in the same session, founder go first. It would answer:
- Does a REST update of a live `<Connect><Stream>` call to `<Dial>` work, and what does the bridge see (stream `stop` event, socket close)?
- Does a `<Connect action=…>` URL fire when the bridge closes the stream, and can it return `<Dial>`?
- Does the bridge's Voice Live event stream expose the agent's tool call at all (direction (a))?
- Timing against the 15 s watchdog, ring-timeout behaviour, and caller ID on the dialled leg.

Its result would feed the design spec and the §7 lift, the same way D-059 fed the calendar work.

## Sequencing

**Priority: backlog, ranked.** Founder order as of 2026-10-01: calendar booking (UC02a to UC12), then call logging (plug-and-play, Google Docs/Sheets and Microsoft Office targets), then **live call transfer**; observability comes last, before the production gates. The hard gates before real callers are the whole-repo security audit (Q-071) and Canada hosting (Q-074; the founder's list also cites Q-084, a number D-062 reserves for the call-records draft, with no STATUS.md row yet). Any transfer work would sit behind those gates for production use.

**Do not build or spec from this yet.**
