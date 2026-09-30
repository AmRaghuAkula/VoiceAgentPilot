# Calendar Booking — Scoped Implementation Definition (2026-09-29)

**Status: captured, not yet turned into a design spec.** This is the founder's full scope definition, prepared via Claude Desktop and handed to this session on 2026-09-29. It supersedes the level of detail in [`calendar-handoff-to-claude-code-2026-09-28.md`](calendar-handoff-to-claude-code-2026-09-28.md) (which remains as the original handoff framing — who does what, the call flow, the locked decisions) with concrete architecture, auth steps, tool contracts, and a test plan.

**Explicit sequencing (per the founder, verbatim in the brief below):** this is a stretch feature after the required Friday-demo builds. M6 reliability work and any remaining UT02 hardening take priority. Do not let calendar work pull focus from M6 while it's in flight. Pick this up once M6's core demo-readiness is confirmed.

**Do not build from this yet.** Per the founder's instruction (2026-09-29), spec-writing and design review are held off for now — this file exists so the scope is tracked and not lost, not as a go-ahead to implement.

---

## Subject: Calendar Booking — Implementation Spec (sequenced after M6 core demo builds)

**Sequencing (explicit, not implied):** This is scoped as a stretch feature after the required Friday-demo builds are done — M6 reliability work and any remaining UT02 hardening take priority. Do not let calendar work pull focus from M6 while it's in flight. Once M6's core demo-readiness is confirmed, pick this up. If the Wednesday EOD gate below turns out unrealistic once M6's actual timeline is known, say so immediately — the demo already has a safe fallback (callback-only), so slipping this is not a crisis, but silently absorbing the risk is not acceptable.

**Gate:** Working end-to-end by Wed 2026-09-30 EOD, or Friday's demo runs callback-only (already-built fallback, no risk to the base demo).

**Architecture independence (confirmed):** Build this decoupled from wherever the telephony bridge ends up running (laptop+tunnel or Azure Container App post-M6). The calendar tools are invoked by the Foundry agent directly via its own tool-calling mechanism (OpenAPI/function route to an Azure Function) — not by the bridge. So this spec has no dependency on M6's outcome and doesn't need to be redone if the bridge moves.

### 1. Architecture

