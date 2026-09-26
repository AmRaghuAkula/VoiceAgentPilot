# Telephony Bridge — Milestones

Last updated: 2026-09-25 · Plan: [2026-09-25-telephony-bridge-step3.md](2026-09-25-telephony-bridge-step3.md) · Spec: [../specs/2026-09-25-telephony-bridge-design.md](../specs/2026-09-25-telephony-bridge-design.md) · **Live status: [../../STATUS.md](../../STATUS.md)**

**End state of the plan:** the Step 3 bridge code is merged into `main`. That means Microsoft's accelerator is imported, and all 10 spec modifications are built and unit-tested without needing Azure. Deploy (Step 4) and the live acceptance tests come after, and are listed below as M6–M8 so the full path to the pilot is visible.

## How work is cut (D-011, D-012 in [DECISIONS.md](../../DECISIONS.md))

- **The unit of work is one plan task.** Each unit gets **one session, one branch and one PR**. There is no bundling: a session ends when its unit's PR has merged and its branch has been deleted.
- **Milestones group those units** so progress is easy to read. A milestone is done when all of its units have merged and its definition of done below holds on `main`.
- **Every unit's PR runs the full lifecycle in [CLAUDE.md](../../../CLAUDE.md) §5:** Opus code review → `cso` on Opus → auto-opened PR → partner's status commit on the same branch → merge commit → delete the branch → daily email.
- **Only one branch exists at a time.** The next unit's branch is cut from `main` only after the previous one has merged and been deleted.
- **Live status lives only in [STATUS.md](../../STATUS.md):** unit status, PR numbers, test results and review results. This file holds definitions (what "done" means and what is tested), not live status, so the two can't drift apart.

## Units and branches

| Unit | Plan task | Milestone | Branch | Tests added |
| --- | --- | --- | --- | --- |
| U00 | — (governance, spec, plan) | — | `docs/governance-and-bridge-plan` | 0 |
| U01 | Task 0 — toolchain, upstream import, harness | M0 | `feat/tb-t00-foundation` | 1 |
| U02 | Task 1 — number masking | M1 | `feat/tb-t01-log-mask` | 7 |
| U03 | Task 2 — routing primitives | M1 | `feat/tb-t02-routing` | 20 |
| U04 | Task 3 — bridge config and validation | M1 | `feat/tb-t03-bridge-config` | 34 |
| U05 | Task 4 — server wiring and web-client guard | M1 | `feat/tb-t04-server-wiring` | 5 |
| U06 | Task 5 — expiry reasons and cap/idle hooks | M2 | `feat/tb-t05-expiry-hooks` | 4 |
| U07 | Task 6 — Voice Live agent mode | M2 | `feat/tb-t06-voicelive-agent-mode` | 11 |
| U08 | Task 7 — signing and call session lifecycle | M3 | `feat/tb-t07-call-session` | 31 |
| U09 | Task 7.5 — fail closed if web client + telephony both active (D-028) | M3 | `feat/tb-t075-webclient-guard` | not yet written; small, see task text below |
| U10 | Task 8 — IncomingCall answer and callbacks | M4 | `feat/tb-t08-bridge-calls` | 12 |
| U11 | Task 9 — ACS media handler bound to session | M4 | `feat/tb-t09-acs-media-handler` | 9 |
| U12 | Task 10 — callback JWT and ACS routes | M4 | `feat/tb-t10-acs-routes` | 17 |
| U13 | Task 11 — docs and config sample | M5 | `feat/tb-t11-docs` | 0 (runs the full ~151-test suite + 2 grep checks) |
| U14 | M6 Unit 1 — Bicep fixes (existing AI resource, system-assigned identity, scale, Key Vault media token, Foundry User role) | M6 | `feat/tb-m6-u14-infra-fixes` | n/a (infra: `az bicep build` + `what-if`, see [M6 plan](2026-09-27-m6-deploy-plan.md)) |
| U15 | M6 Unit 2 — `azd up`, verify, VoIP-only smoke test | M6 | `feat/tb-m6-u15-deploy` | n/a (infra: `az`/`azd` verification commands, see [M6 plan](2026-09-27-m6-deploy-plan.md)) |
| U16 | M6 Unit 3 — Event Grid number-filter resolution (BLOCKED on Q-002) | M6 | not yet cut | n/a |

