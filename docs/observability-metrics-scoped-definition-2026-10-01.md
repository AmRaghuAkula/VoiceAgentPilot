# Observability and Key Metrics — Backlog Scope Definition (2026-10-01)

**Status: captured, not yet a design spec, not yet prioritized against other backlog items.** This file exists so the requirement is tracked and not lost. It is not a go-ahead to design or build.

**Origin:** the founder asked, while counting dropped calls by hand in the Azure portal on 2026-10-01, for an observability matrix: which key metrics we should watch, where each one comes from, and what "bad" looks like. Counting by hand worked, but it took several ad hoc queries, and the first one was wrong (it searched a log line that does not exist in that form). A written, tested, saved set of metrics removes that fragility.

## What the founder asked for

- An **observability matrix**: the key metrics worth checking, per area of the system.
- Put on the **backlog**. Nothing is built from this yet.

## What we can measure today (verified against the code and logs, 2026-10-01)

| Signal | Where it lives | Notes |
| --- | --- | --- |
| Calls seen by the bridge | Container App console logs (Log Analytics): `Call ended: call_id=… provider=… duration=…s` | Includes test calls; no way to separate real callers from tests yet |
| Calls the bridge ended itself, with a reason | Same logs: `call_ended reason=<code>` | Codes: `voicelive_connect_failed`, `voicelive_dropped`, `response_failed`, `idle`, `call_cap` |
| Calls cut by the maximum-duration cap | Same logs: `Call expired (max duration)` | |
| Caller hang-ups | Same logs, as a normal `Call ended` with **no** `reason=` | Indistinguishable from a drop except by a very short duration |
| Tool service outcomes (SMS today) | The tool's own App Insights: one request per call with a `code` | `sent`, `already_sent`, `invalid_request` (+reason), `rate_limited`, `forbidden`, `unavailable`, `send_failed`, `send_unconfirmed`; also latency, segments, encoding |
| Agent/Foundry behaviour | Foundry portal Traces and Monitor tabs | Not queried programmatically yet |
| Twilio-side call and message status | Twilio console | Calls that never reach the bridge are only visible here |

**Baseline captured 2026-10-01 (last 7 days, mostly test traffic):** 33 calls seen by the bridge; 3 ended as `response_failed`, all on 2026-09-29 within about 20 minutes and none since; 0 other bridge-side failures; 0 maximum-duration cap hits; 7 calls under 20 seconds; average about 131 seconds.

## Candidate metrics to put in the matrix (to be reviewed, not decided)

1. **Call health:** calls per day; bridge-ended failures by reason; short calls (under 20 s); average and 95th-percentile duration; cap hits; concurrent calls against the cap; time to connect to Voice Live.
2. **Response health:** responses that fail or never finish; watchdog triggers (`no_progress`, `retry_rejected`); retries used; time from caller speech to first agent audio.
3. **Tool health (per tool: SMS now, calendar later):** calls per day; outcome mix by `code`; latency 50th/95th percentile against the 15 s OpenAPI watchdog (D-050); `forbidden`/`unauthorized` counts (an identity or role problem); `unavailable` rate (a config or secret problem); provider error codes.
4. **Business funnel (only if call logging lands):** calls that captured a lead; follow-up texts sent per call; bookings made.
5. **Platform and cost:** Container App restarts, CPU and memory; Voice Live and model usage; Twilio call and SMS spend, SMS segments per message; Key Vault read failures; secret and API key age (rotation due).
6. **Change and drift:** which agent version each call used (the bridge runs unpinned `latest`, D-049); detect an unplanned change in production behaviour after any agent save.

## Known gaps this would need to address

- `unavailable` from the SMS service carries **no reason code**, so diagnosing it needs a guess (found 2026-10-01). A non-secret reason code is a small candidate fix.
- Caller hang-ups and drops look the same in the bridge logs.
- No identifier ties together one call's bridge log, the tool request, and the Foundry trace.
- No alerts exist today for any of this. Everything is a manual query.
- Calls that fail before reaching the bridge are visible only in Twilio.
- Test calls and real calls are not distinguishable.

## Open questions to raise at spec time (not decided here)

- Form: a dashboard (workbook or Grafana), alerts, a periodic report, or a saved query pack as a first step.
- Who is told, and how (the daily email has no connector today), and what counts as urgent.
- Thresholds: what failure rate or latency is "bad", and for how long.
- Privacy: phone numbers stay masked (D-006); no transcripts or message bodies in metrics; retention period.
- Data residency: where logs and metrics may live (see Q-074 for Canada).
- Cost of log retention and any extra alerting.
- Relationship to other backlog items: the whole-repo security audit (Q-071), call logging and notifications, the production-readiness checklist (`docs/production-readiness.md`).

## Possible first step (small, no design needed)

A saved, tested set of the queries above (call counts, failure reasons, short calls, tool outcome codes), written down with expected results, so any session can answer "how many calls dropped?" without rebuilding the queries.

## Sequencing

**Priority: backlog, not yet ranked.** Current order, as of 2026-10-01: SMS demo (USMS02 finishing: attach, demo, detach, key rotation), then calendar booking (UC02a onward); live call transfer and call logging are not yet scheduled. The founder should say where observability lands before a design spec is dispatched.

**Do not build or spec from this yet.**