- Azure Function (HTTP-triggered) — separate from the telephony bridge's Container App / laptop process entirely.
- Google Calendar API called from the Function.
- Key Vault holds the OAuth refresh token; Function's managed identity has Key Vault Secrets User role — no credentials ever reach the Foundry agent or its instructions.
- Foundry agent (the pilot's production agent) gets two new tools wired via OpenAPI/function route pointing at this Function's endpoints.

### 2. Auth — step by step

- Create a Google Cloud project (or reuse one if we already have one — confirm with Raghu before creating a new one, avoid resource sprawl).
- OAuth client (Desktop or Web app type — pick based on whichever supports server-side refresh-token issuance cleanly without a redirect UI running long-term).
- Scopes: narrowest that supports free/busy read + event create (`https://www.googleapis.com/auth/calendar.events` at minimum; avoid the broader full-calendar scope unless free/busy specifically requires it — confirm against Google's docs, don't assume).
- Raghu's one-time consent: a single OAuth consent screen click, captured once, refresh token extracted and stored in Key Vault immediately — never held in a file, `.env`, or chat.
- Server-side token refresh: Function must handle access-token refresh silently on every invocation using the stored refresh token; if refresh itself fails (revoked/expired), that's a distinct failure mode — surface it as "calendar unreachable," not a crash.

### 3. `check_availability`

- Inputs: date/range, duration (default 30 min).
- Query: Google Calendar free/busy, constrained server-side to 9am–7pm America/Toronto — do not trust the caller-provided range to already respect these bounds, clamp it.
- Output: ISO 8601 with UTC offset (not naive local time — avoids the classic "whose timezone is this" bug when Foundry or logs display it).
- Explicit error object when the calendar is unreachable (API down, auth failure) — must be distinguishable from "no free slots," since the agent's honesty constraint depends on knowing the difference between "nothing's free" and "I couldn't check."

### 4. `book_appointment`

- Inputs: caller name, phone, call-or-meetup type, start datetime (must be one previously returned by `check_availability` — do not accept an arbitrary datetime from the agent without re-validation), duration.
- Re-check the slot immediately before creating — fetch free/busy again in the same request just before the create call, reject if now busy (return a specific "slot just taken" error, distinct from generic failure, so the agent can react honestly: "actually that just got taken, let me check another time").
- Idempotency: generate a deterministic idempotency key from (caller phone + start datetime + duration) or similar, check for an existing event with that signature before creating — if the same booking request arrives twice (retry, double-invocation), don't create a duplicate event.
- Event contents: title should identify this as an AI-agent-sourced lead call/meetup (e.g. "Call: [Caller Name] — via voice agent"), description includes caller name, phone, call/meetup type, and a note this was booked by the voice agent.
- Success output: booked datetime (ET, human-readable, for the agent to read back), event ID.
- Failure output: reason code distinguishing slot-taken vs. API-down vs. validation error — the agent needs to react differently to each.

### 5. Foundry wiring

- Expose both tools on the production agent via OpenAPI schema pointing at the Function's routes.
- Tool descriptions must explicitly restate the honesty constraint inline (e.g. "Only call this with a slot previously returned by check_availability. Never invent a time. If this tool errors, tell the caller honestly that you'll have someone call them back — do not claim a booking succeeded.") — this is belt-and-suspenders on top of the agent instructions already covering fallback behavior.
- Confirm which Foundry agent version this gets attached to and whether it requires a new pinned version (per the existing "never latest" discipline already established for the telephony bridge) — do not let this slip through unpinned.

### 6. Failure modes to explicitly handle

- Slot taken between check and book → covered by re-check in §4.
- API down (Google Calendar unreachable) → explicit error, agent falls back to callback promise, never fabricates a slot or a confirmation.
- Duplicate call (agent retries the same booking) → covered by idempotency key in §4.
- Vague date from caller ("sometime next week") → this is prompt-side resolution (agent's own instructions handle disambiguation and read-back) — noted here for completeness, not a Function-side concern.
- New: Key Vault/refresh-token failure → distinguish from a plain "calendar API down" so whoever's debugging later doesn't waste time on the wrong system.

### 7. Test plan — six checks, all must be green by Wed EOD

1. Real free slots returned for a known date/time range, matching what's actually on Raghu's calendar.
2. Booked event appears correctly — right title, right time (ET), right caller details.
3. Double-book attempt on an already-booked slot is refused cleanly (not silently overwritten, not a crash).
4. API-down simulation → agent falls back honestly, no fabricated confirmation.
5. Vague-date caller phrasing → agent resolves and reads back correctly (prompt-side, verify it still works with the new tools attached).
6. Full voice rehearsal, Thursday — a real phone call exercising the whole flow end to end, not just the Function in isolation.

**Acceptance criteria:** both tools wired, all six tests green by Wed EOD, one real test booking visible on Raghu's actual Google calendar with correct title/time/caller details. Short of that, Friday's demo runs callback-only — already safe, no last-minute scrambling needed.

---

## Partner's review notes (2026-09-29, not yet actioned)

Captured for when spec-writing resumes:

1. **Where does this code live?** Not stated. A separate Function is architecturally independent of the bridge, but it needs a home — this repo (a new top-level directory sibling to `server/`) or a separate repo. This decision affects CLAUDE.md's scoping rules (real-estate words/agent names are banned from `server/app/`/`server.py` specifically — a new Function elsewhere isn't automatically covered unless we say so) and blocks everything downstream.
2. **Needs unit/branch/session structure** matching this project's discipline (CLAUDE.md §3, §0) — auth setup, `check_availability`, `book_appointment`, Foundry wiring, and test rehearsal each look like they'd be separate units, not one big build.
3. **Two open questions embedded in the brief need to become tracked Q-NNNs**, not silent implementation-time decisions: (a) reuse vs. create a new Google Cloud project, (b) OAuth client type (Desktop vs. Web).
4. **No STATUS.md/DECISIONS.md footprint yet** — if this proceeds under the same governance as the rest of the project, it needs its own unit table (like the Twilio-pilot track's UT-prefixed units) and a real plan document under `docs/superpowers/plans/`.
5. **Test plan mixes automated and live-call checks** without a stated tie-breaker — worth being explicit that check 6 (live rehearsal) is the true gate; checks 1–5 are necessary but not sufficient on their own.

**Next step, when resumed:** partner drafts a real design spec + implementation plan from this brief (Opus, per CLAUDE.md §0), answers or tracks the two embedded open questions, runs it through an Opus design review before any builder touches code, and gives it its own STATUS.md section.
