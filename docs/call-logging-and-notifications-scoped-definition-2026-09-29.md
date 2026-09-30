# Call Logging & Notifications — Backlog Scope Definition (2026-09-29)

**Status: captured, not yet a design spec, not yet prioritized against other backlog items.** This file exists so the requirement is tracked and not lost — it is not a go-ahead to design or build.

**Origin:** the founder asked, while reviewing the calendar-booking work, how a realtor using the agent would get a summary of calls and a list of prospects who reached out. The honest answer, verified directly against the current code (`server/app/`) on 2026-09-29: **nothing today captures call outcomes anywhere a human could read them.** Every call's transcript flows through Voice Live and is logged only at DEBUG level (normally off in production) as raw, unstructured log lines mixed with connection/audio noise. There is no CRM, spreadsheet, or email-sending code anywhere in this repo. If a realtor asked for a list of this week's callers today, nobody could produce one without manually digging through raw Azure logs.

**Explicit requirement from the founder, verbatim intent — read this before scoping:** this must be designed **agent-agnostic and destination-agnostic from the start**, the same discipline just applied to the calendar-booking feature. Specifically:
- **Plug-and-play to any email service** — not hardcoded to one provider (e.g. not assuming Gmail/SendGrid specifically); a realtor's own email provider should be swappable without a redesign.
- **Plug-and-play to any target document/data source** — the founder used "one central xls or table" as the example, but the design must not assume a spreadsheet specifically; a database, a CRM, or a different document format should be swappable the same way the calendar work made Google/Apple/Microsoft calendars swappable.
- This is explicitly one of the **agent-agnostic platform capabilities** the founder wants built as reusable infrastructure — not a one-off feature wired to `re-intake-pilot-agent` specifically. The same binding/adapter pattern used for calendar-booking (a generic core, thin vendor-specific adapters, a per-deployment config binding a specific agent to specific destinations) is the expected shape, not a novel approach.

## What this likely needs to cover (not yet designed — captured for scoping)

1. **Call summarization.** Something has to turn a raw call (transcript, duration, outcome) into a structured, human-readable summary — likely agent-authored (matching D-004: the agent, not the bridge, decides what a call was "about"), triggered either by an agent tool call at the end of a conversation or a bridge-side call-end hook.
2. **A generic "notification" adapter layer** — plug-and-play email, analogous to the calendar service's `CalendarProvider` adapter interface (`get_busy`/`find_bookings`/`create_event`/`delete_event`). Needs its own small, vendor-neutral contract (e.g. `send_summary(recipient, subject, body)`), with a first concrete implementation (provider TBD — likely whichever email service is easiest to get working for a demo, decided at spec time, not now) and room for others later.
3. **A generic "lead record" adapter layer** — plug-and-play storage, analogous to the calendar adapter but for writing structured rows (caller name/phone, call time, summary, outcome, any lead-qualification fields the agent captured) to a destination. Needs the same adapter-interface discipline so a spreadsheet-backed store, a lightweight database, or a real CRM are all swappable implementations of one contract, not three different codebases.
4. **Binding/config model** — likely reuses or directly extends the calendar service's own binding concept (one deployed service, many agent/destination pairings via config), rather than inventing a parallel mechanism. Whether this becomes part of the same `agent-tools/` service family or its own sibling service is an open architecture question for the spec to resolve, not assumed here.
5. **Privacy/security bar** — same discipline as calendar (D-006-equivalent secret handling, no credentials reaching the agent, masked phone numbers in logs per the bridge's existing standard) applies to whatever email/storage credentials this needs.
6. **What triggers this** — every call, or only calls that reach some qualification bar? Not decided. Needs a real answer at spec time (likely a founder decision, similar to the "which calendar/which OAuth setup" questions raised for the calendar feature).

## Explicit open questions to raise at spec time (not decided here)

- Does this apply to every call, or only "qualified" ones (and if so, who/what decides qualification — the agent's own judgment, per the live-transfer feature's planned "agent judgment" trigger model)?
- What email service and what storage destination should the first concrete implementation target, for the demo/testing phase (mirroring how calendar-booking picked Google Calendar as its first adapter while staying provider-agnostic in architecture)?
- Does a call summary reuse any of the calendar service's own scaffolding (Key Vault, managed identity, Azure Function hosting), or does it need its own?

## Sequencing

**Priority: backlog, not yet ranked against other pending work.** As of 2026-09-29, the founder's stated priority order is: (1) latest-agent-version cutover — done; (2) calendar booking — spec accepted, UC00 (implementation plan) in progress; (3) live call transfer — not yet scoped. This call-logging/notifications feature was raised after that ordering was set and has not yet been slotted into it — the founder should say explicitly where it lands relative to live call transfer and any other backlog item before a design spec is dispatched.

**Do not build or spec from this yet.** Per the same discipline as the calendar and live-transfer backlog items: this file tracks the requirement so it isn't lost, not a go-ahead.
