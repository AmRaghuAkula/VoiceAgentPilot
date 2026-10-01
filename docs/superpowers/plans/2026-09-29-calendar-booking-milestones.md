# Calendar Booking Tools — Milestones

Last updated: 2026-10-01 (rev 1.2, UC01-F: C1/C4/C5 DoD aligned with plan rev 1.6 and spec rev 3.2; rev 1.1, written in UC00, aligned with plan rev 1.1 after its Opus design review) · Plan: [2026-09-29-calendar-booking-plan.md](2026-09-29-calendar-booking-plan.md) · Spec: [../specs/2026-09-29-calendar-booking-design.md](../specs/2026-09-29-calendar-booking-design.md) (rev 3.2; rev 3.1 founder-accepted, D-051) · **Live status: [../../STATUS.md](../../STATUS.md) §1b**

**End state of this track:** a real phone call through the production bridge reaches an agent that checks real availability on the host's Google calendar, books a slot after an explicit yes, and confirms it; the event appears on the real calendar with the correct title, local time and contact details; and the same call shows one honest-failure path (spec §13.4, test T6). The calendar tool service is agent-agnostic and provider-agnostic: a second agent is a binding plus a tool attachment, and a second calendar vendor is one adapter module that passes the conformance suite.

## How work is cut

- **Same rules as the bridge track** ([telephony-bridge milestones](2026-09-25-telephony-bridge-milestones.md), CLAUDE.md §3–§6): one unit = one plan task = one session = one branch = one PR; one branch at a time, shared with bridge work; status inside the unit's PR; merge commits; daily email.
- **Branchless units** (UC01, UC08a, UC10, UC11, UC12) are verification or ops sessions sanctioned by D-053 and CLAUDE.md §3: no code branch; the session's status branch is cut first (carrying any throwaway-resource list) and merged the same session; throwaway code only in the session scratchpad; redacted evidence only (public repo). A finding that needs code becomes its own branch unit.
- **Build, review, `cso` and verification work runs on Opus** (CLAUDE.md §0, D-034); status commits and PR mechanics follow §0's Sonnet routing.
- **This file holds definitions, not live status.** Unit status, PR numbers, test counts and review results live only in STATUS.md §1b.
- **Both test suites** (bridge and calendar) must pass on every code unit from UC02a on.

## Units and branches

