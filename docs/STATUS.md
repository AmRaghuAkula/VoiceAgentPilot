# Status

The **first file every session reads**. It is the only place live status is tracked. Definitions (what "done" means and what each unit tests) are in the [milestone doc](superpowers/plans/2026-09-25-telephony-bridge-milestones.md). The partner updates it **inside each unit's PR, before the PR merges** (D-017). When no unit is in flight, updates go in a status-only PR (D-018). See [CLAUDE.md](../CLAUDE.md) §3 and §5 step 8.

Last updated: 2026-09-26

---

## §1 Unit dashboard

Status values: `NEXT` (the one unit the next session works on; its branch may exist only if its PR is open) · `CLOSED` (PR merged and branch deleted) · `QUEUED` · `BLOCKED`.

This table is updated **inside each unit's own PR** (D-017), so what `main` shows is always current.

| Unit | Plan task | Milestone | Status | Branch | PR | Tests (pass/planned) | Opus review | cso |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| U00 | Governance, spec, plan | — | CLOSED | `docs/governance-and-bridge-plan` | [#1](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/1) | 0/0 | 2 rounds; findings fixed | n/a (docs only) |
| U01 | Task 0 — foundation | M0 | CLOSED | `feat/tb-t00-foundation` | [#2](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/2) | 3/3 | 2 rounds; findings fixed | n/a (import/config/test scaffolding, no app security surface) |
| U02 | Task 1 — log mask | M1 | CLOSED | `feat/tb-t01-log-mask` | [#3](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/3) | 10/10 | 1 round; clean, low-severity notes only | n/a (pure string logic, no security surface) |
| U03 | Task 2 — routing | M1 | CLOSED | `feat/tb-t02-routing` | [#4](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/4) | 38/20 (grew via review) | 2 rounds; real bugs fixed | n/a (pure parsing/validation, no security surface) |
| U04 | Task 3 — bridge config | M1 | CLOSED | `feat/tb-t03-bridge-config` | [#5](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/5) | 92/34 (grew via review) | 4 rounds; real bugs fixed each round | n/a (validation logic, no network/auth surface of its own — but see D-023) |
| U05 | Task 4 — server wiring | M1 | CLOSED | `feat/tb-t04-server-wiring` | [#6](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/6) | 149/5 (grew via review) | 4 rounds; real bugs fixed (D-024) | 2 rounds; 1 real finding fixed (D-024) |
| U06 | Task 5 — expiry hooks | M2 | CLOSED | `feat/tb-t05-expiry-hooks` | [#7](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/7) | 153/4 (149 pre-existing + 4 new) | 1 round; clean, low-severity notes only | 1 round; clean, 1 medium latent finding logged as Q-008 |
| U07 | Task 6 — Voice Live agent mode | M2 | NEXT | `feat/tb-t06-voicelive-agent-mode` | — | 0/11 | — | — |
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
| M1 | complete (U02–U05 all CLOSED) |
| M2 | in progress (U06 CLOSED, U07 NEXT) |
| M3 | not started |
| M4 | not started |
| M5 | not started |
| M6–M8 | blocked (§3: Q-002 to Q-006) |

**Unit tests passing overall:** 153 / ~250 (running total, `pytest --collect-only`). U01: 1 planned → 3; U02: 7 planned → 10; U03: 20 planned → 38; U04: 34 planned → 92 (four review rounds on the startup validator — see §2); U05: 5 planned → 5 (+1 regression test landed in U04's `test_bridge_config.py` for a `cso` finding, so the file-level total this unit touched is 149); U06: 4 planned → 4. Later units' "planned" counts in this table are the plan's original estimates and will likely grow the same way once reviewed.

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
| 2026-09-25 | U03 | `routing.py` added, 2 Opus review rounds | [#4](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/4) | Round 1 found real bugs: the NANP regex accepted dropped-digit keys as valid (silent route miss with no startup error — see D-022), `normalize_number` could turn garbage/extensions/GUID rawIds into plausible-looking numbers, regexes were Unicode-digit and trailing-newline permissive, and event extraction raised on non-dict identifiers. All fixed. Round 2 confirmed the fixes hold and found only low-severity nits (event-payload-level guard, non-string phoneNumber.value, exact boundary test); fixed the cheap ones, left one intentional (non-ASCII whitespace fails loud, matching spec). Test tally grew from 20 planned to 38. |
| 2026-09-25 | U04 | `bridge_config.py` added, 4 Opus review rounds | [#5](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/5) | This is the startup gate D-022 was written to protect, so it got extra scrutiny (all reviews dispatched as Opus subagents from a Sonnet session, per CLAUDE.md §5). Round 1 found fail-quiet gaps: non-ASCII/leading-zero version strings, whitespace-padded secrets passing the required-value check, `inf`/`nan` timeouts, D-003 precedence broken by whitespace-only values, uncaught exceptions escaping past `BridgeConfigError`. Round 2 found round 1's own duplicate-key masking fix had *regressed* D-021 (a digit-bearing-but-not-strictly-E.164 duplicate key leaked unmasked) plus an `OverflowError` on huge integers. Round 3 found the masking fix still missed non-consecutive-digit keys, a `_deep_freeze` recursion boundary sitting outside the try block, `1e999`-style float overflow bypassing the NaN/Infinity guard, and an ungrounded shared numeric ceiling. Round 4 found round 3's own exception-catch narrowing had reintroduced the exact uncaught-exception bug round 1 fixed (Python's 4300-digit integer-string-conversion limit raises a plain `ValueError`). All fixed; round 4 confirmed the unit "ready to ship" once that one fix landed. See D-023. Test tally grew from 34 planned to 92. |
| 2026-09-25 | U05 | `server.py` wired to `load_bridge_config()`, web client guarded, 4 Opus + 2 cso rounds | [#6](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/6) | All reviews dispatched as Opus subagents from a Sonnet session, per CLAUDE.md §5. Opus round 1 found a real D-007 gap: unregistering `/web/ws` and `/` left Quart's default static route still serving the web client's HTML/JS regardless of `ENABLE_WEB_CLIENT` — fixed by making `static_folder` itself conditional. Round 2 found the new env vars this unit made load-bearing were missing from `server/.env.sample` (a DoD item) — fixed. Round 3 found the newly-added `.env.sample` placeholders for `MEDIA_WS_TOKEN`/`AGENT_ROUTING_JSON` were left uncommented and long/valid enough to silently pass startup validation if copied as-is — fixed (commented out, matching every other optional value in the file). Round 4: clean. `cso` round 1 found `BridgeConfig`'s auto-repr would leak `media_ws_token` now that the object is reachable via `app.config["BRIDGE"]` — fixed with `field(repr=False)` plus a regression test in `test_bridge_config.py`. `cso` round 2: clean. See D-024. Five further items (acs_active detection method, unvalidated `MAX_CONCURRENT_CALLS`/`CALL_IDLE_TIMEOUT`, an upstream error-message mismatch, no code guard against `ENABLE_WEB_CLIENT`+ACS coexisting, and upstream's own `0.0.0.0` bind) were deferred rather than fixed in-unit — see D-025 and Q-007. Test tally: 5 planned → 5, plus 1 regression test landed in U04's test file for the `cso` finding. M1 is now complete (U02–U05 all closed). |
| 2026-09-26 | U06 | `is_expired()` now returns a reason string; `on_call_cap`/`on_idle` hooks added, 1 Opus + 1 cso round | [#7](https://github.com/AmRaghuAkula/VoiceAgentPilot/pull/7) | Both reviews dispatched as Opus subagents from a Sonnet session, per CLAUDE.md §5, scoped to `main...HEAD`. Opus code-review: clean, only 4 low-severity notes (no test for a hook that raises, no test pinning duration-before-idle precedence, no `-> None` annotation on the base hooks, calling unbound methods with `object()` in the no-op test) — none blocking, left as-is since Task 5's scope is exactly what the plan specifies. `cso`: no exploitable issue in this diff (today's hooks are no-ops; log content unchanged; no auth/secret/network surface touched), but flagged a latent medium finding: `run_call_loop` awaits `on_call_cap()`/`on_idle()` with no timeout before the `finally` cancels the Voice Live task and before the caller releases the call slot, so once a future telephony subclass gives these hooks real I/O (a hangup call, a goodbye-message send), a hang there would leak the call slot and keep billing the Voice Live session — an operational DoS risk. Not fixed in this unit because Task 5's hooks are no-ops with no I/O; logged as Q-008 for whichever unit adds real hook bodies. Test tally: 4 planned → 4 (149 pre-existing + 4 = 153 total). M2 is now in progress. |

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
| Q-007 | Should the bridge refuse to start when `ENABLE_WEB_CLIENT=true` and a telephony provider (e.g. ACS) is active at the same time, instead of only warning? Today (U05) it warns and starts. D-007 says the web client is "never set in a deployed environment," but nothing in code enforces that beyond the env var itself. See D-025. | Founder/partner | M6 (should be settled before deploy) | OPEN |
| Q-008 | `run_call_loop` (U06) awaits `handler.on_call_cap()` / `handler.on_idle()` with no timeout before the `finally` cancels the Voice Live task and before the caller releases the call slot (`cso` finding, PR #7). Today both hooks are no-ops so there is no live exposure, but once a telephony subclass gives them real I/O (a hangup call, a goodbye-message send), a hang there would leak the call slot and keep billing the Voice Live session. Should the unit that adds real hook bodies (or a follow-up) wrap the hook calls in `asyncio.wait_for` with a short timeout and add a hang/raise regression test? | Partner (sequence into the hook-implementing unit) | Whichever unit gives `on_call_cap`/`on_idle` real bodies | OPEN |
