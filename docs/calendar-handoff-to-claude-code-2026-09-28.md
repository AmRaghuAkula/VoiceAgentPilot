# Calendar Booking — Handoff Brief for Claude Code
**Demo:** Friday 2026-10-02 (the pilot customer) · **Gate:** working end-to-end by Wed 2026-09-30 EOD, or Friday runs callback-only.

## Who does what
- **Partner (now):** turn this brief + the existing spec into an implementation-ready build spec.
- **Builder (after M6):** implements it.
- **Raghu:** one Google OAuth consent click when asked; confirms which Google account to use.

## Objective
Give the Foundry voice agent (the pilot's production agent) the ability to check real availability and book real meetings on **the founder's Google calendar** during a live voice call. Demo-scoped — the pilot repoints the same build at the pilot customer's calendar.

## The call flow (locked)
Prospect calls → the agent qualifies → prospect chooses **phone call or in-person meetup** (no location handling) → the agent reads real free slots → caller picks one → the agent reads back date + time, gets a yes → books → confirms "the founder will reach out at [day, time]" → warm close. If the calendar tool is missing or fails, the agent falls back honestly to "the agent will call you back" — never claims a booking that didn't happen. (This fallback is already in the agent instructions.)

## Locked decisions
- **Google Calendar** (not Outlook — avoids tenant admin consent).
- **The founder's calendar** for the demo; the pilot customer's for the pilot (same build, new OAuth consent).
- Bookable hours **9am–7pm America/Toronto**, default **30-minute** meetings.
- Auth: OAuth refresh token in **Azure Key Vault**; agent never sees credentials.
- Booking confirmation names **the founder** (demo); pilot names the pilot customer.
- **SMS confirmation: parked** unless Raghu says otherwise — cut for the demo unless calendar is green early.
- No new purchases. No invented slots, ever.

## References
- Existing build spec: `~/workspace/voice-agent/calendar-booking-build-spec-2026-09-28.md`
- Agent instructions (tool behavior contract): the production agent's Foundry instructions document (kept outside this repo) — see "Next steps — calls and meetups" and "Closing".

## What the partner's spec must cover
1. **Architecture:** Azure Function (HTTP) → Google Calendar API; Key Vault via managed identity; Foundry agent tools calling the function.
2. **Auth, step by step:** Google Cloud project + OAuth client setup, exact scopes, how Raghu's one-time consent is captured, refresh-token storage in Key Vault, server-side token refresh.
3. **`check_availability`:** inputs (date/range, duration), free/busy query constrained to 9am–7pm America/Toronto, output format (ISO 8601 with offset), explicit error (not fake slots) when the calendar is unreachable.
4. **`book_appointment`:** inputs (caller name, phone, call/meetup, start datetime from a slot previously returned, duration); **re-check the slot immediately before creating** (no double-books); **idempotency** (don't create a duplicate if called twice); event title/description contents; success/failure output shape.
5. **Foundry wiring:** exact steps to expose both as tools on the production agent (OpenAPI/function route), including tool descriptions that restate the honesty constraints.
6. **Failure modes:** slot taken between check and book; API down; duplicate call; vague date from caller (agent resolves + reads back — prompt-side, noted for completeness).
7. **Test plan:** the six checks in the existing spec (real slots, event correctness, double-book refusal, API-down fallback, vague-date handling, full voice rehearsal Thursday).

## Acceptance criteria
Both tools wired to the agent; all six tests green by Wed EOD; a real test booking appears on Raghu's Google calendar with correct title, time (ET), and caller details. Anything less → demo runs the callback-only version.
