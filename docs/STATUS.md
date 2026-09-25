# Status

The **first file every session reads**. It is the only place live status is tracked. Definitions (what "done" means and what each unit tests) are in the [milestone doc](superpowers/plans/2026-09-25-telephony-bridge-milestones.md). The partner updates it **inside each unit's PR, before the PR merges** (D-017). When no unit is in flight, updates go in a status-only PR (D-018). See [CLAUDE.md](../CLAUDE.md) §3 and §5 step 8.

Last updated: 2026-09-25

---

## §1 Unit dashboard

Status values: `NEXT` (the one unit the next session works on; its branch may exist only if its PR is open) · `CLOSED` (PR merged and branch deleted) · `QUEUED` · `BLOCKED`.

This table is updated **inside each unit's own PR** (D-017), so what `main` shows is always current.

| Unit | Plan task | Milestone | Status | Branch | PR | Tests (pass/planned) | Opus review | cso |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| U00 | Governance, spec, plan | — | CLOSED | `docs/governance-and-bridge-plan` | [#1](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/1) | 0/0 | 2 rounds; findings fixed | n/a (docs only) |
| U01 | Task 0 — foundation | M0 | CLOSED | `feat/tb-t00-foundation` | [#2](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/2) | 3/3 | 2 rounds; findings fixed | n/a (import/config/test scaffolding, no app security surface) |
| U02 | Task 1 — log mask | M1 | CLOSED | `feat/tb-t01-log-mask` | [#3](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/3) | 10/10 | 1 round; clean, low-severity notes only | n/a (pure string logic, no security surface) |
| U03 | Task 2 — routing | M1 | NEXT | `feat/tb-t02-routing` | — | 0/20 | — | — |
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

**Unit tests passing overall:** 13 / ~156 (U01's plan tally was 1, grew to 3; U02's tally was 7, grew to 10 after the review's boundary/int-input cases; see §2).

---

## §2 Audit log (append-only, newest last)

| Date | Unit | Event | PR | Notes |
| --- | --- | --- | --- | --- |
| 2026-09-24 | — | Repo pulled from GitHub | — | The local clone was empty. Fetched `origin/main` (HANDOFF.md, spec, README). |
| 2026-09-25 | U00 | Design spec rev 1 → rev 5 | — | Four Opus design-review rounds. Accepted by the founder (D-008). |
| 2026-09-25 | U00 | Implementation plan written | — | 12 TDD tasks; SDK APIs checked against azure-ai-voicelive 1.3.0 and ACS Call Automation 1.6.0. |
| 2026-09-25 | U00 | Governance set up | — | Partner/builder agents, CLAUDE.md session protocol, STATUS.md, DECISIONS.md, one unit per session/branch/PR (D-009 to D-015). |
| 2026-09-25 | U00 | Opus review of governance docs, 2 rounds | — | 20 findings fixed. Added D-016 (merge commits, upstream out of review scope), D-017 (status inside the unit PR) and D-018 (status-only PRs, branch-resume rules). |
| 2026-09-25 | U00 | Q-001 answered; PR #1 opened and merged | [#1](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/1) | Founder: builder merges its own PRs (D-019). Branch `docs/governance-and-bridge-plan` deleted after merge. |
| 2026-09-25 | U01 | Upstream accelerator imported | — | `git merge upstream/main --allow-unrelated-histories`, upstream SHA `a4f40bc`. README conflict resolved (ours kept; theirs moved to `docs/ACCELERATOR_README.md`). |
| 2026-09-25 | U01 | SDK pins + pytest harness added, 2 Opus review rounds | [#2](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/2) | `azure-ai-voicelive>=1.3.0,<2`, `azure-communication-callautomation>=1.6.0,<2`. Review found a real bug (pytest's prepend import mode loaded the wrong `server` module — fixed with `--import-mode=importlib`) plus README-link and log-capture fixes. Smoke test grew from 1 to 3 planned tests to actually exercise `load_server`. Second round: 3 low-severity nits (env isolation, conftest double-import risk, caplog docstring), all fixed. |
| 2026-09-25 | U02 | `mask_number()` added, 1 Opus review round | [#3](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/3) | No real defects found. Added D-021 (the masking rule's correct decision entry, correcting U02's own commit message which had miscited D-006). Added boundary (4/5-digit) and int-input tests per the review's low-severity notes; skipped two theoretical-only notes (non-phone rawId cosmetic mislabeling, float input). Test tally grew from 7 planned to 10. |

---

## §3 Open questions

Status values: `OPEN` · `ANSWERED` (record the answer and link a D-NNN if it became a decision).

| ID | Question | Owner | Blocks | Status |
| --- | --- | --- | --- | --- |
| Q-001 | Who merges PRs? | Founder | Closing every unit, U00 onwards | ANSWERED: the builder merges its own PR once Opus review, `cso` and tests all pass, then deletes the branch (see D-019). |
| Q-002 | Buy the ACS test number (Option B subscription). | Founder + Cowork | M6 | OPEN |
| Q-003 | Confirm the region of `hireastra-resource`. | Cowork | M6 | OPEN |
| Q-004 | Bridge identity at deploy: keep the accelerator's user-assigned identity, or switch to system-assigned (spec §6)? Whichever is chosen gets Foundry User. | Founder | M6 | OPEN |
| Q-005 | Verify the ACS callback JWT issuer, JWKS URL and audience before enabling `ACS_CALLBACK_JWT_AUDIENCE`. | Builder at M6 | M6 | OPEN |
| Q-006 | Run `az login` + `azd auth login` on the deploy machine. | Founder | M6 | OPEN |
