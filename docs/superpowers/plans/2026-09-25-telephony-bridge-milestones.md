# Telephony Bridge — Milestones

Last updated: 2026-09-25 · Plan: [2026-09-25-telephony-bridge-step3.md](2026-09-25-telephony-bridge-step3.md) · Spec: [../specs/2026-09-25-telephony-bridge-design.md](../specs/2026-09-25-telephony-bridge-design.md)

**End state of the plan:** the Step 3 bridge code is merged into `main`. That means Microsoft's accelerator is imported, and all 10 spec modifications are built and unit-tested without needing Azure. Deploy (Step 4) and the live acceptance tests come after, and are listed below as M6–M8 so the full path to the pilot is visible.

**Review and merge cadence:** every milestone (M0–M5) is its own work unit and ships as its own branch and PR — not one big PR at the end. Each milestone's branch goes through the standing pipeline before it merges into `main`: Opus code review → `cso` security review → auto-opened PR. The next milestone branches off `main` only after the previous one has merged, so each PR is small and reviewed close to where the code was written.

## Status at a glance

| # | Milestone | Plan tasks | Tests planned | Tests passing | Opus review | cso review | Status | PR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M0 | Foundation: toolchain, upstream import, test harness | 0 | 1 | 0 | Not run | Not run | Not started | not yet opened |
| M1 | Config and routing core | 1–4 | 66 | 0 | Not run | Not run | Not started | not yet opened |
| M2 | Voice Live agent mode | 5–6 | 15 | 0 | Not run | Not run | Not started | not yet opened |
| M3 | Call lifecycle (hang-up, fallback, call cap) | 7 | 31 | 0 | Not run | Not run | Not started | not yet opened |
| M4 | Phone-call integration and security | 8–10 | 38 | 0 | Not run | Not run | Not started | not yet opened |
| M5 | Docs and final polish | 11 | full suite (~151) + 2 checks | 0 | Not run | Not run | Not started | not yet opened |
| M6 | Deploy to Azure (Step 4) | outside this plan | — | — | — | — | **Blocked:** ACS number not bought | — |
| M7 | Live acceptance tests 1–9 | outside this plan | 9 live tests | 0 | — | — | Blocked on M6 | — |
| M8 | Pilot passes | — | tests 1–8 pass | — | — | — | Blocked on M7 | — |

**PR plan:** six PRs, one per milestone, each from a short-lived branch off the then-current `main`, merged before the next milestone starts:

| Milestone | Branch |
| --- | --- |
| M0 | `feat/telephony-bridge-m0-foundation` |
| M1 | `feat/telephony-bridge-m1-config-routing` |
| M2 | `feat/telephony-bridge-m2-voicelive-agent-mode` |
| M3 | `feat/telephony-bridge-m3-call-lifecycle` |
| M4 | `feat/telephony-bridge-m4-acs-integration` |
| M5 | `feat/telephony-bridge-m5-docs-polish` |

The design spec and this milestone doc land in the M0 PR, since nothing else can start without them. PR numbers are added to the table above as your review pipeline opens each one. M6 deploy changes (Bicep, config) get their own PR later, outside this plan.

**Branch discipline:** only one of these branches exists at a time. Each is cut from `main` after the previous milestone's branch has merged **and been deleted** — never cut the next branch while the current one is still open. This repo now has two dedicated subagents enforcing the planner/builder split and this branch discipline: `voice-agent-partner` (`.claude/agents/voice-agent-partner.md`) owns sequencing and specs and never writes code; `voice-agent-builder` (`.claude/agents/voice-agent-builder.md`) owns implementation, runs the Opus review + `cso` pipeline per milestone, opens the PR, and deletes the branch once it merges — and never starts a milestone without sign-off from the partner and, where HANDOFF.md requires it, the founder.

**Test readiness today:** all ~151 unit tests are **written out in full in the plan**, but **none exist in the repo or have run yet**. No code has been implemented. The 9 live acceptance tests (spec §8) can't run until M6.

---

## M0 — Foundation

**Covers:** plan Task 0.

**Definition of done:**
- `uv` is installed and a Python 3.12 environment builds.
- Microsoft's accelerator is merged in *with its git history*, and the `upstream` remote is set.
- The README merge conflict is resolved: ours stays at the top, and theirs moves to `docs/ACCELERATOR_README.md`.
- The SDK minimums are pinned: Voice Live ≥1.3.0 and ACS Call Automation ≥1.6.0.
- The test runner works: `pytest` runs and the smoke test passes.

**Test coverage:** 1 smoke test (upstream modules import cleanly).

**Readiness:** written in the plan; not run yet.

**Branch/PR:** `feat/telephony-bridge-m0-foundation`. **Review and merge:** Opus code review, then `cso` security review, then the PR auto-opens against `main`, per the standing pipeline. Merges before M1 starts.

## M1 — Config and routing core

**Covers:** plan Tasks 1–4.

**Definition of done:**
- Phone numbers are masked to `***1234` everywhere they're logged.
- The called number is normalized from any format (including ACS `4:+1…` IDs) and matched to a route.
- The routing config is validated at startup. It **refuses to start** on:
  - a typo'd number;
  - duplicate numbers;
  - an agent version other than a pinned string of digits (`"latest"`, `"Latest"` and integers are all rejected);
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
| Server startup wiring | 5 |

**Readiness:** written in the plan; not run yet.

**Branch/PR:** `feat/telephony-bridge-m1-config-routing`, branched from `main` after M0 merges. **Review and merge:** Opus code review, then `cso`, then auto-PR against `main`. Merges before M2 starts.

## M2 — Voice Live agent mode

**Covers:** plan Tasks 5–6.

