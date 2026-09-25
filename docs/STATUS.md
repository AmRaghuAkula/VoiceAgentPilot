# Status

The **first file every session reads**. It is the only place live status is tracked. Definitions (what "done" means and what each unit tests) are in the [milestone doc](superpowers/plans/2026-09-25-telephony-bridge-milestones.md). The partner updates this file at the end of every session (see [CLAUDE.md](../CLAUDE.md) → Session-end protocol).

Last updated: 2026-09-25

---

## §1 Unit dashboard

Status values: `NEXT` (the one unit the next session works on; its branch may exist only if its PR is open) · `CLOSED` (PR merged and branch deleted) · `QUEUED` · `BLOCKED`.

This table is updated **inside each unit's own PR** (D-017), so what `main` shows is always current.

| Unit | Plan task | Milestone | Status | Branch | PR | Tests (pass/planned) | Opus review | cso |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| U00 | Governance, spec, plan | — | NEXT | `docs/governance-and-bridge-plan` | — | 0/0 | 2 rounds; findings fixed | n/a (docs only) |
| U01 | Task 0 — foundation | M0 | QUEUED | `feat/tb-t00-foundation` | — | 0/1 | — | — |
| U02 | Task 1 — log mask | M1 | QUEUED | `feat/tb-t01-log-mask` | — | 0/7 | — | — |
| U03 | Task 2 — routing | M1 | QUEUED | `feat/tb-t02-routing` | — | 0/20 | — | — |
| U04 | Task 3 — bridge config | M1 | QUEUED | `feat/tb-t03-bridge-config` | — | 0/34 | — | — |
| U05 | Task 4 — server wiring | M1 | QUEUED | `feat/tb-t04-server-wiring` | — | 0/5 | — | — |
| U06 | Task 5 — expiry hooks | M2 | QUEUED | `feat/tb-t05-expiry-hooks` | — | 0/4 | — | — |
| U07 | Task 6 — Voice Live agent mode | M2 | QUEUED | `feat/tb-t06-voicelive-agent-mode` | — | 0/11 | — | — |
| U08 | Task 7 — call session | M3 | QUEUED | `feat/tb-t07-call-session` | — | 0/31 | — | — |
| U09 | Task 8 — bridge calls | M4 | QUEUED | `feat/tb-t08-bridge-calls` | — | 0/12 | — | — |
| U10 | Task 9 — ACS media handler | M4 | QUEUED | `feat/tb-t09-acs-media-handler` | — | 0/9 | — | — |
| U11 | Task 10 — ACS routes | M4 | QUEUED | `feat/tb-t10-acs-routes` | — | 0/17 | — | — |
| U12 | Task 11 — docs | M5 | QUEUED | `feat/tb-t11-docs` | — | full suite | — | — |
| M6 | Deploy (Step 4) | M6 | BLOCKED | — | — | — | — | — |
| M7 | Live acceptance tests | M7 | BLOCKED | — | — | 0/9 live | — | — |

**Milestone roll-up:**

| Milestone | Status |
| --- | --- |
| M0 | not started |
| M1 | not started |
| M2 | not started |
| M3 | not started |
| M4 | not started |
| M5 | not started |
| M6–M8 | blocked (§3: Q-002 to Q-006) |

**Unit tests passing overall:** 0 / ~151.

---

## §2 Audit log (append-only, newest last)

| Date | Unit | Event | PR | Notes |
| --- | --- | --- | --- | --- |
| 2026-09-24 | — | Repo pulled from GitHub | — | The local clone was empty. Fetched `origin/main` (HANDOFF.md, spec, README). |
| 2026-09-25 | U00 | Design spec rev 1 → rev 5 | — | Four Opus design-review rounds. Accepted by the founder (D-008). |
| 2026-09-25 | U00 | Implementation plan written | — | 12 TDD tasks; SDK APIs checked against azure-ai-voicelive 1.3.0 and ACS Call Automation 1.6.0. |
| 2026-09-25 | U00 | Governance set up | — | Partner/builder agents, CLAUDE.md session protocol, STATUS.md, DECISIONS.md, one unit per session/branch/PR (D-009 to D-015). |

---

## §3 Open questions

Status values: `OPEN` · `ANSWERED` (record the answer and link a D-NNN if it became a decision).

| ID | Question | Owner | Blocks | Status |
| --- | --- | --- | --- | --- |
| Q-001 | **Who merges PRs?** Proposed default: the builder merges its own PR once the Opus review, `cso` and tests all pass, then deletes the branch. That lets a session close with a single branch. The alternative is that the founder merges every PR, which leaves each session open until the founder acts. | Founder | Closing every unit, U00 onwards | OPEN |
| Q-002 | Buy the ACS test number (Option B subscription). | Founder + Cowork | M6 | OPEN |
| Q-003 | Confirm the region of `hireastra-resource`. | Cowork | M6 | OPEN |
| Q-004 | Bridge identity at deploy: keep the accelerator's user-assigned identity, or switch to system-assigned (spec §6)? Whichever is chosen gets Foundry User. | Founder | M6 | OPEN |
| Q-005 | Verify the ACS callback JWT issuer, JWKS URL and audience before enabling `ACS_CALLBACK_JWT_AUDIENCE`. | Builder at M6 | M6 | OPEN |
| Q-006 | Run `az login` + `azd auth login` on the deploy machine. | Founder | M6 | OPEN |