| Unit | Plan task | Milestone | Branch | Kind | Gate (Q-NNN) | Tests added (estimate) |
| --- | --- | --- | --- | --- | --- | --- |
| UC00 | Plan, milestone doc, governance amendments | — | `feat/cal-uc00-plan` | Docs | Spec accepted (D-051) | 0 |
| UC01 | Feasibility and token-claims check | C0 | none (branchless) | Verification | Q-072 (D-052 confirmed), Q-067 (throwaway resources) | 0 (evidence only) |
| UC02a | Package skeleton, bindings, contract document, provider port, fake provider, conformance harness, G1–G3 | C1 | `feat/cal-uc02a-skeleton` | Code | — | ~70 |
| UC02b | HTTP dispatcher and in-code Entra authentication | C1 | `feat/cal-uc02b-http-auth` | Code | — | ~45 |
| UC03 | Slot engine, slot tokens, `check_availability` | C1 | `feat/cal-uc03-check-availability` | Code | — | ~75 |
| UC04a | Identity HMACs, contact normalization, claim-store interface + fake + conformance | C1 | `feat/cal-uc04a-claim-store-core` | Code | — | ~45 |
| UC04b | Booking algorithm (conservative interim), `book_appointment`, event content, races, G4 | C1 | `feat/cal-uc04b-book-appointment` | Code | — | ~60 |
| UC04c | Recovery, verification, uncertain-create path | C1 | `feat/cal-uc04c-recovery` | Code | — | ~40 |
| UC05 | Azure identity and data plane | C2 | `feat/cal-uc05-azure-platform` | IaC + ops | Q-067 | n/a (verification commands) |
| UC06 | Azure Blob claim store | C2 | `feat/cal-uc06-blob-claim-store` | Code | — | ~30 (+ live) |
| UC07 | Google credential source, Key Vault `KeyRing` and consent tool | C3 | `feat/cal-uc07-google-credentials` | Code | Q-065 | ~40 |
| UC08a | Google project, consent screen, OAuth client, test-calendar consent | C3 | none (branchless) | Ops | Q-064, Q-066 | 0 (evidence only) |
| UC08b | Google adapter and live Google checks | C3 | `feat/cal-uc08b-google-adapter` | Code + live checks | — | ~40 (+ ~10 live) |
| UC09 | Function deploy | C4 | `feat/cal-uc09-function-deploy` | IaC + deploy | (Q-067 always-ready, decided on UC09's measurement) | ~10 |
| UC10 | Real-calendar consent, test-agent wiring, T1–T5 | C4 | none (branchless) | Ops + verification | Q-069 | 0 (T1–T5 evidence) |
| UC11 | Production-agent attach | C5 | none (branchless) | Ops (founder-gated) | Q-068 | 0 |
| UC12 | Live voice rehearsal (T6, acceptance gate) | C5 | none (branchless) | Verification | — | 0 (T6 evidence) |

**Test readiness today:** 0 calendar tests exist (no code yet). The plan names every test case per unit; the estimates above total **~455 automated tests** plus ~10 opt-in live Google checks and the opt-in live Blob conformance run. Past units in this repo have always grown past their estimates during review; STATUS.md tracks actuals.

---

## C0 — Feasibility (UC01)

**Definition of done:**
- It is known, with evidence, whether a Foundry agent in a Voice Live **agent-mode** session runs an OpenAPI tool and/or an MCP tool **server-side**, using the bridge's own connect path and `api_version` (plan P7: a scratchpad harness, not the web debug client).
- The primary transport is chosen and recorded (D-NNN).
- The token's principal claim (`oid`/`azp`/`appid`), its granularity (resource, project or agent), and whether publishing changes it are recorded.
- Whether an app role can be assigned to that principal, and whether Entra then refuses tokens to unassigned principals, is recorded.
- Foundry's tool timeout (if observable), latency, and how a non-2xx body reaches the model are recorded.
- All throwaway Azure resources are deleted and the deletion verified.
- **Go/no-go:** Go continues the plan (with a plan revision first if MCP is primary); No-go stops the track until the partner revises the spec (spec §3.1).

**Test coverage:** none automated; an evidence table in the STATUS.md audit row.

## C1 — Core on the fakes (UC02a, UC02b, UC03, UC04a, UC04b, UC04c)

**Definition of done:**
- `agent-tools/calendar/` exists as a self-contained uv project that never imports from `server/` (G1b) and whose `core/` imports no framework, vendor SDK, provider, store or HTTP code (G1).
- Bindings are validated at startup and fail closed on every spec §4.1 rule without echoing values.
- The OpenAPI document is the single contract: valid, carrying the §5.4 descriptions verbatim, and rendered per binding by changing only `servers[0].url` (G3). It contains no agent, business or person vocabulary (G2).
- The HTTP layer enforces health/404/405/413/401/403/invalid-body rules in a fixed order, with byte-identical 403 bodies and in-code Entra JWT validation.
- `check_availability` implements spec §5.2/§7.1/§7.2 against the fake provider, including DST.
- `book_appointment` implements spec §5.3/§7.3/§7.4 in full (steps 1–8, recovery, verification, the uncertain-create path) against the fake provider and fake claim store, with every §13.1 booking case green, including the round-2 chain cases, the cross-binding race, mixed buffers, clock skew and the host-edit cases.
- One service instance serves two bindings on two adapters with no cross-binding leakage (G4).
- Logs carry one structured line per request and no contact data, tokens, titles or calendar IDs. Every non-OK outcome logs a diagnostic that is neither empty nor `ok`, with a non-secret `reason` where spec §10 requires one; a malformed secret is `secret_invalid`, apart from `secret_store_unreachable` (spec rev 3.2).

**Test coverage (~335, estimate):**

| Area | Unit | Tests |
| --- | --- | --- |
| Ports, bindings, contract document, renderer, registry, fake conformance, G1/G1b/G2/G3, logging | UC02a | ~70 |
| Config, JWT validation, dispatcher rules | UC02b | ~45 |
| Slot engine (incl. DST), tokens, `check_availability`, HTTP contract | UC03 | ~75 |
| Identity HMACs, cells, contact normalization, claim-store conformance (fake) | UC04a | ~45 |
| Booking steps, replay independence, races, G4, redaction, HTTP contract | UC04b | ~60 |
| Recovery table, uncertain create, SF-3/SF-7/SF-8 lifecycle cases | UC04c | ~40 |

## C2 — Azure platform (UC05, UC06)

**Definition of done:**
- A dedicated resource group (per Q-067), user-assigned identity, dedicated RBAC Key Vault, and identity-only storage account with a `claims` container and a 90-day lifecycle rule exist, created by Bicep under the calendar project's own `azure.yaml`, with `az deployment sub validate` and `--preview` gates passed.
- The identity holds exactly Key Vault Secrets User on the vault and Storage Blob Data Contributor on the claims container (or on a separate claims account, if UC05's Flex check requires it).
- The Entra app registration exists with one app role, assignment required, no credentials, and the role assigned only to the UC01-verified principal(s).
- `slot-token-key` and `fingerprint-key` exist in the vault; no value was ever printed or committed; the operator's time-bound Secrets Officer grant is removed.
- `AzureBlobClaimStore` passes the claim-store conformance suite against a faithful Blob emulator and, once, against the real `claims` container, including concurrent claims on one cell and age measured by storage-server time.

**Test coverage (~30 + live):** Blob store conformance (emulator), 409/412/5xx mappings, token caching; the opt-in live conformance run. UC05 is verified by `az`/`azd` commands, not pytest.

## C3 — Google (UC07, UC08a, UC08b)

**Definition of done:**
- The Google OAuth client and consent screen exist per Q-064–Q-066 (never External + Testing).
- The consent tool captures a refresh token with PKCE and `state`, checks the granted scopes, and writes it straight to Key Vault without it touching disk, stdout, logs or chat.
- The credential source refreshes and caches access tokens, single-flights refreshes, and maps `invalid_grant` to `credential_rejected`.
- `GoogleCalendarProvider` passes the provider conformance suite (mocked), including the freeBusy `errors` case and the 409-on-ID flow.
- Live checks on a throwaway test calendar confirm the scopes are sufficient (or the recorded fallback), the free/busy semantics (the fake is aligned with them), and that created events are visible to `find_bookings` far inside 120 s.

**Test coverage (~80 + ~10 live):** Key Vault source, the Key Vault-backed `KeyRing` (plan P18), OAuth exchange, credential caching and single-flight, consent tool (loopback, PKCE, state, scopes, no-leak); adapter operation mapping, status mapping, conformance, G4 with Google.

## C4 — Deployed and verified on a test agent (UC09, UC10)

**Definition of done:**
- The Function app runs on Flex Consumption with the user-assigned identity, identity-based storage, the bindings from azd (`CALENDAR_BINDINGS_JSON_B64`), App Insights, and the spec §10 alerts; deployed with the D-042 rules applied by analogy (provision immediately followed by deploy).
- Probes: health 200; no/malformed/wrong-audience token 401; unknown and forbidden bindings an identical 403 (via the test agent, UC10).
- Cold-start and warm latency are measured; the always-ready decision is put to the founder under Q-067.
- A Foundry-issued token is shown to carry `roles: ["Calendar.Invoke"]` once the app-role assignment is older than Foundry's 24 h token cache (Q-075, UC01 E13).
- The real host calendar is consented; the demo binding uses Q-069's values; the tool is attached to a **test** agent that is `prompt`-kind (UC01 E1); T1–T5 pass with evidence.

**Test coverage (~10):** the Functions adapter, the requirements/lockfile sync check, and wiring (no service key read at startup, plan P18); the rest is live evidence (probes, T1–T5).

## C5 — Production and acceptance (UC11, UC12)

**Definition of done:**
- The production agent carries the tool and the §11.4 instructions, attached per Q-068 (not before the 2026-10-02 demo has finished unless the founder says otherwise), with principal/publish and pinned/unpinned (D-049) handling confirmed and a written rollback. The agent stays `prompt`-kind with Voice mode on, and is not channel-published without a principal re-check (D-059 (c)).
- **T6 passes on the production path** (spec §13.4): the acceptance gate. Automated tests and T1–T5 are necessary but not sufficient.

**Test coverage:** T6 evidence (call ID, Foundry trace, service log lines, calendar screenshot). Any behavior T6 finds that the automated tests missed gets a regression test in a new code unit before its fix is accepted.

---

## Question gates (founder answers needed, STATUS.md §3)

| Question | Needed before |
| --- | --- |
| Q-072 Confirm D-052's narrow §7 lifts | UC01 (the first OpenAPI tool wiring) |
| Q-067 Azure placement and spend | UC01 (throwaway resources), UC05 (real resources), UC09 (always-ready) |
| Q-065 OAuth client type | UC07 |
| Q-064 Google Cloud project | UC08a |
| Q-066 Google account and consent-screen type | UC08a |
| Q-069 Demo binding values | UC10 |
| Q-068 When the production agent may change (D-049) | UC11 |
