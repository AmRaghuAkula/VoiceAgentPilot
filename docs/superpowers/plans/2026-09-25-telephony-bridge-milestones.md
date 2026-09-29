# Telephony Bridge — Milestones

Last updated: 2026-09-28 (M6 re-planned for Twilio, D-042/D-043) · Plan: [2026-09-25-telephony-bridge-step3.md](2026-09-25-telephony-bridge-step3.md) · Spec: [../specs/2026-09-25-telephony-bridge-design.md](../specs/2026-09-25-telephony-bridge-design.md) · **Live status: [../../STATUS.md](../../STATUS.md)**

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
| U14a | M6 — existing AI resource (cross-subscription), ACS text-to-speech wiring, Container App scale fix | M6 | `feat/tb-m6-u14a-existing-ai-resource` | n/a (infra: `az bicep build` + `azd provision --preview`, see [M6 plan](2026-09-27-m6-deploy-plan.md) — Q-012/Q-014/Q-016/Q-017 all answered, clear to start) |
| U14b | M6 — Container App identity: reconfirm D-031 for Twilio, narrow Key Vault to Secrets User; first deletes stale ACS azd envs (plan rev 5.1, D-042); **Opus** | M6 | `feat/tb-m6-u14b-identity-least-privilege` | n/a (infra: `az role definition list` + Twilio-mode `azd provision --preview -e`, see [M6 plan](2026-09-27-m6-deploy-plan.md)) |
| U14c | M6 — secrets hygiene (`server/.dockerignore` excludes `.env`) + container/ACR sizing (founder's cost call); `MEDIA_WS_TOKEN` dropped (ACS-only) | M6 | `feat/tb-m6-u14c-secrets-and-sizing` | n/a (infra) |
| U14d | M6 — RESOLVED 2026-09-27, folded into U14a | M6 | — | n/a |
| ~~U-CFG~~ | RETIRED 2026-09-27 (D-031) — `config_validator.py`'s existing check already passes under D-031's chosen identity design; no hook needed | M6 | — | n/a |
| U15 | M6 — fresh azd env, provision→deploy, pre-cutover verification (never touches the real Twilio number); prerequisites UT01a/UT01b, not U10–U13 | M6 | `feat/tb-m6-u15-deploy` | n/a (infra: `az`/`azd` verification + HTTP probes) |
| U15b | M6 — real-number cutover + live Twilio smoke test (after Friday's demo, founder's timeline — Q-045 (b), D-043) | M6 | `feat/tb-m6-u15b-cutover` | n/a (live calls: UT02 criteria + long call + masking) |
| ~~U16~~ | RETIRED 2026-09-28 (D-041) — ACS Event Grid number filter; no ACS number, no Event Grid on the Twilio path | M6 | — | n/a |

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

**Re-planned for Twilio (2026-09-28, D-041/D-042/D-043).** Twilio, not ACS, is the permanent phone number provider (D-041). M6 deploys the already-merged Twilio call path (UT01a/UT01b: `POST /voice` → `/twilio/ws` → Voice Live agent mode) to the Container App — it is still the priority, because it removes Q-040's laptop/tunnel single point of failure. D-031's identity shape (one user-assigned identity for registry pull, Key Vault, and Voice Live/Foundry User) is unchanged. Q-045 is answered (b): Friday's 2026-10-02 demo stays on the laptop path, and the real-number cutover (U15b) happens after the demo on the founder's timeline; the staging-number option is deferred.

**Full implementation plan (rev 5.1):** [2026-09-27-m6-deploy-plan.md](2026-09-27-m6-deploy-plan.md) — units, standing deploy rules, verification steps, and DoD live there rather than being duplicated here. Rev 4's ACS-era text is preserved in git at `0608658`.

**Standing deploy rules (D-042):** `-e <env>` on every azd command; first check `azd env get-value TELEPHONY_PROVIDER -e <env>` prints `twilio`; `azd provision` always immediately followed by `azd deploy` (provision resets the app to a hello-world placeholder); never `azd down`; never deploy during a live call.

**Units and branches (see the plan doc for full detail):**

| Unit | What | Branch | Status |
| --- | --- | --- | --- |
| U14a | Existing AI resource (cross-subscription), Container App scale fix; its ACS-specific Bicep is dormant under `TELEPHONY_PROVIDER=twilio` | `feat/tb-m6-u14a-existing-ai-resource` | CLOSED (#33) |
| U14b | Delete stale ACS azd envs; Key Vault Secrets Officer → Secrets User; Twilio-mode preview confirms D-031 shape and no ACS resources; **Opus** | `feat/tb-m6-u14b-identity-least-privilege` | NEXT |
| U14c | `.dockerignore` excludes `.env`; container/ACR sizing (founder's cost call) | `feat/tb-m6-u14c-secrets-and-sizing` | QUEUED |
| U14d | RESOLVED 2026-09-27, folded into U14a | — | RETIRED |
| ~~U-CFG~~ | RETIRED 2026-09-27 (D-031) | — | RETIRED |
| U15 | Fresh env, preview hard gate, provision→deploy (C1 first-deploy proof), pre-cutover verification incl. `/acs/incomingcall` → 404; never touches the real number | `feat/tb-m6-u15-deploy` | QUEUED |
| U15b | Real-number cutover (full voice-config record, fresh-tunnel rollback) + live Twilio smoke test (UT02 criteria, long call, called/caller masking, Twilio Debugger); after Friday's demo per Q-045 (b) | `feat/tb-m6-u15b-cutover` | QUEUED |
| ~~U16~~ | RETIRED 2026-09-28 (D-041) | — | RETIRED |

**Definition of done:** see the plan doc's "Definition of done and verification coverage" section. M6 closes when **both U15 and U15b** have merged. Removed from the ACS-era DoD: the Event Grid subscription, the ACS text-to-speech link, the callback JWT check (Q-005/Q-043 not applicable — no ACS routes registered), and the VoIP-only smoke test (Q-011 superseded).


### M7 — Live acceptance tests (spec §8)

**Definition of done:** you call the Twilio number (D-041), and Cowork pulls the trace and logs for each test.

1. Alex greets within 3 s.
2. A 2-minute conversation completes and its transcript shows in Foundry under v10.
3. Barge-in works.
4. There's no filler on fast replies.
5. "buy" vs "bye" and a 10-digit number come through correctly.
6. After hang-up, the Voice Live session closes within 5 s.
7. A wrong version triggers the fallback.
8. A 60 s cap ends the call with the goodbye.

**Re-scope needed before M7 (M6 plan T10):** tests 7 and 8 assume ACS's spoken fallback/goodbye; on the Twilio path a connect failure or the call cap ends the call silently by design (only a route miss speaks, via `<Say>`).
9. Latency is measured against the ~5.9 s baseline.

**Readiness:** the bridge logs needed for tests 6–8 are built into M3/M4.

### M8 — Pilot passes

**Definition of done:** acceptance tests 1–8 pass. Test 9 is a measurement only.