**Test readiness today:** all ~151 original unit tests are **written out in full in the plan**, but as of U08's merge, 200 exist and pass in the repo (the plan's estimates have grown at every reviewed unit so far — see STATUS.md §1). U09 is new, added 2026-09-27 (see below), not in the original ~151 count. The 9 live acceptance tests (spec §8) can't run until M6.

**U09 was inserted after the original plan was written.** It answers Q-007/implements D-028 (see DECISIONS.md): the bridge must refuse to start, not just warn, if `ENABLE_WEB_CLIENT=true` and a telephony provider is active at the same time. It has no "Task 7.5" text in the step3.md plan document (that file's Task numbering is untouched) — the builder implements it directly against `server/server.py`'s existing structure (see U05's Task 4 for the pattern: `BridgeConfigError` → log → `sys.exit(1)`), writes its own small test file, and follows the same TDD/review/PR lifecycle as every other unit.

---

## M0 — Foundation (U01)

**Definition of done:**
- `uv` is installed and a Python 3.12 environment builds.
- Microsoft's accelerator is merged in *with its git history*, and the `upstream` remote is set.
- The README merge conflict is resolved: ours stays at the top, and theirs moves to `docs/ACCELERATOR_README.md`.
- The SDK minimums are pinned: Voice Live ≥1.3.0 and ACS Call Automation ≥1.6.0.
- `pytest` runs and the smoke test passes.

**Test coverage:** 1 smoke test (the upstream modules import cleanly).

## M1 — Config and routing core (U02–U05)

**Definition of done:**
- Phone numbers are masked to `***1234` everywhere they're logged.
- The called number is normalized from any format (including ACS `4:+1…` IDs) and matched to a route.
- The routing config is validated at startup. It **refuses to start** on:
  - a typo'd number;
  - duplicate numbers;
  - an agent version that isn't a pinned string of digits;
  - a missing secret;
  - a missing Cognitive Services endpoint.
- The spec's variable names win over the accelerator's (`MAX_CALL_SECONDS`, `VOICE_LIVE_ENDPOINT`).
- The unauthenticated web debug page is **off by default**.

**Test coverage (66):**

| Area | Tests |
| --- | --- |
| Masking | 7 |
| Routing | 20 |
| Config validation | 34 |
| Server wiring | 5 |

## M2 — Voice Live agent mode (U06–U07)

**Definition of done:**
- Voice Live connects in **agent mode** to the configured project and agent at the pinned version. It uses Entra ID only; there is no API key on this path.
- The bridge sends **no** instructions, voice or turn-detection settings. Foundry stays the only source of Alex's behavior.
- The Voice Live `session_id` and `conversation_id` are logged, so a phone call can be matched to its Foundry trace.
- Reaching the call limit or going idle triggers the right hook.
- A deliberate close is never mistaken for a crash.

**Test coverage (15):**

| Area | Tests |
| --- | --- |
| Expiry reasons and hooks | 4 |
| Agent-mode connect, session contents, id logging, drop detection, force close | 11 |

## M3 — Call lifecycle (U08–U09)

This is the part the four design reviews focused on.

**Definition of done:**
- Every way a call can end goes through one path that acts only once.
- The caller is **never left in silence**: every failure plays the fallback message and then hangs up, with a 15 s safety net.
- The Voice Live session closes within about 5 s, force-closed if it stalls, and it is closed exactly once.
- The goodbye at the call limit plays immediately.
- Every call is removed from memory when it ends.
- The signed per-call URLs reject tampering without crashing.
- **(U09, added 2026-09-27) The bridge refuses to start — does not merely warn — if the unauthenticated web debug client and a telephony provider are configured active at the same time (D-028).**

**Test coverage (31 original + U09's new tests):**

| Area | Tests |
| --- | --- |
| URL signing | 9 |
| Lifecycle, including every race condition from the design reviews | 22 |
| Web-client/telephony mutual-exclusion startup guard (U09) | new, written test-first by the builder |

## M4 — Phone-call integration and security (U10–U12)

**Definition of done:**
- Incoming calls are answered by route. A known number is connected to the agent; an unknown number hears the fallback, then the call ends.
- The callback and audio URLs carry per-call signatures and contain **no phone numbers or secrets**.
- The audio WebSocket rejects unknown, forged, reused or already-ended connections. Callbacks with a bad signature are rejected with no side effects.
- The ACS callback token check works when enabled.
- A background sweep clears abandoned calls.

**Test coverage (38):**

| Area | Tests |
| --- | --- |
| Answer and callbacks | 12 |
| ACS audio handler | 9 |
| Callback token check | 9 |
| End-to-end routes | 8 |

## M5 — Docs and final polish (U13)

**Definition of done:**
- The README documents:
  - the upstream version;
  - the Voice Live API version;
  - the variable-name aliases;
  - security notes;
  - how to run the tests.
- `.env.sample` lists every new setting.
- **The full suite passes (~151 tests).**
- A code search finds no real-estate words, agent names or phone numbers in application code.

Once U13 merges, `main` is the complete Step 3 bridge.

---

## After this plan (blocked; listed for visibility)

### M6 — Deploy to Azure (Step 4)

**Unblocked ahead of the ACS phone number (D-029, 2026-09-27).** Q-003 (region: East US 2), Q-004 (system-assigned identity), Q-005 (verify JWT at deploy time) and Q-006 (`az login`/`azd auth login`) are all answered — see [STATUS.md](../../STATUS.md) §3. Q-002 (the ACS number itself) remains open but no longer blocks M6; it blocks only M7 (live acceptance tests).

**Full implementation plan:** [2026-09-27-m6-deploy-plan.md](2026-09-27-m6-deploy-plan.md) — units U14–U16, definition of done, verification steps, and the Q-011 (VoIP-only validation) analysis all live there rather than being duplicated here.

**Units and branches (see the plan doc for full detail):**

| Unit | What | Branch | Status |
| --- | --- | --- | --- |
| U14 | Fix accelerator Bicep to match spec: existing AI resource, system-assigned identity, Container App scale, Key Vault media token, Foundry User role | `feat/tb-m6-u14-infra-fixes` | QUEUED |
| U15 | `azd up`, verify infra, run the VoIP-only smoke test (Q-011) | `feat/tb-m6-u15-deploy` | QUEUED |
| U16 | Event Grid number-filter resolution (BLOCKED on Q-002) | not yet cut | BLOCKED |

**Definition of done:** see the plan doc's "Definition of done and test/verification coverage" section — it supersedes the short list that used to be here, since three of the six original bullets needed correction once the actual Bicep was read (see the plan's "What the repo actually contains today" section for the AI-resource, identity and Event-Grid-filter gaps found).

### M7 — Live acceptance tests (spec §8)

**Definition of done:** you call the test number, and Cowork pulls the trace and logs for each test.

1. Alex greets within 3 s.
2. A 2-minute conversation completes and its transcript shows in Foundry under v10.
3. Barge-in works.
4. There's no filler on fast replies.
5. "buy" vs "bye" and a 10-digit number come through correctly.
6. After hang-up, the Voice Live session closes within 5 s.
7. A wrong version triggers the fallback.
8. A 60 s cap ends the call with the goodbye.
9. Latency is measured against the ~5.9 s baseline.

**Readiness:** the bridge logs needed for tests 6–8 are built into M3/M4.

### M8 — Pilot passes

**Definition of done:** acceptance tests 1–8 pass. Test 9 is a measurement only.