**Definition of done:**
- Voice Live connects in **agent mode** to the configured project and agent at the pinned version. It uses Entra ID only; there is no API key on this path.
- The bridge sends **no** instructions, voice or turn-detection settings, so Foundry stays the only source of Alex's behavior.
- The Voice Live `session_id` and `conversation_id` are logged, so a phone call can be matched to its Foundry trace.
- A call reaching the time limit or going idle calls the right hook.
- A deliberate close is never mistaken for a crash.

**Test coverage (15):**

| Area | Tests |
| --- | --- |
| Expiry reasons and call-loop hooks | 4 |
| Agent-mode connect, session contents, id logging, drop detection, force close | 11 |

**Readiness:** written in the plan; not run yet.

**Branch/PR:** `feat/telephony-bridge-m2-voicelive-agent-mode`, branched from `main` after M1 merges. **Review and merge:** Opus code review, then `cso`, then auto-PR against `main`. Merges before M3 starts.

## M3 — Call lifecycle

**Covers:** plan Task 7. This is the part the four design reviews focused on.

**Definition of done:**
- Every way a call can end goes through one path that acts only once.
- The caller is **never left in silence**. Every failure plays the fallback message and then hangs up, and a 15 s safety net forces the hang-up.
- The Voice Live session always closes within about 5 s, force-closed if it stalls, and it is closed exactly once.
- The goodbye at the call limit plays immediately, with no dead air.
- Every call is removed from memory when it ends.
- Signed per-call URL tokens verify correctly and reject tampering without crashing.

**Test coverage (31):**

| Area | Tests |
| --- | --- |
| URL signing | 9 |
| Call lifecycle, including all the race conditions from the design reviews | 22 |

**Readiness:** written in the plan; not run yet.

**Branch/PR:** `feat/telephony-bridge-m3-call-lifecycle`, branched from `main` after M2 merges. **Review and merge:** Opus code review, then `cso`, then auto-PR against `main`. Merges before M4 starts.

## M4 — Phone-call integration and security

**Covers:** plan Tasks 8–10.

**Definition of done:**
- **Answering calls:** incoming calls are answered by route. A known number is connected to the agent; an unknown number hears the fallback and the call ends.
- **What goes in the URLs:** callback and audio URLs carry per-call signatures and contain **no phone numbers or secrets**.
- **Rejected connections:** the audio WebSocket rejects unknown, forged, reused or already-ended connections. Callbacks with a bad signature are rejected with no side effects.
- **Optional token check:** the ACS callback token check works when enabled.
- **Clean-up:** a background sweep clears abandoned calls.

**Test coverage (38):**

| Area | Tests |
| --- | --- |
| Call answering and callback dispatch | 12 |
| ACS audio handler | 9 |
| Callback token check | 9 |
| End-to-end routes | 8 |

**Readiness:** written in the plan; not run yet.

**Branch/PR:** `feat/telephony-bridge-m4-acs-integration`, branched from `main` after M3 merges. **Review and merge:** Opus code review, then `cso`, then auto-PR against `main`. Merges before M5 starts.

## M5 — Docs and final polish

**Covers:** plan Task 11.

**Definition of done:**
- The README documents:
  - the upstream version;
  - the Voice Live API version;
  - the variable-name aliases;
  - security notes;
  - how to run the tests.
- The `.env.sample` lists every new setting.
- **The full test suite passes (about 151 tests, cumulative across M0–M4).**
- A code search finds no real-estate words, agent names or phone numbers in application code.

**Test coverage:** the full suite, plus the 2 code-search checks. No new tests of its own — this milestone is documentation only, run against everything M0–M4 already built.

**Readiness:** depends on M0–M4 having merged.

**Branch/PR:** `feat/telephony-bridge-m5-docs-polish`, branched from `main` after M4 merges. **Review and merge:** Opus code review, then `cso`, then auto-PR against `main`. This is the last PR in the Step 3 plan — once it merges, `main` is the complete Step 3 bridge, ready for M6 (deploy).

---

## After this plan (blocked; listed for visibility)

### M6 — Deploy to Azure (Step 4)

**Blocked on:** buying the ACS phone number (you and Cowork), confirming the `hireastra-resource` region, and running `az login` / `azd auth login` on this machine.

**Definition of done:**
- The bridge is deployed with `azd up` to `rg-hireastra-voice-pilot`, in the same region as `hireastra-resource`, with 1 replica.
- The "Foundry User" role is granted to the bridge's identity. That needs your decision on user-assigned vs system-assigned identity.
- Key Vault holds the ACS connection string and the audio-URL secret.
- An Event Grid subscription is filtered to the test number.
- ACS is linked to the AI resource for spoken fallbacks.
- The ACS callback token check is verified and enabled.
- A $50/month budget alert is set.

**PR:** its own PR, opened when M6 starts.

### M7 — Live acceptance tests (spec §8)

**Definition of done:** you call the test number, and Cowork pulls the Foundry trace and logs for each test:

1. Alex greets within 3 s.
2. A 2-minute conversation completes and its transcript appears in Foundry under version 10.
3. Interrupting Alex works (barge-in).
4. No "let me check" filler on fast replies.
5. "Buy" vs "bye" and a 10-digit number are handled correctly.
6. After hang-up, the Voice Live session closes within 5 s.
7. A wrong agent version plays the fallback message.
8. With the limit set to 60 s, the call ends with the goodbye.
9. Latency is measured against the ~5.9 s browser baseline.

**Readiness:** the bridge logs for tests 6, 7 and 8 (`voicelive_closed_ms`, `call_ended reason=…`) are built into M3/M4. Tests 1–5 and 9 depend on the live system.

### M8 — Pilot passes

**Definition of done:** acceptance tests 1–8 pass. Test 9 is a measurement, not pass/fail.
