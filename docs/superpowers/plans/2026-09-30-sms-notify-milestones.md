# SMS Follow-up Notification Tool — Milestones

Last updated: 2026-09-30 (rev 1, written in USMS00) · Plan: [2026-09-30-sms-notify-plan.md](2026-09-30-sms-notify-plan.md) · Spec: [../specs/2026-09-30-sms-notify-design.md](../specs/2026-09-30-sms-notify-design.md) · Decisions: D-062, D-063

**End state of this track:** on the founder's demo call (2026-10-02), near the end of the call and after the caller agrees, the production agent sends one agent-written SMS through `agent-tools/sms-notify/` to the fixed follow-up contact, and the text arrives. After the demo the tool is detached from production (Q-090).

## How work is cut

- **Same rules as the other tracks** (CLAUDE.md §3–§6): one unit = one plan task = one session = one branch = one PR; one branch at a time, shared with bridge and calendar work; status inside the unit's PR; merge commits; daily email.
- **USMS02 is branchless** (CLAUDE.md §3), with D-062's carve-out: its Azure resources are persistent, but the resource list is still committed first on its status branch, each item founder-approved (Q-091).
- **Build, code review and `cso` run on Opus** (CLAUDE.md §0, D-034); status commits, PR mechanics and USMS02's ops steps follow §0's Sonnet routing.
- **This file holds definitions, not live status.** Unit status, PR numbers, test counts and review results live only in STATUS.md §1c.
- **The whole suite** (bridge, calendar once UC02a has merged, and sms-notify once USMS01 has merged) must pass on every code unit.

## Units and branches

| Unit | Plan task | Milestone | Branch | Kind | Gate (Q-NNN) | Tests added (estimate) |
| --- | --- | --- | --- | --- | --- | --- |
| USMS00 | Spec, plan, milestones, D-062/D-063, governance | SM0 | `docs/sms-notify-spec` | Docs | Q-087, Q-088 (answered) | 0 |
| USMS01 | Build `agent-tools/sms-notify/` (provisions nothing) | SM1 | `feat/usms01-sms-notify` | Code | Q-085, Q-093 (answered) | ~90 (+1 opt-in live) |
| USMS02 | Deploy, rehearse, production attach, detach | SM2 | none (branchless; `docs/status-YYYY-MM-DD`) | Ops | Q-086, Q-089, Q-090, Q-091, Q-092 (answered) | 0 (evidence; 1 live send) |

## Milestones

### SM0 — Spec landed (USMS00)
- **DoD:** spec rev 2.1, this doc and the plan merged; D-062 and D-063 ACCEPTED in DECISIONS.md; STATUS §1c, Q-085 to Q-093 (ANSWERED) and Q-094 (OPEN, post-demo lifecycle); CLAUDE.md governance lines applied; Opus docs review CLEAN.
- **Test coverage:** none (docs only). Both existing suites unaffected.
- **PR slots:** 1 (USMS00).

### SM1 — Built and validated offline (USMS01)
- **DoD:** plan §5 USMS01 and the CLAUDE.md §9 universal DoD; Bicep validates; nothing provisioned.
- **Test coverage (spec §9):** core normalization and limits; deadline arithmetic and per-recipient sending (P6); dedupe with recorded outcome, cooldown, hourly cap, concurrency; Twilio adapter over `httpx.MockTransport`; auth and the always-200 matrix; config; OpenAPI contract; guards G1–G3. `live_twilio` exists but is excluded by default.
- **Test readiness:** all offline, runnable on any clone after `cd agent-tools/sms-notify && python -m uv sync --group dev`.
- **PR slots:** 1 (USMS01).

### SM2 — Demo-ready and demo done (USMS02)
- **DoD:** plan §5 USMS02 (one session that starts before the demo and runs through the detach, holding the only branch; plan §3 timeline; a pre-demo code fix goes through the plan §3 USMS02b contingency): resource list committed first; validated and previewed provision; `live_twilio` passes; copy agent sends exactly one SMS and refuses a repeat; failure drill passes; production test call delivers; tool detached after the demo; redacted evidence recorded; status PR merged.
- **Test coverage:** one opt-in live send; scripted copy-agent checks; one live production call.
- **PR slots:** 1 status PR (branchless unit).
