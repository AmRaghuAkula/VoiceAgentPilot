# M6 — Deploy (Step 4) Implementation Plan

Last updated: 2026-09-27 · Supersedes the M6 placeholder in [2026-09-25-telephony-bridge-milestones.md](2026-09-25-telephony-bridge-milestones.md) · Spec: [../../TELEPHONY_BRIDGE_SPEC.md](../../TELEPHONY_BRIDGE_SPEC.md) §6 · Decisions: [D-029](../../DECISIONS.md) · Live status: [../../STATUS.md](../../STATUS.md)

---

## Why now (read before anything else)

M6 is proceeding **without the ACS phone number** (Q-002 stays open — Azure support ticket 2609250040002301, no ETA), per the founder's direct instruction recorded as D-029. The founder judged that an open, ETA-less support ticket should not block standing up the Container App, Key Vault, Event Grid wiring and identity/role assignments that the pilot needs regardless of which number eventually arrives.

**What this unblocks:** every M6 unit below — Bicep changes, the Container App, Key Vault, Event Grid subscription, monitoring, and the `azd up` deploy itself — can complete before Q-002 resolves.

**What this does not unblock:** M7 (the spec §8 live acceptance tests) still needs the real ACS test number, because tests 1–2, 4–5 and 9 require Raghu to dial the number from his own mobile phone. Nothing in this plan pretends otherwise.

**What can be validated before the number arrives:** see the "Q-011 — VoIP-only validation" section below. Short answer: yes, a VoIP-only ACS call (no PSTN number) can exercise the deployed pipeline end-to-end as a *route-miss* — this validates the wiring (Event Grid → Container App → ACS answer → fallback message → hang-up) but cannot validate the Voice Live/Foundry agent connection itself, because a route-miss never reaches Voice Live by design. The exact steps are below, and they are written as a concrete unit (U15), not left as a vague aspiration.

---

## What the repo actually contains today (verified by reading the files, not inferred from the spec)

This section exists because the spec's Step 4 table describes the *desired* end state, and the accelerator's actual Bicep does not match it in three material ways. Each was confirmed by reading the file, not assumed:

| Spec says | Accelerator's Bicep actually does | File read |
| --- | --- | --- |
| "must not create its own AI Services/Speech resource — point at the existing `hireastra-resource` through parameters" | `modules/aiservices.bicep` **creates a brand-new** `Microsoft.CognitiveServices/accounts` resource (`aiServices-${environmentName}-${uniqueSuffix}`) unconditionally. `main.bicep` has no parameter path to point at an existing resource instead. | `infra/main.bicep` lines 116–126; `infra/modules/aiservices.bicep` lines 38–58 |
| "system-assigned managed identity" (Q-004 answer) | `modules/identity.bicep` creates a **user-assigned** identity (`Microsoft.ManagedIdentity/userAssignedIdentities`), and `main.bicep`/`containerapp.bicep`/`aiservices.bicep` all wire that user-assigned identity in (`identity: { type: 'UserAssigned', userAssignedIdentities: {...} }`). There is no system-assigned identity anywhere in the accelerator's Bicep. | `infra/modules/identity.bicep`; `infra/main.bicep` lines 80–89, 163–165; `infra/modules/containerapp.bicep` lines 65–68; `infra/modules/aiservices.bicep` lines 41–44 |
| "Event Grid subscription for `Microsoft.Communication.IncomingCall` filtered to the test number" (Bicep-level resource) | **There is no Event Grid Bicep module at all.** The subscription is created imperatively, post-deploy, by `hooks/providers/acs.postdeploy.ps1` running `az eventgrid event-subscription create --source-resource-id <ACS resource> --included-event-types Microsoft.Communication.IncomingCall`. It filters only by **event type**, not by number — there is no per-number filter in the command or anywhere else in the script. | `hooks/providers/acs.postdeploy.ps1` lines 36–43, 80–86; `grep -ril eventgrid infra/` returned nothing but `abbreviations.json` and `main.bicep`'s own text (no resource) |
| Resource group `rg-hireastra-voice-pilot` | `main.bicep` computes its own resource group name: `rg-${environmentName}-${uniqueSuffix}` where `uniqueSuffix` is a hash. This is *not* the fixed name the spec expects unless `environmentName` is set to `hireastra-voice-pilot` and the hash suffix is accepted as part of the name (e.g. `rg-hireastra-voice-pilot-a1b2c`). | `infra/main.bicep` lines 70–78 |
| Budget alert | Confirmed: **no budget/`Microsoft.Consumption` Bicep module exists.** The spec itself says "Raghu sets" this — so this was never code's job. Recorded here so it isn't silently missed. | `grep -ril "budget\|Microsoft.Consumption" infra/` → no match |

**Consequence for this plan:** M6's first unit is a real Bicep-editing unit, not a no-op deploy of the accelerator as-is. The accelerator's defaults would deploy a second, redundant AI Services resource（wrong — violates "one AI resource only"）and a user-assigned identity（wrong — Q-004's answer was system-assigned), so U14 must fix both before anything is provisioned.

**Also verified:** `hooks/preprovision.ps1` and `hooks/providers/acs.postdeploy.ps1` are interactive-by-default (`Read-Host` prompts, `-Confirm`-style flows) but branch cleanly around ACS with no prompts when `TELEPHONY_PROVIDER=acs` is already set and no other provider's credentials are present (`main.bicep`'s `telephonyProvider` param defaults to `'acs'`; the "no telephony credentials detected" branch in `preprovision.ps1` defaults to ACS with no prompt at all — see lines 151, 361–364). This matters because `azd up`'s `preprovision` hook runs `interactive: true` per `azure.yaml` — the founder/builder should expect at most the model-selection prompt (also skippable by pre-setting `AZURE_VOICE_LIVE_MODEL`), not a wall of telephony questions.

---

## Units

M6 follows the same discipline as M0–M5: **one unit = one branch = one PR**, founder "go" before the builder starts, Opus review (+ `cso` where there's a security surface) before merge, STATUS.md updated inside the PR. Because this is infra work rather than TDD-on-Python, "tests" for these units are **verification commands and their expected output** (`az` queries, `azd` exit codes, log greps), not pytest. Each unit still gets an explicit DoD checklist and a verification step the builder runs and pastes output from before opening the PR.

Two units (U14, U15) plus one explicit go/no-go decision point make up M6. A third, optional unit (U16) is listed but stays `BLOCKED` until Q-002 resolves, since it depends on the real number.

### U14 — Fix the accelerator's Bicep to match the spec (AI resource, identity, resource group, config)

**Branch:** `feat/tb-m6-u14-infra-fixes`

**What it does, in order:**

1. **Point at the existing `hireastra-resource`, don't create a new AI Services resource.**
   - Add parameters to `infra/main.bicep` (and thread through to wherever needed): `existingAiServicesResourceGroup`, `existingAiServicesName` (or a single `existingAiServicesResourceId`).
   - Change `modules/aiservices.bicep`'s call site in `main.bicep` to a conditional: when the existing-resource parameters are supplied, use `existing` resource lookup instead of the `aiServices` module's `resource ... = { ... }` creation block. The cleanest approach given the current module shape: replace the unconditional `module aiServices 'modules/aiservices.bicep' = {...}` with an `if` (create only when no existing resource is given) and add a sibling `resource existingAiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {...}` reference, then a `var aiServicesEndpoint = ... ? existingAiServices.properties.endpoint : aiServices.outputs.aiServicesEndpoint` style selector — mirroring the `acs` module's existing `if (telephonyProvider == 'acs')` conditional pattern already in the file (main.bicep line 128).
   - For the pilot, the existing resource is `hireastra-resource` in resource group `rg-hireastra`, region East US 2 (Q-003). Set `main.parameters.json` to pass these as the deploy-time values (via `azd env set`, not hardcoded).
2. **System-assigned identity, not user-assigned (Q-004).**
   - Change `containerapp.bicep`'s `identity: { type: 'UserAssigned', userAssignedIdentities: {...} }` block to `identity: { type: 'SystemAssigned' }`.
   - Remove (or make conditional/dead) `modules/identity.bicep`'s user-assigned identity creation and the `appIdentity.outputs.principalId`/`identityId`/`clientId` references that currently thread through `main.bicep`, `roleassignments.bicep`, `keyvault.bicep`'s consumers, and `containerapp.bicep`'s registry-pull identity reference.
   - `roleassignments.bicep` currently assigns roles to `identityPrincipalId` (today, the user-assigned identity's principal id) — this must become the Container App's own system-assigned principal id, which is only known *after* the Container App resource is created (`containerApp.identity.principalId`), which changes the module dependency order: role assignments must now depend on `containerapp`, not the other way around as today (`containerapp` currently `dependsOn: [RoleAssignments]`). This is the single largest structural change in this unit — treat the dependency-order rewrite carefully and verify with `az deployment group what-if` before applying.
   - Update the role assigned from "Azure AI User" (today's `roleassignments.bicep` line 19–27, `53ca6127-db72-4b80-b1b0-d745d6d5456d`) to **Foundry User** per spec §6. Verify the exact Foundry User role definition GUID against the portal/`az role definition list` at deploy time — do not assume "Azure AI User" and "Foundry User" share a GUID; the spec explicitly calls out the Microsoft rename ("formerly Azure AI User") but a rename in documentation doesn't guarantee the role definition GUID is identical. **This check is a step in U14, not left to chance** (see verification steps below).
3. **Resource group name.**
   - Confirm with the founder/Cowork whether `rg-hireastra-voice-pilot` must be the exact name or whether `rg-hireastra-voice-pilot-<hash>` (the accelerator's existing suffix pattern) is acceptable. Given D-029 doesn't re-litigate this and the spec just says "a new resource group... isolates cost," the pragmatic default is: set `environmentName=hireastra-voice-pilot` and accept the accelerator's own suffix pattern, rather than hand-editing `main.bicep`'s `rgName` variable to drop the hash (dropping it risks an accidental production diff from every other accelerator-derived environment). **Log this as a decision** (either a new D-NNN if the founder confirms the suffixed name is fine, or a Q-NNN if not) — see "New Open Questions" below (Q-012).
4. **Container App settings already match the spec table** (min replicas 1, max... today's file has `maxReplicas: 10`, not 1). **Fix:** spec §6 says "min replicas 1, max 1." Change `containerapp.bicep`'s `scale.maxReplicas` from `10` to `1`. External HTTPS ingress is already correct (`ingress.external: true`, default HTTPS).
5. **Key Vault** already holds the ACS connection string via the existing `keyvault.bicep` module (verified: `acsConnectionStringSecret` resource, conditional on non-empty `acsConnectionString`) — this needs no structural change, only the identity feeding it Key Vault Secrets User must become the system-assigned principal (covered by point 2 above). `MEDIA_WS_TOKEN` is **not currently a Key Vault secret or a Bicep parameter at all** — it's an app-level env var read directly from the environment per `bridge_config.py`/`.env.sample`. Add it as a new secure parameter threaded the same way `acsConnectionString` is (`main.bicep` → `keyvault.bicep` → `containerapp.bicep`'s `secretRef`), generated as a random 32+ character value at provision time (e.g. via a `preprovision.ps1` addition using `[System.Web.Security.Membership]::GeneratePassword` or `-join ((48..57)+(65..90)+(97..122)|Get-Random -Count 40 |%{[char]$_})`, stored with `azd env set MEDIA_WS_TOKEN`, never echoed to the console).
6. **Config reference table env vars** — verify each of spec §6's table entries is wired through: `VOICE_LIVE_ENDPOINT` (today's Bicep emits `AZURE_VOICE_LIVE_ENDPOINT`; per D-003 both names are read by `bridge_config.py`, so this is not blocking, but prefer adding `VOICE_LIVE_ENDPOINT` alongside it in `containerapp.bicep`'s env array so the spec's own name is what's actually set, not just tolerated), `AGENT_ROUTING_JSON` (not in Bicep today at all — add as a plain, non-secret Container App env var, since it's the routing table, not a credential), `MAX_CALL_SECONDS`, `FALLBACK_MESSAGE`, `AMBIENT_PRESET` (all currently absent from `containerapp.bicep`'s env block — add all four as plain env vars, values from `main.parameters.json`/`azd env set`), `ACS_CONNECTION_STRING` (already wired, Key Vault-backed), `MEDIA_WS_TOKEN` (point 5 above).

**DoD:**
- [ ] `az bicep build --file infra/main.bicep` compiles with no errors.
- [ ] `az deployment sub what-if --location eastus2 --template-file infra/main.bicep --parameters infra/main.parameters.json` runs and its plan shows: no new `Microsoft.CognitiveServices/accounts` resource being created (only a reference/role assignment against the existing `hireastra-resource`), the Container App with `identity.type == SystemAssigned`, `scale.maxReplicas == 1`, and a Key Vault secret entry for `MEDIA_WS_TOKEN`.
- [ ] The Foundry User role GUID has been verified against `az role definition list --name "Foundry User"` (or the portal) and matches what `roleassignments.bicep` now assigns — pasted into the PR body as evidence.
- [ ] `server/.env.sample` already documents every env var this unit wires through Bicep (confirmed: it does, per D-003/U05) — no new undocumented var is introduced.
- [ ] No secret value appears in any committed file, `main.parameters.json` included (parameters must resolve from `${VAR}` placeholders, matching the existing pattern for `TWILIO_AUTH_TOKEN` etc.).
- [ ] Opus code-review round on the Bicep diff (infra changes still get reviewed per D-013's spirit even though there's no Python here) — scoped to `main...HEAD`. `cso` also runs, since this unit creates/changes identity and secret-handling logic (Key Vault wiring, role assignment dependency reordering) — this is not a docs-only PR.
- [ ] STATUS.md/DECISIONS.md updated inside this PR per D-017.

**This unit does not run `azd up`.** It only edits and validates Bicep (`what-if`, `bicep build`). Actual provisioning happens in U15.

### U15 — `azd up`, verify, and (optional) VoIP-only smoke test

**Branch:** `feat/tb-m6-u15-deploy`

**Prerequisite:** U14 merged; founder confirms `az login`/`azd auth login` are active on the deploy machine (Q-006 — founder reported this in progress as of 2026-09-27; the builder verifies with `az account show` and `azd auth login --check-status` or equivalent before proceeding, and stops if not signed in, rather than prompting interactively mid-unit).

**What it does:**

1. **Deploy.** `azd up` from the repo root, with `environmentName` set per U14 point 3's resolution, `AZURE_LOCATION=eastus2`, `TELEPHONY_PROVIDER=acs`, and the existing-AI-Services parameters from U14 point 1 pre-set via `azd env set` (so `preprovision.ps1`'s interactive prompts are skipped wherever possible — model selection is the one prompt likely to remain unless `AZURE_VOICE_LIVE_MODEL` is also pre-set to `gpt-4o-mini`, matching the spec's existing agent).
2. **Verify infra came up as expected**, not just that `azd up` exited 0:
   - `az containerapp show` — confirm `identity.type == SystemAssigned`, `scale.maxReplicas == 1`, ingress external HTTPS, the FQDN.
   - `az role assignment list --assignee <system-assigned principal id> --scope <hireastra-resource id>` — confirm Foundry User is present.
   - `az keyvault secret list` — confirm `ACS-CONNECTION-STRING` and the new `MEDIA-WS-TOKEN` secret both exist (no value printed in logs).
   - Confirm the `postdeploy` hook's Event Grid step ran: `az eventgrid event-subscription show --name incoming-call-webhook --source-resource-id <ACS resource id>` — confirm it exists, its `includedEventTypes` is exactly `["Microsoft.Communication.IncomingCall"]`, and its `destination.endpointBaseUrl` matches the deployed Container App's `/acs/incomingcall` URL. **This is where the spec's "filtered to the test number" line is formally resolved** (see Q-011 section — the actual `az eventgrid event-subscription create` command in `acs.postdeploy.ps1` has no number filter parameter at all; ACS's Event Grid integration filters by event type and source resource, not by destination number, so "filtered to the test number" was aspirational spec language that doesn't correspond to an actual ACS/Event Grid API capability at the subscription level. Log this gap explicitly — see Q-013 below).
   - Confirm Log Analytics/Application Insights: `az monitor app-insights component show` against `hireastra-appinsights-0257` (spec §6) — verify the Container App's logs destination is wired to the same Log Analytics workspace this deploy created, and that connecting the *existing* `hireastra-appinsights-0257` (rather than the newly created `insights-<env>-<suffix>` from `monitoring/applicationinsights.bicep`) was actually intended — **flag this as a probable second infra gap**: today's `monitor.bicep` creates a **new** Application Insights resource, the same "creates its own instead of pointing at ours" pattern found in U14 for AI Services. If the spec truly wants "Application Insights connection to `hireastra-appinsights-0257`" (an *existing* resource) rather than a new one, that's a fourth Bicep fix that belongs in U14, not U15 — **the builder confirms this in U14's `what-if` review before U15 runs**, and if it's confirmed as a real gap, U14's scope grows to include it (this is exactly the kind of plan-vs-spec mismatch CLAUDE.md §5 step 4 says to catch by verifying before implementing, not discover mid-deploy).
3. **Budget alert.** Confirm with the founder that this is on them, not code (spec §6 explicitly says "Raghu sets" it) — no builder action, but log in STATUS.md that this remains the founder's action item, not a missed unit.
4. **The VoIP-only smoke test — see the dedicated Q-011 section below for exact steps.** This is a verification step inside U15, run once the Event Grid subscription is confirmed live, before this unit's PR is opened, and its outcome (pass/fail/inconclusive) is written into the PR body and STATUS.md, not just narrated in chat.

**DoD:**
- [ ] `azd up` completes successfully; the resource group, Container App, Key Vault, and Event Grid subscription all exist as verified above.
- [ ] The four verification commands above (Container App identity/scale, role assignment, Key Vault secrets, Event Grid subscription) are run and their output pasted into the PR body.
- [ ] The Application Insights gap (point 2's flag) is either resolved (folded back into U14 and re-verified) or explicitly logged as a new Open Question if the founder wants to defer it.
- [ ] The VoIP-only smoke test (Q-011) has been attempted and its outcome recorded — pass, fail, or "inconclusive, here's why" are all acceptable outcomes for this unit to merge; "not attempted" is not.
- [ ] The ACS callback JWT check (Q-005) — confirm `ACS_CALLBACK_JWT_AUDIENCE` is verified against the real issuer/JWKS URL/audience at this point (this was deferred to "verify at M6" by Q-005's answer) and either enabled with the confirmed audience value, or left off with the reason logged, before this unit merges.
- [ ] No phone numbers, secrets, or the real ACS connection string appear in the PR body, STATUS.md, or any committed file — verification command output pasted into the PR must be checked for this before pasting (e.g. redact the Key Vault secret *values*, only show secret *names* exist).
- [ ] `cso` runs on this unit (deploying live infra with secrets and identity is squarely a security surface) in addition to the Opus code-review round, both scoped to this unit's diff (docs/hook-script changes, if any) plus a narrative review of the verification command output for anything that shouldn't be there.
- [ ] STATUS.md/DECISIONS.md updated inside this PR per D-017, including the founder-visible confirmation that M6's infra is live (or a clear statement of what's still short, if `azd up` partially failed).

### U16 — Filter the Event Grid subscription to the real test number (BLOCKED on Q-002)

**Branch:** not cut until Q-002 resolves.

**What it does:** once the ACS phone number exists, confirm whether ACS/Event Grid actually supports narrowing `Microsoft.Communication.IncomingCall` delivery by destination number (via an Event Grid **advanced filter** on a `data.to.rawId`/`data.to.phoneNumber.value` field, if the event schema exposes it as a filterable property — this needs checking against the live event schema once a real number exists, not assumed), and either:
- (a) add that advanced filter to the existing subscription (`az eventgrid event-subscription update --advanced-filter ...`) if the schema supports it, narrowing delivery to just the pilot's number, or
- (b) confirm the subscription is deliberately left unfiltered by number (since only one number will ever exist on this ACS resource for the pilot, an unfiltered subscription is already effectively "the test number" in practice) and close Q-013 with that reasoning instead.

This unit is **not required for the pilot to work** — `bridge_calls.py`'s own routing table (`AGENT_ROUTING_JSON`) is what actually decides whether an incoming call reaches the agent or gets the fallback message; the Event Grid filter (or lack of one) only affects whether *other* ACS resources' calls could theoretically reach this Container App, which doesn't apply since the pilot's ACS resource has (and will only ever have, for the pilot) one number. Recorded as its own unit for completeness and because Q-013 asks the question explicitly, but the founder can defer it indefinitely without blocking M7/M8.

**DoD:** decision (a) or (b) above is made and logged as a D-NNN; if (a), the filter is applied and verified with `az eventgrid event-subscription show`.

---

## Q-011 — Can a VoIP-only ACS call validate the pipeline before the number arrives?

**Conclusion: yes, partially — it validates the wiring, not the agent connection. Here is why, and the exact steps.**

### The evidence (read from the actual code, not assumed from the spec)

1. **`hooks/providers/acs.postdeploy.ps1`** creates the Event Grid subscription with `--included-event-types "Microsoft.Communication.IncomingCall"` and **no number filter of any kind** (confirmed: neither `--subject-begins-with`/`--subject-ends-with` nor `--advanced-filter` appear anywhere in the script). ACS's `IncomingCall` event fires for **any** call reaching the ACS resource — by phone number *or* by a VoIP/ACS-user identity (a "communication user") placed via the ACS Calling SDK/REST API. The subscription will deliver both kinds identically to `/acs/incomingcall` once deployed.
2. **`server/app/routing.py`'s `called_number_from_event()`** (already merged, U03) extracts the called identifier generically: `identifier.get("phoneNumber", {}).get("value") or identifier.get("rawId")`. A VoIP call's `to` field has `kind: "communicationUser"` with only a `rawId` (e.g. `8:acs:resource_user-id`), no `phoneNumber`. This is **explicitly one of the plan's own test cases** (`test_resolve_miss` asserts `resolve_route(ROUTES, "8:acs:resource_user-id") is None`; the Task 8 plan text names this exact scenario: *"A called number that isn't a phone number (e.g. `to` is a Teams or ACS user with only a `rawId`...). The caller should hear the fallback message (a route miss), with no crash."*).
3. **`BridgeCallController.handle_incoming()`/`_answer()`** (Task 8, U10 — not yet merged, but its code is fully written out in the plan and unit tests exist for exactly this path) does not reject or special-case a non-phone-number `to` — it calls `resolve_route()`, gets `None` back, and proceeds down the **same `answer_call` path it always does** for any incoming call: it still answers the call via ACS Call Automation, still creates a `CallSession`, still logs `incoming_call route=miss caller=... called=...`, and (per `SessionSettings`/`CallSession`'s design, M3) plays the fallback message and hangs up cleanly. The only difference from a "wrong version" failure path (spec acceptance test 7) is that a route-miss never attempts to open media streaming at all (`kwargs["media_streaming"]` is only set `if route is not None`), so Voice Live is never contacted.

### What this does and does not prove

**Does prove, end-to-end, once M6 is deployed:**
- The Event Grid subscription is live and correctly wired to the Container App's `/acs/incomingcall` URL (infra correctness).
- ACS Event Grid subscription validation handshake succeeded at deploy time (already covered by `process_incoming_call`'s `EventGridSubscriptionValidationEventName` branch, and by U15's own `az eventgrid event-subscription show` check — but a live inbound event exercises the real, running deployed instance, not just the subscription's existence).
- The Container App receives and correctly parses a real `IncomingCall` event shape from live Azure infrastructure (not a unit test's synthetic dict) — this is real value; the unit tests use a hand-built `event.data` dict, and a live event could in principle have field-name or nesting differences the tests don't cover.
- ACS Call Automation's `answer_call` succeeds against the live ACS resource with a real `incomingCallContext` and real Call Automation credentials/connection string.
- The route-miss/fallback-message/clean-hangup path (spec's own "Do not build any calendar/booking, one entry for the pilot" default behavior, and a slice of acceptance test 7's failure-path logic) works against live infrastructure, not just mocks.
- Bridge logs correctly mask/record the call in this live scenario (D-021, log masking; the caller identity for a VoIP call is a `rawId`, not a phone number — worth explicitly confirming `mask_number()`'s "not a phone number" fallback path is exercised correctly and doesn't crash or leak the raw ACS user id).

**Does not and cannot prove before the real number arrives:**
- Anything about Voice Live / Foundry agent connectivity, since a route-miss by design never opens the `media_streaming` options or calls `connect_voicelive()` — no agent-mode session is ever attempted.
- Anything about the acceptance tests that require a route **hit** (tests 1–6, 8 all assume a working route to `re-intake-pilot-agent`) — none of these can be exercised without either (a) the real number, or (b) temporarily adding a *second*, throwaway routing-table entry keyed to some VoIP-callable identifier, which the spec's design explicitly treats as "one entry for the pilot" and which would require the caller's `to` identifier to be a stable, dialable ACS user id — not something the founder can currently "dial" the way he dials a phone number. This is not recommended as a way to fake test 1–6 early; it would need its own scoping decision if pursued, and is out of scope for this plan.

### Exact steps for the founder to place and observe a VoIP-only test call (once U15's `azd up` has completed)

This requires an ACS-Calling-SDK-based caller (a **browser or app using the ACS Calling SDK / Communication Services Calling JS/Web SDK**, or the ACS Call Automation REST API's own `create_call` operation targeting a `CommunicationUserIdentifier`) — a VoIP call inside the ACS resource is not something you dial from a regular phone; it is placed by a piece of code or a test client that authenticates as an ACS-issued identity. Concretely:

1. **Provision a test ACS user identity** (Cowork, in the portal or via `az`): `az communication identity user create --connection-string <ACS connection string>` — returns a `communicationUserId` (the `8:acs:...` rawId format).
2. **Issue that identity a VoIP access token** with the `voip` scope: `az communication identity token issue --connection-string <ACS connection string> --user <communicationUserId> --scope voip`.
3. **Place a call using that token**, targeting the bridge's own ACS resource, using either:
   - the **ACS Calling SDK for JavaScript** in a throwaway static HTML page (Microsoft's own quickstart: `azure-communication-calling` npm package, `callAgent.startCall([{ communicationUserId: <target> }])`) run locally by the founder or Cowork in a browser — the "target" here would be a *second* test ACS user identity also provisioned in step 1, since this is a user-to-user VoIP call *within* the ACS resource, which is what triggers `Microsoft.Communication.IncomingCall` against the resource the Event Grid subscription is watching; **or**
   - the simpler path: **ACS's own Call Automation REST API/SDK "create call" operation** initiating a call *to* a test communication user, which is directly analogous to `answer_call` but from the calling side, and can be scripted with a short Python snippet using the same `azure-communication-callautomation` SDK already in `server/`'s dependencies (no new SDK to add) — this avoids needing a browser at all.
4. **Watch the bridge's own logs in real time**: `az containerapp logs show --follow` (or the Log Analytics workspace query) filtered for `incoming_call route=miss`. The founder/Cowork should see, within seconds of the call being placed: an `incoming_call` log line with `route=miss`, `masked_caller=***` (or the ACS rawId, masked per D-021's non-phone-number fallback), the call answered, the fallback message played (`PlayCompleted`/`PlayFailed` callback logged), and `CallDisconnected` shortly after.
5. **Confirm in the Azure portal** (Cowork): the Event Grid subscription's own metrics (delivered event count) incremented by exactly one for this test.
6. **Report back to the founder** (partner, after U15 merges, in the same session's daily email or immediately if run standalone): pass/fail against the exact log lines expected in step 4, plus a screenshot or copy of the actual log line for the founder's own record — since the founder explicitly asked to see this confirmed against the real event shape, not assumed.

**This is U15's smoke-test step, not a separate unit** — it happens inside U15, after `azd up` and the Event Grid verification, before U15's PR opens, so its outcome is part of U15's DoD rather than a dangling promise.

---

## Definition of done and test/verification coverage — summary table

| Unit | Milestone | What's verified | Verification method (no pytest — this is infra) |
| --- | --- | --- | --- |
| U14 | M6 | Bicep points at existing `hireastra-resource`, system-assigned identity, Container App scale/ingress matches spec, Key Vault carries `MEDIA_WS_TOKEN`, Foundry User role GUID confirmed | `az bicep build`, `az deployment sub what-if`, `az role definition list` |
| U15 | M6 | `azd up` succeeds; Container App/Key Vault/Event Grid/monitoring all live and correctly configured; VoIP-only smoke test attempted and its outcome recorded; ACS callback JWT verified (Q-005) | `az containerapp show`, `az role assignment list`, `az keyvault secret list`, `az eventgrid event-subscription show`, live smoke-test log grep |
| U16 | M6 (optional, BLOCKED on Q-002) | Event Grid subscription's number-filtering question resolved one way or the other | `az eventgrid event-subscription show` post-change, or a closed Q-013 with no code change |

**M6 definition of done** (supersedes the placeholder in the milestones doc):
- [ ] The bridge is deployed with `azd up`, 1 replica, system-assigned managed identity, pointed at the existing `hireastra-resource` (no second AI resource created).
- [ ] The Foundry User role is granted to the bridge's system-assigned identity on `hireastra-resource`.
- [ ] Key Vault holds the ACS connection string and the media WebSocket token; the identity has Key Vault Secrets User.
- [ ] An Event Grid subscription for `Microsoft.Communication.IncomingCall` exists and points at the deployed Container App's `/acs/incomingcall` URL (number-filtering resolved per U16, or explicitly deferred).
- [ ] Application Insights is connected to `hireastra-appinsights-0257` specifically (not a newly created Application Insights instance) — **or** this is flagged and resolved as part of U14 if today's Bicep can't do that without a fix (see U15 point 2's flag).
- [ ] The ACS callback JWT check (Q-005) is verified against the real issuer/JWKS/audience and either enabled or explicitly left off with the reason logged.
- [ ] A $50/month budget alert exists on `rg-hireastra-voice-pilot` — founder's own action, confirmed done, not a code deliverable.
- [ ] The VoIP-only smoke test (Q-011) has been run at least once against the live deployment, with its outcome (pass/fail/inconclusive) recorded in STATUS.md.
- [ ] No unit in M6 touched anything under TELEPHONY_BRIDGE_SPEC.md §7's "Do not build" list (autoscaling beyond the fixed 1 replica, multi-region, a second active telephony provider, etc.).

---

## New Open Questions raised while writing this plan

Add these to STATUS.md §3 (see the status update alongside this PR):

- **Q-012** — Must the deployed resource group be named exactly `rg-hireastra-voice-pilot`, or is the accelerator's own `rg-<environmentName>-<hash-suffix>` naming pattern (e.g. `rg-hireastra-voice-pilot-a1b2c`) acceptable? Affects U14's resource-group handling. Owner: founder. Blocks: U14 (a small decision, not a blocker to starting the unit, but needs an answer before `azd up` in U15).
- **Q-013** — The spec's phrase "Event Grid subscription... filtered to the test number" does not correspond to an actual capability the accelerator's Event Grid wiring uses today (`acs.postdeploy.ps1` filters only by event type, via `az eventgrid event-subscription create --included-event-types`, with no number-based filter). Should U16 add a number-based **advanced filter** once the real number exists (if the `IncomingCall` event schema exposes a filterable `to` field), or is an unfiltered-by-number subscription acceptable indefinitely, since the ACS resource will only ever carry the one pilot number? Owner: founder/partner. Blocks: nothing in M6/M7; only closes out U16's DoD.
- **Q-014** — `infra/modules/monitoring/applicationinsights.bicep` creates a **new** Application Insights resource (`insights-<env>-<suffix>`) rather than connecting to the existing `hireastra-appinsights-0257` the spec names. Is this a real gap requiring a U14 Bicep fix (point the Container App's monitoring at the existing App Insights resource/connection string instead of creating a new one), or was `hireastra-appinsights-0257` meant as "create a new one with roughly that name," in which case no fix is needed? This needs resolving **before** U15 runs, since it changes U14's scope. Owner: founder/partner, needs an answer before U14's branch is cut. Blocks: U14, U15.

These three should be read alongside Q-002 (still open, doesn't block M6) and Q-005/Q-010 (already tracked, folded into U15's DoD above).
