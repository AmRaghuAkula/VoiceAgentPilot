# M6 — Deploy (Step 4) Implementation Plan

Last updated: 2026-09-28 (rev 5.1, plus Q-045 answered (b) by the founder — rev 5 re-targeted M6 from ACS to Twilio as the call path, per D-041, answering Q-044; rev 5.1 applies its Opus design-review fix pass: 2 blocking, 8 should-fix, 9 nits — see "What changed in rev 5.1") · Supersedes the M6 placeholder in [2026-09-25-telephony-bridge-milestones.md](2026-09-25-telephony-bridge-milestones.md) · Spec: [../../TELEPHONY_BRIDGE_SPEC.md](../../TELEPHONY_BRIDGE_SPEC.md) §6 (ACS-era text; see "Spec drift" below) · Twilio code this deploys: [2026-09-27-twilio-pilot-plan.md](2026-09-27-twilio-pilot-plan.md) (UT01a/UT01b, merged) · Decisions: [D-029](../../DECISIONS.md) (M6 unblocked), [D-030](../../DECISIONS.md) (partner delegation), [D-031](../../DECISIONS.md) (identity), [D-032](../../DECISIONS.md)/[D-039](../../DECISIONS.md) (upstream divergence, role trim and its correction), [D-033](../../DECISIONS.md) (monitoring deferred), [D-034](../../DECISIONS.md) (Opus routing), [D-036](../../DECISIONS.md) (ACS callback JWT), [D-040](../../DECISIONS.md) (U14a follow-ups), **[D-041](../../DECISIONS.md) (Twilio is the permanent number provider — the reason for this revision)** · Live status: [../../STATUS.md](../../STATUS.md)

**Revision note (rev 5.1, this revision).** Rev 5.1 is rev 5 plus a fix pass from rev 5's own Opus design review. That review returned 2 blocking findings, 8 should-fix and 9 nits. Its verdict was "fix pass, not rewrite", and it independently confirmed T1–T3, the Q-005/Q-043/D-036 reasoning and the Bicep line references. The main changes are:
- the rule that every `azd provision` is paired with an `azd deploy` (T11);
- clean-up of the stale ACS azd environments (T12);
- splitting U15 into U15 (deploy) and U15b (live smoke test and cutover);
- a rollback procedure that doesn't depend on a recorded tunnel URL;
- a staging-number option for Q-045 (later answered (b) by the founder; (d) deferred).

The full list is in "What changed in rev 5.1" at the end of this document.

**Rev 5.** Revs 1–4 were written entirely around ACS carrying production calls (Event Grid → `/acs/incomingcall` → Call Automation answer → ACS media WebSocket). D-041 (2026-09-28) made Twilio the permanent phone-number provider, so rev 4's central assumption no longer holds. Rev 5 re-targets M6 at **deploying the already-merged Twilio call path** (UT01a/UT01b — `/voice` and `/twilio/ws`) to the Container App. It does not design any new call-handling logic.

Everything below was re-derived by reading the current `main` checkout (`0608658`): `infra/`, `hooks/`, `azure.yaml`, `server/server.py`, `server/app/bridge_config.py`, `server/app/config_validator.py`, `server/app/provider_registry.py`, `server/app/providers/twilio/*`, `server/Dockerfile` and `server/.dockerignore`. Nothing was carried over from rev 4 on trust.

**Headline findings of rev 5:**
1. **The accelerator's Bicep already deploys Twilio natively.** Setting `TELEPHONY_PROVIDER=twilio` turns off every ACS-specific piece of U14a's Bicep automatically. It also stores `TWILIO_AUTH_TOKEN` in Key Vault and injects it through a secret reference. No new Bicep is needed for the Twilio call path itself.
2. **U14a's ACS wiring appears never to have been applied to Azure.** PR #33's stated verification was `az bicep build`, `azd provision --preview` and `what-if` only, all of them read-only. On that evidence it is dormant Bicep, not "dead infra", and there is nothing in Azure to decommission. *Hedge: this rests on PR #33's own description and the STATUS audit log. It has not been live-queried against Azure (see the evidence row for the one-line Cowork check).*
3. **The provider choice is load-bearing in three separate places, and ACS wins a tie.** If `TELEPHONY_PROVIDER` resolves to `acs`, the real damage is at the infra level (a full-PUT of the existing ACS resource, or a second ACS resource), and the container then crash-loops at startup (T1).
4. **`azd provision` resets the app to the upstream hello-world image (T11, blocking-class).** Every `provision` must be immediately followed by `azd deploy`. That is a standing rule for this environment.
5. **Two stale ACS azd environments exist locally (`u14a-preview`, `u14b-preview`), and the default environment is `u14b-preview` (T12).** Both are deleted as U14b's first step. Every azd command in this plan carries `-e <env>`.
6. **U14b's requirement stands but its build scope has collapsed.** D-031's identity design is already what `main` provisions. U14b is re-scoped to a small least-privilege Key Vault fix plus Twilio-mode verification. The first-deploy check moves into U15, whose deploy is itself a genuine first deploy.
7. **U14c loses two of its three items** (`MEDIA_WS_TOKEN` is ACS-only; the Foundry User GUID was already confirmed live in U14a per D-039). It gains a real secrets-hygiene fix: `server/.dockerignore` does not exclude `.env`, so a remote build would upload the founder's local `.env` (holding the Twilio auth token and a Voice Live API key) to Azure (T3).
8. **U15 is split in two.** U15 deploys and runs pre-cutover verification, and it can merge on that alone. **U15b** runs the live Twilio smoke test and the production cutover, on its own timeline after Friday's demo, per the founder's Q-045 answer (option (b); the staging-number option (d) is deferred).
9. **The ACS callback/JWT concerns (Q-005, Q-043, D-036) are not applicable to this deploy**, for a verifiable reason: `/acs/*` routes are never registered when ACS isn't the detected provider. U15 checks this with a 404 rather than assuming it.

History of earlier revisions (unchanged in substance, detail at the end of this document and in git): rev 1 was sent back by Opus review for under-stating deploy complexity. Rev 2 fixed that and split U14. Rev 3 fixed the ACS text-to-speech principal and the Key Vault half of the circular dependency (C1). Rev 4 folded in D-031/D-032/D-033. **Rev 4's full ACS-era text (including the Q-011 VoIP-only procedure) is preserved in git at `0608658:docs/superpowers/plans/2026-09-27-m6-deploy-plan.md`**. It is only summarized below, to keep this document usable.

---

## Why now (read before anything else)

**Q-040 is the driver.** The bridge today runs only on the founder's laptop, reached through a Cloudflare quick tunnel. A real call attempt on 2026-09-27 got "services unavailable". Any laptop sleep, stopped process or changed tunnel URL is a dead end for a caller. The founder named M6 (moving off the laptop) as the priority. D-041 then changed *how a call reaches the bridge* but not the need to host it in Azure: the Container App still has to pull its image, read its secrets, and authenticate to Voice Live, whatever carrier delivers the call.

**What M6 now deploys (the Twilio call path, all code already on `main`):**

```
Caller ──PSTN──> Twilio number (D-041)
  Twilio ──HTTPS POST /voice (X-Twilio-Signature)──> Container App (external ingress, 1 replica)
     /voice: validate signature → resolve route from `To` → TwiML <Say "please wait"> <Connect><Stream wss://<fqdn>/twilio/ws>
             with customParameters {token = HMAC(TWILIO_AUTH_TOKEN, ts.calledNumber), calledNumber}
  Twilio ──WSS /twilio/ws (Media Streams, mulaw 8k)──> TwilioMediaHandler
     authenticate_and_start() → re-resolve route → TwilioCallSession → run_call_loop
       └─ Voice Live agent mode (DefaultAzureCredential → the user-assigned identity, D-031; Foundry User on hireastra-resource)
```

**What is gone compared to rev 4:** Event Grid, the `acs.postdeploy.ps1` Event Grid subscription, ACS answering and speaking the call, the ACS text-to-speech role, `MEDIA_WS_TOKEN`, the ACS callback JWT, the VoIP-only smoke test (Q-011) and the Event Grid number filter (U16). All of that remains in the repo as dormant code (TELEPHONY_BRIDGE_SPEC.md §7: "leave that code in place, unused"). It must not be *configured*, because configuring a second telephony provider is itself on §7's "Do not build" list, and here it would also silently disable Twilio (T1).

---

## What the repo actually contains today (verified by reading the files on `main` @ `0608658`)

Every row was confirmed by reading the file named. None is inferred from the spec, from rev 4, or from memory.

| Fact | Evidence |
| --- | --- |
| The accelerator's Bicep natively supports `telephonyProvider = 'twilio'`: a `@secure() twilioAuthToken` parameter, written to Key Vault as `TWILIO-AUTH-TOKEN`, attached to the Container App as secret `twilio-auth-token` (identity-resolved), and exposed as env `TWILIO_AUTH_TOKEN` via `secretRef`. | `infra/main.bicep` 37–42, 208, 266; `infra/modules/keyvault.bicep` 50–56, 107; `infra/modules/containerapp.bicep` 114–120, 196–200; `infra/main.parameters.json` 17–22 |
| Every ACS-specific piece is gated on `telephonyProvider == 'acs'`: the ACS module (and so its system-assigned identity and the adoption of `hireastra-voice-pilot-acs`), the ACS connection-string secret, `ACS_COGNITIVE_SERVICES_ENDPOINT`, and the ACS text-to-speech role assignment (`acsPrincipalId` passed as `''`, which skips it). Under `twilio`, none of these deploy. | `main.bicep` 185 (`acs` module `if`), 207 (secret), 239 (role), 258 (endpoint); `airoleassignments.bicep` 40 (`if (!empty(acsPrincipalId))`) |
| Parts of U14a that still apply under Twilio: the existing resource group is adopted (`existingResourceGroupName`), `hireastra-resource` is referenced cross-subscription with **no new AI account**, Foundry User goes to the user-assigned identity, scale is fixed at 1/1, `VOICE_LIVE_ENDPOINT` is set, and `AGENT_ROUTING_JSON` arrives base64-decoded. | `main.bicep` 106–129, 167–183, 232–241, 259; `airoleassignments.bicep` 30–38; `containerapp.bicep` 176–178, 253–256 |
| **U14a appears never to have been provisioned live (hedged: not live-queried).** PR #33's verification section lists `az bicep build`, `azd provision --preview --no-prompt` (scratch env `u14a-preview`) and `az deployment sub what-if` only. Those are all read-only. STATUS §2's U14a audit row says the same, and D-041 says nothing was pushed to Azure for U14b. So the ACS resource's system-assigned identity is still OFF (as Cowork found for Q-016), and no text-to-speech role assignment exists. *Evidence is PR #33's body and the audit log. Live Azure state was not independently queried in this planning session. Cowork can confirm with `az resource show -n hireastra-voice-pilot-acs -g rg-hireastra-voice-pilot --resource-type Microsoft.Communication/communicationServices --query identity`.* | `gh pr view 33` (Verification section); STATUS.md §2 U14a rows |
| External HTTPS ingress already exists: `external: true`, `targetPort: 8000`, `transport: 'auto'` (HTTP/1.1 upgrade, so WebSockets work). The FQDN is output, and `SERVICE_API_ENDPOINTS` for `twilio` is `https://<fqdn>/voice`. `allowInsecure` is unset (defaults to false). | `containerapp.bicep` 95–99, 271; `main.bicep` 294–302 |
| The container image is built with only the selected provider's extras: `azure.yaml` passes `TELEPHONY=${TELEPHONY_PROVIDER}` as a Docker build arg, and the Dockerfile runs `uv sync --extra ${TELEPHONY}` (default `acs`). A Twilio deploy needs `TELEPHONY_PROVIDER=twilio` at **build** time, or the `twilio` package is missing from the image. The base image is Python 3.12, so the stdlib `audioop` used by `TwilioMediaHandler` is present. | `azure.yaml` 14–18; `server/Dockerfile` 4, 7, 27; `server/pyproject.toml` (`twilio` extra) |
| `hooks/preprovision.ps1`: when `TWILIO_AUTH_TOKEN` is already in the azd env, it sets `TELEPHONY_PROVIDER=twilio` and shows **no** provider prompt. Only when *no* provider credential is set does it prompt, and pressing Enter there defaults to **ACS**. The model prompt is skipped when `AZURE_VOICE_LIVE_MODEL` is set. | `hooks/preprovision.ps1` 32–37, 139–166, 367–371 |
| `hooks/predeploy.ps1` only fills `TELEPHONY_PROVIDER` when it's blank. It does not correct a stale `acs` value. | `hooks/predeploy.ps1` 6–9 |
| `hooks/postdeploy.ps1` dispatches to `providers/twilio.postdeploy.ps1`. That script uses **no** `az` commands (only `azd env get-value` and Twilio's REST API). **If `TWILIO_ACCOUNT_SID` is set, it overwrites the Twilio number's `VoiceUrl` with the Container App's `/voice` URL automatically**, without recording the previous value. With more than one voice-capable number it prompts with `Read-Host`. If `TWILIO_ACCOUNT_SID` is unset, it prints the URL and exits 0. `azure.yaml` runs postdeploy with `continueOnError: true`. | `hooks/postdeploy.ps1` 8–18; `hooks/providers/twilio.postdeploy.ps1` 22–28, 60–73, 87–103; `azure.yaml` 29–33 |
| Runtime provider detection: `server.py` computes `_acs_active = bool(ACS_CONNECTION_STRING)` and `_twilio_active = bool(TWILIO_AUTH_TOKEN) and not _acs_active`. Only the detected provider's routes are registered. Provider packages load in sorted order (`acs` before `twilio`), so **ACS wins when both credentials are set**. | `server/server.py` 27–33, 81–89, 113–130; `server/app/provider_registry.py` 67–77 |
| `bridge_config.py`: with Twilio active, only `AGENT_ROUTING_JSON` is required. `MEDIA_WS_TOKEN` and `ACS_COGNITIVE_SERVICES_ENDPOINT` are required only when ACS is active. | `server/app/bridge_config.py` `load_bridge_config` (the `twilio_active` / `acs_active` gates, file lines 139–153) |
| The Twilio WebSocket token is an HMAC keyed on `TWILIO_AUTH_TOKEN` (60 s TTL, bound to the called number). It is **not** keyed on `MEDIA_WS_TOKEN`. | `server/app/providers/twilio/event_handler.py` 26–35; `media_handler.py` 19, 96–118 |
| `/voice` is POST-only. It returns **403** on a bad signature (its 503 branch for an empty `TWILIO_AUTH_TOKEN` is unreachable in practice: with an empty token the Twilio routes never register at all, so the request gets a 404), and on a route miss returns `<Say>` + `<Hangup>` TwiML without opening the media stream. `/twilio/ws` closes with 4404 on a route miss. | `server/app/providers/twilio/__init__.py` 48–76, 94–103 |
| `config_validator.py` (unmodified upstream) passes because Bicep always sets `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID` (D-031). `validate_config` also requires the provider's `required_config` (`TWILIO_AUTH_TOKEN`) or the Twilio routes are not registered. | `server/app/config_validator.py` 32–39, 57–69; `containerapp.bicep` 179–182; `providers/twilio/__init__.py` 21–26 |
| **`server/.dockerignore` does not exclude `.env`** (it ignores `.venv/`, caches, logs, and `../../docs/` only; unmodified upstream). A local `server/.env` exists on the deploy machine with keys including `TWILIO_AUTH_TOKEN` and `AZURE_VOICE_LIVE_API_KEY` (key *names* checked only, values not read; per the UT02 spec, the latter is `hireastra-resource`'s real key for local testing). The Dockerfile's `COPY *.py *.md` / `app` / `static` does not copy `.env` into the image. | `server/.dockerignore`; `server/Dockerfile` 29–31; `git diff upstream/main HEAD -- server/.dockerignore` is empty |
| `roleassignments.bicep` grants role GUID `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` on the Key Vault under the name "Key Vault Secrets User". U14a's `cso` review identified that GUID live as **Key Vault Secrets Officer** (read/write/delete secrets). That is an upstream mislabel U14a correctly left out of scope (D-016). | `infra/modules/roleassignments.bicep` 11–18; PR #33 body ("Upstream (out of scope)") |
| `containerregistry.bicep` defaults to `adminUserEnabled: true` and `sku: Standard`. Container resources are 2.0 vCPU / 4.0 GiB. | `containerregistry.bicep` 6, 13–15; `containerapp.bicep` 244–247 |
| `.azure` is gitignored. That's where azd stores env values, including `TWILIO_AUTH_TOKEN`, in plain text. | `.gitignore` line 166 |
| **`azd provision` always resets the container image to the upstream placeholder.** `main.bicep` passes a hardcoded `imageName: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'`, and `containerapp.bicep` uses `imageName` whenever it is non-empty. The `fetchLatestImage` module is declared but its output is never used (dead code). So any `provision` after a `deploy` replaces the bridge with hello-world. That image listens on port 80, not the app's `targetPort: 8000`, so the revision looks unhealthy. | `infra/main.bicep` 278; `infra/modules/containerapp.bicep` 60–66, 168 |
| **Two stale ACS azd environments exist on the deploy machine:** `.azure/u14a-preview` and `.azure/u14b-preview`. Both have `TELEPHONY_PROVIDER="acs"` and `EXISTING_ACS_NAME="hireastra-voice-pilot-acs"`. `.azure/config.json` has `defaultEnvironment: "u14b-preview"`, so an azd command run without `-e` targets an ACS environment that would adopt and full-PUT the real ACS resource. | `.azure/config.json`; `.azure/u14a-preview/.env`, `.azure/u14b-preview/.env` (only the `TELEPHONY_PROVIDER`/`EXISTING_ACS_NAME` lines were read) |
| `roleassignments.bicep` seeds the Key Vault role assignment's name with the **literal string** `'Key Vault Secrets User'`, not the role GUID. | `infra/modules/roleassignments.bicep` 12 |
| `monitoring/monitor.bicep` (upstream) still **creates** an Application Insights component and dashboard. Its connection string is output but never consumed. | `infra/modules/monitoring/monitor.bicep` 19–31 |

### Rev 4's C-series findings: status under a Twilio-first deploy

| Finding (rev 4) | Status in rev 5 | Why |
| --- | --- | --- |
| **C1** — circular dependency (registry pull and Key Vault secret references need an identity at revision-creation time) | **Still applies; already satisfied on `main`** | The Container App still pulls from ACR and resolves a Key Vault secret (now `TWILIO-AUTH-TOKEN`) at revision creation. D-031's design (a user-assigned identity for everything) is exactly what `main` provisions: `identity.bicep` is unmodified, `containerregistry.bicep` grants `AcrPull` to the user-assigned identity, and `containerapp.bicep` keys `registries[]` and every `secrets[]` entry off `identityId`. Residual first-deploy risk: RBAC propagation lag between the role assignment and revision creation (see U15). |
| **C2** — `config_validator.py` crash-loop | **Still avoided** | `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID` is always populated (D-031). U-CFG stays retired. |
| **C3** — ACS text-to-speech role on the ACS resource's identity | **Not applicable** | ACS never answers or speaks. The Bicep is gated off under `twilio`, and per PR #33's stated verification it was never applied live (see U14a below). |
| **C4** — placeholder 555 routing number | **Superseded** | The real Twilio number (D-041) is the routing key. It is set via `AGENT_ROUTING_JSON_B64` in the azd env (U14a's base64 mechanism) and never committed. |
| **C5** — cross-subscription topology | **Half still applies** | The Foundry User assignment on `hireastra-resource` (credits subscription) still needs Owner or User Access Administrator on `rg-hireastra`. The *silent Event Grid failure* half is gone, because `twilio.postdeploy.ps1` issues no `az` commands. Pinning the subscription stays as hygiene. |
| **C6** — existing resource group / ACS | **Resource group: still applies. ACS: not applicable** | `rg-hireastra-voice-pilot` is adopted. `hireastra-voice-pilot-acs` is *not* adopted under `twilio` (the ACS module is skipped). Leave `EXISTING_ACS_NAME`/`ACS_DATA_LOCATION` unset (see T1). |
| **C7** — U15 needs U10–U13 | **Superseded** | U15 exercises the Twilio path. Its code prerequisites are **UT01a and UT01b**, both CLOSED (PRs #24, #25). U10–U13 (ACS) are irrelevant to both U15's deploy and U15b's live smoke test. |
| **C8** — monitoring | **Unchanged (D-033)** | Log Analytics container logs plus Foundry tracing. D-033 defers App Insights *instrumentation* (SDK code and connection-string wiring), not the resource. Upstream `monitor.bicep` still creates an App Insights component and dashboard, so seeing them in a preview is **expected and is not a D-033 violation**. |
| **C9** — Key Vault is RBAC-only | **Still applies** | Verify secret references via `az containerapp show` (control plane), not `az keyvault secret list`, unless a data-plane role was granted. |
| **C10** — tooling | **Revised** | The `az communication` extension is no longer needed. `azd` is present (U14a ran azd 1.34.2). **`pwsh` is not installed on the deploy machine** (per U14a's audit notes), and every azd hook runs with `shell: pwsh`, so install it before U15. |
| **C11** — cost | **Still applies** | Same resources as before, minus ACS usage. Sizing and ACR tier stay the founder's call (U14c). |

### New findings specific to a Twilio-first deploy (T-series)

- **T1 — the provider choice must line up in three places, and ACS wins a tie.** (a) The Bicep parameter `telephonyProvider` (from `TELEPHONY_PROVIDER`, **default `acs`** in `main.parameters.json`). (b) The Docker build arg `TELEPHONY` (same env var, via `azure.yaml`). (c) Runtime detection in `server.py`.
  - If `TELEPHONY_PROVIDER` resolves to `acs`, the **silent, lasting damage is at the infra level**. Bicep deploys the ACS module, which **adopts and full-PUTs** `hireastra-voice-pilot-acs` if `EXISTING_ACS_NAME` is set (replacing its tags, resetting undeclared properties, and turning on its identity), or **creates a second ACS resource** if it isn't. The ACS connection string lands in Key Vault and the container env, and the image is built without the `twilio` package.
  - The *app* failure is loud, not silent. With `ACS_CONNECTION_STRING` set, `bridge_config.py`'s ACS gate requires `MEDIA_WS_TOKEN`, which Bicep never sets, so the container exits at startup with `BridgeConfigError` and crash-loops. Either way, every Twilio call fails.
  - Two mechanisms help, and neither is sufficient alone. `preprovision.ps1` forces `TELEPHONY_PROVIDER=twilio` whenever `TWILIO_AUTH_TOKEN` is in the azd env. And **both** stale scratch environments, `u14a-preview` and `u14b-preview` (the current `defaultEnvironment`), are ACS environments that must **not** be reused (T12).
  - U15 therefore makes this an explicit, checked invariant: `TELEPHONY_PROVIDER=twilio` is set, the `azd provision --preview` output contains **no** `Microsoft.Communication/*` resource and no `ACS-CONNECTION-STRING` secret, and the running container has `TWILIO_AUTH_TOKEN` and **no** `ACS_CONNECTION_STRING`. The same invariant satisfies TELEPHONY_BRIDGE_SPEC.md §7 ("do not configure a second telephony provider").
- **T2 — pointing the Twilio number at the Container App is a live production cutover.** The Twilio number is the one production number (D-041), and today it points at the laptop tunnel. `twilio.postdeploy.ps1` overwrites its `VoiceUrl` the moment `azd deploy` finishes if `TWILIO_ACCOUNT_SID` is set, before any verification and without recording the old URL. U15 leaves `TWILIO_ACCOUNT_SID` **unset**, so the hook just prints the URL. The cutover is a separate, founder-confirmed unit (**U15b**) that runs after U15's pre-cutover checks pass. The number's full voice config is recorded first, and rollback re-points the number to a *freshly read* tunnel URL, because a Cloudflare quick-tunnel URL changes on every restart (Q-040's own failure mode). A recorded old URL is not a valid rollback target. *When* to cut over was the founder's call. Q-045 is answered (b): the cutover happens after Friday's demo, on the founder's own timeline. The staging-number option is deferred.
- **T3 — a remote build would upload the local `.env` to Azure.** `azure.yaml` sets `remoteBuild: true`, so azd packages the `server/` build context and sends it to ACR Tasks. `server/.dockerignore` does not exclude `.env`, and the founder's local `server/.env` holds `TWILIO_AUTH_TOKEN` (a full-account Twilio credential) and `AZURE_VOICE_LIVE_API_KEY` (a real `hireastra-resource` key). The Dockerfile does not copy `.env` into the *image*, so the exposure is the uploaded build-context archive in Azure-managed storage, not the running container. *Confidence: high that `.env` is not excluded (file read). Medium that azd's remote-build upload honors `.dockerignore` and would otherwise include `.env`: not verified against azd's source in this session.* The one-line fix is cheap either way, and it is a small hook on an upstream file (CLAUDE.md §10). It goes in U14c, which must merge before U15's first `azd deploy`.
- **T4 — the only Key Vault secret is now a full-account credential, and the identity can write it.** Under ACS, Key Vault held an ACS connection string and `MEDIA_WS_TOKEN`. Under Twilio it holds `TWILIO-AUTH-TOKEN`, which grants full Twilio account API access, not just webhook validation. `roleassignments.bicep` grants the Container App identity `b86a8fe4-…`, identified live as **Key Vault Secrets Officer** (write/delete). Container Apps Key Vault references need only **Key Vault Secrets User** (read). U14b narrows this.
- **T5 — Twilio signature validation depends on the URL the app reconstructs behind ingress.** `TwilioEventHandler._reconstruct_url` forces `https`, keeps the hostname from the request, and drops the port. `/voice` derives the `wss://` stream URL from `request.host_url`. Container Apps ingress is expected to preserve the `Host` header as the app FQDN. That is plausible but **not verified in this session**, and it is exactly what the UT02 spec had to check for the tunnel. A mismatch shows up as 403s on real calls (visible in the Twilio Debugger as a failed webhook). The unauthenticated `curl` checks in U15 prove the routes are registered and that unsigned requests are rejected. Only a real call (U15b) proves a *valid* signature is accepted.
- **T6 — WebSocket duration through Container Apps ingress is unverified.** A call is one long-lived WebSocket (up to `MAX_CALL_SECONDS`, 600 s). Whether Container Apps ingress imposes a maximum connection or request duration shorter than that was **not verified** in this planning session. The longest live call so far (UT02) was about 196 s. U15b includes one deliberately long call of at least 6 minutes. If calls drop at a fixed wall-clock time, that's a finding to investigate before the pilot relies on long intakes.
- **T7 — deploying a new revision drops calls in progress.** `activeRevisionsMode: 'Single'` and one replica mean an `azd deploy` or configuration change replaces the only running instance. Operational rule for U15, U15b and after: never deploy during a live call. (Friday's demo runs on the laptop path per Q-045, so cloud deploys don't affect it.) `azd provision` counts as a deploy here (T11).
- **T8 — a stable public FQDN slightly raises Q-034's exposure.** An unauthenticated peer can hold `/twilio/ws` open indefinitely by repeating `"connected"` events (Q-034, pre-existing, not a call-hijack or billing risk). A permanent, discoverable FQDN on a single replica makes that marginally easier to exploit than a rotating tunnel URL. Not blocking for M6. Q-034's own "before sustained production traffic" timing stands, and this note is added to it.
- **T9 — azd stores the Twilio token in plain text on the deploy machine.** Any secure parameter set with `azd env set` lands in `.azure/<env>/.env` (gitignored, local only). That's acceptable for a founder-operated machine, but the token must be entered through a secure prompt rather than typed on a command line (shell history). U15 gives the exact pattern.
  - Residual exposure: `azd env set <NAME> <value>` still receives the value as a command-line argument, so it is briefly visible in the `azd.exe` process command line (e.g. to another process on the same machine listing processes) while the command runs. This was not checked for a stdin/piped-input form of `azd env set` in this session. If the builder finds one in `azd env set --help`, prefer it; otherwise the brief argv exposure is accepted for a single-user founder machine.
  - To avoid a second plaintext copy, only U15's real environment ever holds the real token. U14b uses a dummy value in a throwaway environment.
- **T10 (for M7, not M6) — two ACS-era acceptance tests don't map directly onto Twilio's behavior.** TELEPHONY_BRIDGE_SPEC.md §8 test 7 expects the caller to *hear the fallback message* on a wrong agent version, and test 8 expects a *spoken goodbye* at the call cap. On the Twilio path, a Voice Live connect failure and the call cap both end the call through `TwilioCallSession.request_end`, which closes silently by design (twilio-pilot-plan B2: `message` is accepted but not spoken). Only a *route miss* speaks the fallback, via `<Say>`. Recorded here so M7's re-scope picks it up. Nothing in M6 changes it.
- **T11 — `azd provision` silently replaces the live bridge with hello-world (blocking-class operational rule).** See the evidence row: `main.bicep` 278 hardcodes the MCR hello-world image, and `containerapp.bicep` 168 always uses it. Any `azd provision` after the first `azd deploy` (a Q-041 role narrowing, a sizing change, a U15 retry, a token rotation done "the azd way") swaps the running bridge for a placeholder listening on the wrong port. The Twilio number then dead-ends every call until someone runs `azd deploy` again. **Standing rule (recorded in the rev-5 decision entry):**
  - **`azd provision -e <env>` is always immediately followed by `azd deploy -e <env>` in the same maintenance window.**
  - It never runs during live-call hours.
  - It is never left mid-sequence.
  - After the pair, `GET /health` must return 200 before the window closes.

  Fixing the root cause (stop hardcoding `imageName` so `fetchLatestImage` takes effect) would be an upstream-Bicep change. It is not in M6 scope, and should be a proposed Q-NNN for production hardening.
- **T12 — the default azd environment is a stale ACS environment.** `.azure/u14a-preview` and `.azure/u14b-preview` both carry `TELEPHONY_PROVIDER=acs` plus `EXISTING_ACS_NAME=hireastra-voice-pilot-acs`, and `u14b-preview` is `defaultEnvironment`. Any untargeted azd command (`azd provision`, `azd up`, `azd deploy`) would run the ACS path against the real ACS resource (T1). Two consequences:
  1. **U14b's first precondition** is to delete both stale environments.
  2. **Every azd command in U14b, U14c, U15 and U15b is written with `-e <env>`**, and **the first check in each unit is that `azd env get-value TELEPHONY_PROVIDER -e <env>` prints `twilio`.**

### Spec drift (recorded, not fixed here)

TELEPHONY_BRIDGE_SPEC.md §6 and §8 still describe the ACS design: the Event Grid row, the Key Vault row ("ACS connection string and media WebSocket token"), "Raghu calls the test number", and a system-assigned identity (already superseded by D-031). D-041 supersedes those rows for the call path. This plan treats D-041 as authoritative, and the partner should record that explicitly (see "Proposed decision entries" below) rather than edit the founder's spec silently.

---

## Units

The same discipline as M0–M5 applies: **one unit = one branch = one PR**, a founder "go" before the builder starts, Opus code review and `cso` before merge, and STATUS.md updated inside the PR. **Every implementation unit runs on Opus, unconditionally** (CLAUDE.md §0 / D-034). Rev 4's "Model: Sonnet" lines for U14a, U14c and U15 predate D-034 and are corrected here. "Tests" for infra units are verification commands and their expected output.

**Sequence (rev 5.1):** U14a (CLOSED) → **U14b (re-scoped)** → **U14c (re-scoped)** → **U15 (deploy + pre-cutover verification)** → **U15b (live Twilio smoke test + production cutover; new in rev 5.1)**. U14d, U-CFG and U16 are retired.

### Standing operational rules for every azd command in U14b, U14c, U15 and U15b

These come from T11/T12 and apply to every step below. They are recorded in the rev-5 decision entry.

1. **Always target the environment explicitly.** Every azd command is written as `azd <command> -e <env>`. Never rely on `defaultEnvironment`: today it is the stale ACS environment `u14b-preview` (T12).
2. **The first check in every unit, before anything else:** `azd env get-value TELEPHONY_PROVIDER -e <env>` must print `twilio`. If it prints anything else, or errors, stop.
3. **`azd provision -e <env>` is always immediately followed by `azd deploy -e <env>`, in the same maintenance window.** Never during live-call hours, and never left mid-sequence. After the pair, `GET https://<fqdn>/health` must return 200 before the window closes (T11: provisioning resets the app to the hello-world placeholder).
4. **Never run `azd down`** against any environment that adopts `rg-hireastra-voice-pilot` (Q-042).
5. **Never deploy during a live call** (T7).

### U14a — CLOSED (PR #33, merged `8f4d495`) — what still matters under Twilio

- **Still load-bearing:** adopting `rg-hireastra-voice-pilot`, referencing `hireastra-resource` cross-subscription (no second AI account), Foundry User on the user-assigned identity via `airoleassignments.bicep`, scale fixed at 1/1, `VOICE_LIVE_ENDPOINT`, and `AGENT_ROUTING_JSON_B64` → `AGENT_ROUTING_JSON`.
- **Dormant under `telephonyProvider = 'twilio'`:** `acs.bicep`'s system-assigned identity and adoption of `hireastra-voice-pilot-acs`, the ACS text-to-speech role in `airoleassignments.bicep`, `ACS_COGNITIVE_SERVICES_ENDPOINT` and the ACS connection-string secret. All of it is gated in Bicep, and none of it deploys under `twilio`. `ACS_CALLBACK_JWT_AUDIENCE` stays an inert optional env var: it is empty by default, and the app registers no ACS routes that would read it.
- **Decommissioning recommendation.** This answers Q-044's "whether U14a's now-dead ACS TTS/identity wiring should eventually be decommissioned". It is low priority and blocks nothing.
  1. **There appears to be nothing to decommission in Azure from U14a.** PR #33's stated verification commands were all read-only (see the hedged evidence row above). Cowork can confirm with one read-only query at any time.
  2. **Leave the dormant ACS Bicep in place; do not revert PR #33.** It is inert under `twilio`. Reverting would cost a full unit (a branch, two reviews and a PR) plus new upstream-divergence churn in `main.bicep` and `acs.bicep`, for zero runtime effect. Keeping it also keeps the ACS path intact for a possible future client, and the spec (§7) says to leave the second provider's code in place, unused.
  3. **The pre-existing `hireastra-voice-pilot-acs` resource** (created by Cowork before U14a, not by this repo) sits idle, with no number and no Event Grid subscription. Whether to delete it, and whether to close Azure support ticket 2609250040002301, is a founder housekeeping call (proposed Q-046). **Recommendation:** close the support ticket now, since no ACS number will be bought. Leave the resource idle until production hardening, then delete it if nothing has used it. Never delete it with `azd down` (Q-042).

### U14b — Container App identity: reconfirm D-031 for a Twilio deploy, narrow Key Vault to Secrets User (re-scoped in rev 5)

**Branch:** STATUS.md §1 currently names this branch `feat/tb-m6-u14b-system-identity`. That name was already misleading under D-031, and is more so now. The partner recommends renaming it to `feat/tb-m6-u14b-identity-least-privilege` in the same status commit that un-pauses this unit. No branch exists yet (the earlier WIP was discarded, per D-041). · **Model: Opus** (CLAUDE.md §0).

**Prerequisites:**
- U14a merged (true).
- **T12 cleanup, before any other azd command. Deleting both stale ACS environments is the required path; re-pointing is not an alternative.**
  - Remove the `.azure/u14a-preview` and `.azure/u14b-preview` folders.
    - Both are local-only and gitignored.
    - The `.env` files inside them should hold no real secrets, because they were preview-only environments. Confirm this with a key-name check, without reading the values.
  - Re-pointing the default instead was dropped in rev 5.1's re-review, for two reasons:
    - it leaves both ACS environments in place, so the evidence below can't be produced;
    - there is no surviving environment to point at yet. U14b's throwaway environment is created later and then deleted, so the default would end up pointing at a deleted environment.
  - After deletion, `.azure/config.json`'s `defaultEnvironment` may still name `u14b-preview`. That is harmless once the folder is gone, because every command carries `-e`. The builder may also clear it or leave it; either way, record which.
  - Evidence to record in the PR:
    - `azd env list` shows **no** environment with `TELEPHONY_PROVIDER=acs`, and neither `u14a-preview` nor `u14b-preview` exists;
    - `defaultEnvironment` does not name an existing ACS environment.

**Is U14b still needed? Reasoning from first principles** (coordinator brief item 5; not a Q-044 sub-item).
- **Yes. The requirement stands whatever the provider.** The Container App must:
  1. pull its image from ACR;
  2. resolve a Key Vault secret reference when the revision is created (now `TWILIO-AUTH-TOKEN`);
  3. authenticate to Voice Live in agent mode.

  All three need an identity. (1) and (2) are C1's chicken-and-egg cases. None of this depends on how the call arrives.
- **But its original build scope is already on `main`.** D-031 chose "keep the accelerator's user-assigned identity for everything", which is the accelerator's unmodified default for (1) and (2). U14a added the only missing piece for (3): Foundry User on the user-assigned identity, cross-subscription. Re-reading the files confirms there is no resequencing left to write:
  - `identity.bicep` is unmodified.
  - `containerregistry.bicep` 37–47.
  - `containerapp.bicep` 87–90, 100–120, 179–182.
  - `airoleassignments.bicep` 30–38.
  - `main.bicep` 280: `dependsOn: [RoleAssignments, aiRoleAssignments]`.
- **So U14b is un-paused with a narrower, honest scope — neither retired nor rebuilt as before.** Rev 4's DoD item "validate from a clean, never-provisioned scratch environment" moves into U15, for three reasons:
  1. U15's real provision and deploy into `rg-hireastra-voice-pilot` create the Container App, Key Vault, ACR and user-assigned identity for the first time. That *is* a first deploy, so it exercises both halves of C1: the Key Vault half at provision time, and the ACR half at deploy time (see N5 in U15's step 3).
  2. A separate scratch deploy would cost real money. Because of `enablePurgeProtection: true`, it would also leave a Key Vault name reserved for up to 90 days.
  3. It would prove nothing that U15's deploy does not.

  This is a partner sequencing and design call under D-030, recorded in the rev-5 decision entry.

**What it does:**
1. **Key Vault least privilege (T4).** In `infra/modules/roleassignments.bicep`, replace role GUID `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` with the **Key Vault Secrets User** role GUID. U14a's `cso` review identified `b86a8fe4-…` live as Key Vault Secrets **Officer**.
   - **Confirm both GUIDs live** with `az role definition list --name "Key Vault Secrets User" --query "[].name"` (and the same query for Officer), and paste the output into the PR. Do not rely on memory or on this plan.
   - **Correction from the review:** the role assignment's `name` is `guid(keyVault.id, identityPrincipalId, 'Key Vault Secrets User')`. It is seeded with that **literal string**, not the role GUID (`roleassignments.bicep` 12). So changing only `roleDefinitionId` keeps the **same** assignment name. On any environment where the old Officer assignment already exists, ARM would reject the change with `RoleAssignmentUpdateNotPermitted`, because a role assignment's role cannot be updated in place.
   - This is harmless on U15's fresh environment, since no assignment exists yet. To make the change unambiguous anyway, the builder chooses one of these and states the choice in the PR:
     - **(a)** Also change the `guid()` seed, for example to include the Secrets User role GUID. On an existing environment this creates a new assignment and leaves the old Officer one orphaned, to be deleted manually.
     - **(b)** Keep the seed, and document that any pre-existing environment must have its old Officer assignment deleted before re-provisioning.

     Either choice is acceptable. What matters is that the PR states which one, and why.
2. **Twilio-mode verification of D-031's shape (no code change expected).**
   - **The first check:** `azd env get-value TELEPHONY_PROVIDER -e <throwaway env>` prints `twilio`.
   - **Environment:** create a **throwaway** azd environment (e.g. `azd env new u14b-twilio-preview`), shaped like U15's environment list but with an **obvious dummy Twilio token**. For example, `azd env set TWILIO_AUTH_TOKEN 00000000000000000000000000000000 -e u14b-twilio-preview`. A preview only needs a non-empty value to exercise the secret and `secretRef` wiring; the real token is never used here (T9). Leave the ACS-specific variables unset. Use a dummy base64 routing value too.
   - **Run:** `azd provision --preview -e u14b-twilio-preview`. This is a preview only, so rule 3's provision-then-deploy pairing doesn't apply.
   - **Confirm from the plan output:**
     - The Container App has **only** the user-assigned identity.
     - `registries[].identity` and the `twilio-auth-token` secret's `identity` both equal that identity.
     - `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID` is populated.
     - Foundry User is granted to the user-assigned identity's principal on `hireastra-resource`, cross-subscription.
     - `AcrPull` is granted to the same principal.
     - The Key Vault role is Secrets User after step 1.
     - **No `Microsoft.Communication/*` resource, no `ACS-CONNECTION-STRING` secret, and no ACS text-to-speech role assignment** (T1).
     - No Cognitive Services account is created.
     - An App Insights component and dashboard do appear. This is expected: upstream `monitor.bicep` creates them, and it is not a D-033 violation (see C8).
   - **Cleanup:** delete the throwaway environment (`.azure/u14b-twilio-preview`) once the PR's evidence is captured. **U15 creates its own separate environment, with the real token, and never reuses this one.**
3. **No Container App system-assigned identity is added.** Nothing consumes one (D-031). The PR states this explicitly.

**Files touched (expected):** `infra/modules/roleassignments.bicep` only. It was already modified by U14a, so it is already within D-032's scoped exception.

**DoD:**
- [ ] The T12 cleanup is done. Both `u14a-preview` and `u14b-preview` are deleted, `azd env list` shows no environment with `TELEPHONY_PROVIDER=acs`, and `defaultEnvironment` does not name an existing ACS environment.
- [ ] The first check (`TELEPHONY_PROVIDER` prints `twilio`) is shown in the PR.
- [ ] Both Key Vault role GUIDs are confirmed live via `az role definition list`, with the output in the PR body. The role-assignment naming choice, (a) or (b) from step 1, is stated.
- [ ] `az bicep build --file infra/main.bicep` passes.
- [ ] The `azd provision --preview` output (secret values redacted) shows every item in step 2, including the T1 negatives. It was produced with a dummy token, in a throwaway environment that has since been deleted.
- [ ] The PR states that the first-deploy (C1) proof is deferred to U15 by design, and why, citing the rev-5 decision entry.
- [ ] Opus code review and `cso` (identity/secret boundary).
- [ ] STATUS.md is updated inside this PR. No new D-NNN is needed for D-031 itself.

### U14c — Secrets hygiene for remote build, container and ACR sizing (re-scoped in rev 5)

**Branch:** `feat/tb-m6-u14c-secrets-and-sizing` · **Model: Opus** (CLAUDE.md §0).

**Prerequisite:** U14b merged. That includes its T12 cleanup, so no stale ACS environment exists.

**First check (standing rule 2, applied to U14c's throwaway preview environment):** create a throwaway environment shaped like U14b's (dummy token, `TELEPHONY_PROVIDER=twilio`, ACS vars unset). Then confirm that `azd env get-value TELEPHONY_PROVIDER -e <throwaway env>` prints `twilio` before any other azd command. Delete the environment once the PR's evidence is captured.

**What changed against rev 4, and why.** This answers Q-044's "what U14c ... actually need[s] to build against a Twilio-first deploy". U14c was three items, and none was ACS-specific in *intent*. Checked one by one against the Twilio deploy:
- **`MEDIA_WS_TOKEN` in Key Vault: dropped.** Under Twilio, no code path reads it. `bridge_config.py` requires it only when ACS is active, and the Twilio WebSocket token is keyed on `TWILIO_AUTH_TOKEN` (see the evidence table). Adding an unread secret, plus a `preprovision.ps1` generator for it, would widen the upstream-divergence footprint (D-032) for nothing. If ACS is ever reactivated, this item comes back with it. It also removes `infra/modules/keyvault.bicep` and `hooks/preprovision.ps1` from M6's expected list of touched files.
- **Foundry User role GUID verification: already done.** D-039 records that U14a's `cso` review reconfirmed `53ca6127-db72-4b80-b1b0-d745d6d5456d` live via `az role definition list`, and `airoleassignments.bicep` 23 uses it. Nothing to repeat. Narrowing that role's *scope* is Q-041, which stays open and separate.
- **Right-sizing: unchanged. It is still the founder's cost call (C11).**
- **New: the `.dockerignore` fix (T3).** It must land before U15's first remote build.

**What it does:**
1. **Exclude `.env` from the Docker build context (T3).** Add `.env` and `.env.*` to `server/.dockerignore`. Keep `!.env.sample` if the sample is wanted in the context; it is not copied into the image either way. This is a small hook on an unmodified upstream file (CLAUDE.md §10); note it in the PR as an upstream-divergence line item.

   **Verification:** show that the build context excludes `.env`, for example with a *local, uncommitted* `docker build` that uses a throwaway `RUN ls -la`. If Docker isn't available on the deploy machine, cite Docker's documented `.dockerignore` semantics instead, and flag the claim as not executed. Don't claim it was verified if it wasn't.
2. **Right-size the container and ACR (C11), and present the result to the founder rather than applying it silently.**
   - **Container:** in `containerapp.bicep`, change `resources` from 2.0 vCPU / 4.0 GiB to the partner's recommendation of **1.0 vCPU / 2.0 GiB**. That is a step up from rev 4's 0.5/1.0 suggestion. The Twilio path does per-frame μ-law↔PCM conversion and 8k↔24k resampling in Python (`audioop.ratecv`) on the call's hot path, and none of it has been profiled. A starved CPU shows up to the caller as choppy audio, which is harder to diagnose than a cost line. Container Apps only accepts specific vCPU/memory pairings, so confirm the pairing is valid.
   - **ACR tier:** in `containerregistry.bicep`, change `sku` from Standard to Basic.
   - **ACR admin user:** change `adminUserEnabled` from `true` to `false`. The Container App pulls images through `AcrPull` on the managed identity (C1), and azd's remote build uses ACR Tasks under the deploying principal, so the admin user is unused. *Before relying on this, the builder confirms that azd's remote build still works with admin disabled, using documented azd behavior. It must not run `azd deploy` against a throwaway environment, because that would require a real provision. If this can't be confirmed, leave it enabled and log why.*
   - **Cost:** re-estimate the monthly cost with the Azure pricing calculator at unit time, rather than reusing rev 4's rough figures. The founder chooses. Log the choice in the PR and STATUS.md, including if the choice is "keep the accelerator's defaults".
3. **Optional, only if the founder asks:** set `MAX_CONCURRENT_CALLS` to a value that matches the chosen sizing. It defaults to 50 in `server.py` and is not in Bicep today, so this would mean a new plain env var in `containerapp.bicep` and `server/.env.sample`. It is not required for M6 and is not built by default.

**DoD:**
- [ ] `server/.dockerignore` excludes `.env`. The PR states the verification method, and whether it was executed or cited-and-flagged.
- [ ] The first check (`TELEPHONY_PROVIDER` prints `twilio` on the throwaway environment) is shown, and that environment was deleted afterwards.
- [ ] The sizing/ACR decision is recorded (the founder's choice, with the cost re-estimate). `azd provision --preview -e <throwaway env>` shows the chosen values, using a dummy token, same as U14b.
- [ ] If `adminUserEnabled` changed, the remote-build path is confirmed to still work. Otherwise the change is reverted and logged.
- [ ] No secret value is in any committed file.
- [ ] Opus code review and `cso` (`.dockerignore` is a secret-exposure control).
- [ ] STATUS.md/DECISIONS.md updated inside this PR.

### U14d — RETIRED (folded into U14a, 2026-09-27). Do not cut `feat/tb-m6-u14d-resource-naming`.

### U-CFG — RETIRED (D-031). Do not implement. Still true under Twilio: the Container App's `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID` is always populated.

### U15 — Deploy and pre-cutover verification (redesigned in rev 5; split from U15b in rev 5.1)

**Branch:** `feat/tb-m6-u15-deploy` · **Model: Opus** (CLAUDE.md §0; this is a live deploy against real resource groups, not rote execution).

**Scope boundary (S2):** U15 deploys the bridge and proves it is correctly configured and reachable, **without routing any real caller to it**. It merges on steps 1–6 alone. The live call and the production number cutover are **U15b**, which is timed by the founder's Q-045 answer. So U15 can always complete, whichever Q-045 option is chosen.

**Prerequisites:**
- **Code:** UT01a and UT01b CLOSED (PRs #24 and #25, the Twilio call path). **U10–U13 are not prerequisites.** This corrects rev 4's C7 and STATUS §1's U15 row: U10–U13 are the ACS path.
- **Infra units:** U14b and U14c merged. U14c must come before the first remote build (T3). U14b's T12 cleanup is already done.
- **Founder decisions:**
  - **Q-042** (CanNotDelete locks on `rg-hireastra-voice-pilot` and `rg-hireastra`, and "never `azd down`") is answered and, if accepted, applied *before* the first real provision.
  - Sizing is chosen (U14c).
  - Q-045 is **not** a U15 prerequisite; it gates U15b.
- **Access:** `az login` and `azd auth login` are active. The deploying principal has:
  - Owner or User Access Administrator on `rg-hireastra` (credits subscription `82632bb8-e34b-41be-a7c7-a134f16c1c9c`), for the Foundry User assignment;
  - deploy rights in the Voice Pilot subscription `13b3dbed-e03a-4d01-8b88-ac5c80fc749e`.
- **Tooling (C10 revised):** `azd` (present), `az`, **`pwsh` (install it first; it's missing)** and `curl`. The `az communication` extension is **not** needed.
- **Maintenance window:** pick a time with no expected calls. The real Twilio number keeps pointing at the laptop throughout U15, so U15 cannot break live calls. The window is for the provision-then-deploy pairing (rule 3).

**What it does:**

1. **Create U15's own fresh azd environment.** Never reuse `u14a-preview`, `u14b-preview` or U14b's throwaway environment (T1, T12). Every command targets it explicitly with `-e`.
   ```powershell
   azd env new <env-name>                                   # a new name; Key Vault purge protection reserves names (see below)
   azd env set AZURE_SUBSCRIPTION_ID 13b3dbed-e03a-4d01-8b88-ac5c80fc749e -e <env-name>
   azd env set AZURE_LOCATION eastus2 -e <env-name>
   azd env set EXISTING_RESOURCE_GROUP_NAME rg-hireastra-voice-pilot -e <env-name>
   azd env set EXISTING_AI_SERVICES_NAME hireastra-resource -e <env-name>
   azd env set EXISTING_AI_SERVICES_SUBSCRIPTION_ID 82632bb8-e34b-41be-a7c7-a134f16c1c9c -e <env-name>
   azd env set EXISTING_AI_SERVICES_RESOURCE_GROUP rg-hireastra -e <env-name>
   azd env set TELEPHONY_PROVIDER twilio -e <env-name>
   azd env set AZURE_VOICE_LIVE_MODEL gpt-4o-mini -e <env-name>   # skips preprovision's model prompt; agent mode ignores it
   azd env set MAX_CALL_SECONDS 600 -e <env-name>
   azd env set AMBIENT_PRESET none -e <env-name>
   # FALLBACK_MESSAGE optional (must not contain " or \ — U14a finding)
   # Twilio auth token: the founder enters it, never on a command line or in chat (T9; see T9's note on brief argv exposure):
   $t = Read-Host "Twilio Auth Token" -AsSecureString
   azd env set TWILIO_AUTH_TOKEN ([Net.NetworkCredential]::new('', $t).Password) -e <env-name>
   # Routing: the real Twilio number (D-041) → agent route, base64-encoded (U14a mechanism), never committed.
   # Q-045 was answered (b): no staging number in M6. Option (d) is deferred; if adopted later, U15b adds it (U15b step 2).
   $json = '{"<E.164 Twilio number>": {"project": "hireastra", "agent": "re-intake-pilot-agent", "version": "24"}}'
   # Confirm the exact published version in the Foundry portal (Build tab, top of the agent page) immediately
   # before this step — this pin has drifted silently before (was "10" in docs, "11" on the laptop, while the
   # real published version was 24). Never trust this file's own number without checking the portal first.
   azd env set AGENT_ROUTING_JSON_B64 ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))) -e <env-name>
   ```
   **Deliberately left unset:**
   - `EXISTING_ACS_NAME` and `ACS_DATA_LOCATION`. They are inert under `twilio`. Leaving them unset also means an accidental `acs` value would show up in the preview as a *new* ACS resource, which is easy to spot, rather than as a silent full-PUT on the existing one.
   - `ACS_CALLBACK_JWT_AUDIENCE`, because no ACS routes exist.
   - `TWILIO_ACCOUNT_SID` (T2). This stops the postdeploy hook from repointing the production number.
   - `ENABLE_WEB_CLIENT` (D-007/D-028).
   - `AZURE_VOICE_LIVE_API_KEY` (D-004: Entra ID only in the deployed environment).

   The agent version is a pinned digit string, never `"latest"` (D-004).
2. **The first check, then pin the subscription.**
   - `azd env get-value TELEPHONY_PROVIDER -e <env-name>` must print `twilio`. Stop if it doesn't.
   - Then run `az account set --subscription 13b3dbed-…`. Rev 4's reason for this step, the silent Event Grid failure, no longer applies. But `preprovision.ps1` still reads the ambient `az` account (line 24), and being explicit costs nothing.
3. **Preview, then provision. This proves C1's Key Vault half.**
   - **Preview:** `azd provision --preview -e <env-name>` is a **hard gate**. Stop unless its output shows:
     - no `Microsoft.CognitiveServices/accounts` creation;
     - no `Microsoft.Communication/*` resource;
     - no `ACS-CONNECTION-STRING` secret;
     - a `TWILIO-AUTH-TOKEN` secret;
     - the resource group as Skip/Ignore;
     - Foundry User granted cross-subscription to the user-assigned identity;
     - Key Vault Secrets User.

     The App Insights component and dashboard are expected (C8).
   - **Provision:** then run `azd provision -e <env-name>`. There should be no provider prompt, because `TWILIO_AUTH_TOKEN` is set. **If a provider-selection prompt appears, abort; do not press Enter.** Enter selects ACS, which means `TWILIO_AUTH_TOKEN` is missing from the environment.
   - **What this proves:** this provision is the first-ever creation of the Container App, Key Vault and identity, so resolving the `twilio-auth-token` Key Vault reference proves **C1's Key Vault half**. It does **not** prove the ACR half. The provisioned revision pulls the public MCR hello-world image (T11), not an image from ACR, so `AcrPull` is only exercised in step 4.
   - **Expected, not a failure (B1):** right after provisioning, the revision runs the **hello-world placeholder** against `targetPort: 8000`. Hello-world listens on port 80, so the revision looks **unhealthy, or not ready, whatever the state of RBAC**. Do not misdiagnose this as an RBAC-propagation failure, and do not re-provision because of it. Go straight to step 4.
   - **The genuine RBAC transient to watch for is different:** provisioning itself fails with a Key Vault secret-reference resolution error. RBAC can lag between the role assignment and revision creation. If that happens, wait a few minutes and re-run `azd provision -e <env-name>` once; step 4 still follows. Record it if it happens. A second failure is a real finding (see the stop rule below).
4. **Deploy the image, immediately after step 3 (rule 3). This proves C1's ACR half.**
   - Run `azd deploy -e <env-name>`. The build uses `TELEPHONY=twilio` (T1) and a context free of `.env` (T3, after U14c).
   - This is the first pull of the bridge image from ACR through the user-assigned identity's `AcrPull`, which proves **C1's ACR half**.
   - The postdeploy hook prints the `/voice` URL and exits, because `TWILIO_ACCOUNT_SID` is unset.
   - `GET /health` must return 200 before moving on.
5. **Pre-cutover verification.** The real number still points at the laptop tunnel. Paste all output into the PR, with secrets and `AGENT_ROUTING_JSON` values redacted.
   - **`az containerapp show`:**
     - `scale.minReplicas == maxReplicas == 1`, ingress is external, and the FQDN is present.
     - The image is the bridge from ACR, **not** hello-world.
     - The identity type is `UserAssigned` only.
     - `secrets[]` holds exactly `twilio-auth-token`, with a `keyVaultUrl` (C9; print no values).
     - The env has `TWILIO_AUTH_TOKEN` (via `secretRef`), `AGENT_ROUTING_JSON`, `VOICE_LIVE_ENDPOINT` and `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID`.
     - The env has **none** of `ACS_CONNECTION_STRING`, `ACS_COGNITIVE_SERVICES_ENDPOINT`, `MEDIA_WS_TOKEN`, `ENABLE_WEB_CLIENT` or `AZURE_VOICE_LIVE_API_KEY`.
   - **Role assignments** on the user-assigned identity's principal: Foundry User on `hireastra-resource` (cross-subscription), Key Vault Secrets User on the vault, and `AcrPull` on the registry.
   - **Startup logs** (`az containerapp logs show`) include `Telephony provider: twilio`, `Configuration validated for provider=twilio` and `Registered routes for provider: Twilio`. They must **not** include `Multiple telephony credentials detected` or `Telephony routes not registered`.
   - **HTTP probes.** These prove routing and auth without placing a call:
     - `GET https://<fqdn>/health` returns **200**.
     - `POST https://<fqdn>/voice` with no signature returns **403**. A **404** means the Twilio routes are not registered: either `TWILIO_AUTH_TOKEN` is missing or empty in the container (no provider is detected, and `validate_config` fails its `required_config`), or ACS took precedence (T1). The 503 branch in `/voice` is effectively unreachable in a deploy, because an empty token means the route never registers.
     - `GET https://<fqdn>/voice` returns **405** (UT01a's POST-only rule).
     - `POST https://<fqdn>/acs/incomingcall` returns **404**, which proves no ACS route is live (Q-005/Q-043; see below).
   - **Monitoring (D-033):** container logs are reaching Log Analytics, and the Foundry-side conversation tracing is confirmed on or off for the project (that setting lives outside this repo's Bicep).
6. **Record the U15b handoff.** Record the FQDN and the `/voice` URL, and confirm that the laptop/tunnel path is still the live route for the real number. Confirm the budget alert is the founder's own action (spec §6), and log it in STATUS.md as the founder's item.

**Stop rule (S6), which also applies to U15b:** if a live check fails and the fix needs a Bicep or code change — for example, U14b's or U14c's changes don't hold up live — the builder does **not** fix it inside U15 or U15b:
1. Stop.
2. Make sure the real Twilio number is on the laptop path. In U15 it never left. In U15b, roll back per U15b's rollback procedure. Never leave the real number cut over to a broken deploy.
3. Record the failure with log evidence.
4. Hand back to the partner.

The fix becomes its own new unit. U15 and U15b never turn into Bicep-fixing units mid-flight. Operational retries don't count as fixes: the single RBAC-propagation retry, and re-running the rule-3 provision/deploy pair.

**Key Vault purge protection (unchanged fact, new relevance):** if U15's environment ever has to be torn down and recreated, the new environment needs a **new** `environmentName`. The old vault name stays soft-deleted for its retention period, and no one can purge it early. **Never use `azd down` on this environment** (Q-042): it adopts `rg-hireastra-voice-pilot` and touches `rg-hireastra` cross-subscription. Remove individual resources instead.

**Twilio auth token rotation (operational note; rewritten in rev 5.1 per B1):** rotation must **not** go through `azd provision`, because that resets the app to hello-world (T11). Instead:
1. Update the Key Vault secret directly: `az keyvault secret set --vault-name <kv> --name TWILIO-AUTH-TOKEN --file <temp file>`. Read the new value into a temp file from a secure prompt, then delete the file. This needs a data-plane role on the vault (Key Vault Secrets Officer, or equivalent, for whoever rotates). Subscription Owner alone is not enough (C9), so grant it to the rotating principal just-in-time.
2. Restart the revision so the env var re-resolves at container start: `az containerapp revision restart`. This never touches the image.
3. Update the azd environment's copy too (`azd env set TWILIO_AUTH_TOKEN … -e <env-name>`, via a secure prompt), so a future provision doesn't write the old token back.
4. Do all of this outside a live call (T7). After the restart, check that `POST /voice` without a signature still returns 403, and `GET /health` still returns 200.

The laptop's `server/.env` must be updated too, if the laptop path is still in use.

**DoD:**
- [ ] A fresh azd environment is used, and the first check (`TELEPHONY_PROVIDER` prints `twilio`) is shown. Every azd command used `-e <env-name>`. The preview hard gate passed, with the T1 negatives shown.
- [ ] `azd provision` was **immediately** followed by `azd deploy` (rule 3). Any RBAC-propagation retry is recorded. The hello-world state after provisioning was not mistaken for a failure. This is **the C1 first-deploy proof**, moved here from U14b: the Key Vault half from step 3, and the ACR half from step 4.
- [ ] All step 5 checks pass, with output pasted (secrets and routing values redacted). This includes the running image being the bridge (not hello-world), `/acs/incomingcall` returning **404**, and `ENABLE_WEB_CLIENT` and `AZURE_VOICE_LIVE_API_KEY` being absent.
- [ ] The real Twilio number was **not** repointed by this unit. It still points at the laptop path.
- [ ] No phone numbers (other than masked ones), secrets, routing JSON or Key Vault secret *values* appear in the PR body or STATUS.md.
- [ ] Opus code review **and `cso`**. The diff is expected to be docs/status only, plus any runbook notes. **Running `cso` on a docs-only PR is a deliberate override of CLAUDE.md §5 step 6's docs-only skip (N6), not an oversight.** The PR carries pasted live-infrastructure verification output, and `cso` reviews that output as narrative, for leaked secrets, numbers or over-broad access.
- [ ] STATUS.md/DECISIONS.md are updated inside this PR, per D-017.

### U15b — Live Twilio smoke test and production cutover (new in rev 5.1)

**Branch:** `feat/tb-m6-u15b-cutover` (proposed; to be added to STATUS.md §1) · **Model: Opus** (CLAUDE.md §0).

**Prerequisites:**
- U15 merged.
- **Q-045: answered (b), 2026-09-28.** U15b runs after Friday's demo, on the founder's own timeline, with no deadline. The demo stays on the laptop path. No staging number is used in M6: option (d) is deferred, and its sub-steps in step 2 apply only if it is adopted at a later stage.
- The laptop bridge and tunnel are runnable, because they are the rollback target.
- Every standing rule above applies. In particular, the first check is `azd env get-value TELEPHONY_PROVIDER -e <env-name>` printing `twilio`.

**What it does:**

1. **The first check.** It prints `twilio`, and `GET https://<fqdn>/health` returns 200.
2. **Which number carries the smoke test.** Under Q-045's settled answer (b), it is the real number: step 3, then step 4. The option-(d) sub-steps below are kept for a later stage, and do **not** apply to M6 as answered.
   - **Under option (d), U15b covers both the staging smoke test and the real-number cutover, in this one unit and session.** It starts only when the founder's timing allows the real cutover, and it never merges on the staging test alone. The order is:
     1. **Add the staging number to routing, as a U15b step, if U15 didn't already include it.** Update `AGENT_ROUTING_JSON_B64` with `-e <env-name>` so it carries both numbers with the same route. Then, in a maintenance window, run the rule-3 pair: `azd provision -e <env-name>` immediately followed by `azd deploy -e <env-name>`. Then confirm `GET /health` returns 200 and that the running image is the bridge, not hello-world. This is never an azd command run outside a unit.
     2. Point the staging number's voice webhook at `POST https://<fqdn>/voice`. The real number stays on the laptop.
     3. Run step 4's full smoke test against the staging number, with **zero production exposure**. If a pass criterion fails, apply the stop rule. The real number is never cut over to a deploy that failed on staging.
     4. If the staging test passes, run step 3 (the real-number cutover, with its full voice-config record and rollback readiness).
     5. Then place **one confirmation call on the real number**, checking step 4 items 1–4, 7 and 8 on that call.
   - **Under Q-045's answer (b) (the M6 path):** step 3 (the real-number cutover) comes first, then step 4's full smoke test runs against the real number. U15b starts only when the founder chooses to cut over, after the demo. Step 2's option-(d) sub-steps are not used.
3. **Cut over the real number (T2), with the founder's go.**
   1. **Record the number's full voice configuration** (from the console, or from `GET …/IncomingPhoneNumbers/<sid>.json`): `VoiceUrl`, `VoiceMethod`, `VoiceFallbackUrl`, `VoiceFallbackMethod` and `StatusCallback`. Paste them into the PR, masking any tunnel hostname if the founder prefers.
   2. Set the voice webhook to `POST https://<fqdn>/voice` in the Twilio console. The console is simpler, and it leaves no SID in the azd environment.
   3. **Optional hardening (non-blocking):** set `VoiceFallbackUrl` to a Twilio TwiML Bin containing a short, polite apology `<Say>` plus `<Hangup>`. Twilio calls this URL if `/voice` errors or times out, so a caller hears an apology instead of Twilio's generic error message. The apology is static TwiML in the founder's Twilio account, not bridge code or agent behavior, so D-004 is untouched.
   4. **Rollback procedure (S1).** A recorded old URL is **not** a valid rollback target: a Cloudflare quick-tunnel URL changes on every restart (Q-040). To roll back:
      1. Start the laptop bridge (`python server.py`).
      2. Start the tunnel (`cloudflared tunnel --url http://localhost:8000`).
      3. Read the **current** tunnel URL from its output.
      4. Set the number's `VoiceUrl` to `<current tunnel URL>/voice` (POST), restoring the other recorded fields.
      5. Confirm with one real call.
4. **Live smoke test, with real calls from the founder's own phone.** Per HANDOFF.md §7, the founder places test calls, and the builder or Cowork stages the checks and tails the logs. Every criterion is judged from **log evidence**, not only from how the call sounded (the UT02 standard). Mask numbers as `***NNNN` in anything pasted (D-021).
   1. **Connects within 10 s**, measured from the Twilio `start` to a successful agent-mode connect (`[VoiceLive] SDK connected in …`). This is UT02 criterion 1. The 10 s allows for the 8 s `VOICE_LIVE_CONNECT_TIMEOUT_SECONDS` default, plus headroom.
   2. **Correct agent, pinned version:** `[VoiceLive] Agent mode project=hireastra agent=re-intake-pilot-agent version=24` appears in the log (UT02 criterion 2) — confirm the expected number against the Foundry portal at test time, since it has drifted before (D-045). This also proves the user-assigned identity's Foundry User grant works end to end, and that D-032's role trim didn't break agent auth.
   3. **Two-way audio:** the founder confirms it, and continuous speech-started/stopped events appear in the log (UT02 criterion 3).
   4. **Clean hang-up:** `call_ended reason=…`, then `[VoiceLive] Cleaned up`, with no force-close or timeout path (UT02 criterion 4).
   5. **Quick hang-up**, about 2 s after connecting: the session ends cleanly, with no stuck call slot. This is UT02 criterion 5, which has still not been exercised live (Q-035).
   6. **Long call of at least 6 minutes (T6):** the call is not dropped at a fixed wall-clock mark. If it is, record the time and treat it as an ingress-duration finding.
   7. **Masking check (S7, D-021):** Log Analytics queries over the test window return **nothing** for any of these:
      - the **called** Twilio number's digits;
      - the **caller's own** number (the `From` field, which is the founder's personal phone on these test calls, and the more sensitive of the two to leak), in both E.164 form (`+1XXXXXXXXXX`) and bare 10-digit form (`XXXXXXXXXX`).

      Enter the query strings only in the Log Analytics query box, never in the PR. Record just "0 rows" for each form.
   8. **Twilio side:** the Twilio console call log shows the calls as `completed`, and the Twilio Debugger shows **no** webhook errors (11200-class). A signature-URL mismatch (T5) would show up here as 403s.
   9. Watch for `sr_too_many_requests` (Q-035) and for failed-response retry markers (Q-039, D-038), and record what you see. Neither is a pass/fail criterion for M6.
5. **Outcome.**
   - If items 1–5, 7 and 8 pass on the number that carries the real cutover, the cloud path is live. The laptop path can then be retired at the founder's discretion, and **Q-040 is proposed for closure**.
   - If any criterion fails on the real number, roll back (step 3.4), record the failure, and apply the stop rule.
   - Under option (d), if the staging test fails, the real number is never cut over. Record the failure and apply the stop rule. That is a valid "attempted, failed" outcome.

   "Attempted, failed, rolled back, here's why" is an acceptable outcome for U15b to merge. "Not attempted" is not. Under option (d), a staging-only pass with the real cutover skipped is also **not** acceptable, because the real cutover is part of this unit. U15b is not started until Q-045's timing allows the real cutover, so it is never forced to merge without an attempt, and its branch never stays open across the demo.

**DoD:**
- [ ] The first check is shown. Q-045's chosen option is stated.
- [ ] The real number's full voice configuration was recorded before any change, and the rollback procedure (fresh tunnel URL) was confirmed runnable.
- [ ] The real-number cutover was performed in this unit, with the founder's go. Under option (d), the staging smoke test ran first, **in this same unit**. U15b does not merge on the staging test alone. Optional `VoiceFallbackUrl` hardening is applied or explicitly skipped.
- [ ] Every item from step 4 is recorded (pass, fail or inconclusive, with log evidence), including the caller-number masking queries. A rollback was performed if a pass criterion failed on the real number.
- [ ] No unmasked phone numbers or secrets appear in the PR body or STATUS.md.
- [ ] Opus code review and `cso`, with the same deliberate docs-only override as U15 (N6).
- [ ] STATUS.md/DECISIONS.md are updated inside this PR. Q-040 is proposed for closure if the real-number cutover passed.

### U16 — RETIRED (D-041). It filtered ACS Event Grid deliveries by an ACS number that will never exist, and Twilio delivery never passes through Event Grid.

---

## Do the ACS callback/JWT concerns still matter?

Q-044 asks, verbatim: *"whether the ACS-specific callback/JWT concerns (Q-005/Q-043, D-036) still matter if `/acs/callbacks/...` never sees production traffic"*.

**Not for this deploy.** That answer comes from reading the code and the infra, not from declaring the question moot:

1. **The routes don't exist.** `server.py` registers only the detected provider's routes (lines 113–130). With `TWILIO_AUTH_TOKEN` set and `ACS_CONNECTION_STRING` absent, the detected provider is `twilio`, so `/acs/incomingcall`, `/acs/callbacks/{key}/{sig}` and `/acs/ws/{key}/{sig}` are never registered. D-036's attack (replaying a logged callback signature) therefore has no endpoint to hit. U15 step 5 confirms this with a 404 probe rather than assuming it.
2. **Nothing ACS-shaped is deployed or wired.** Under `twilio`, Bicep deploys no ACS module, no ACS secret and no text-to-speech role (verified in `main.bicep`). `acs.postdeploy.ps1` doesn't run, so no Event Grid subscription is created. The pre-existing `hireastra-voice-pilot-acs` resource has no subscription pointing at the Container App.
3. **Is ACS used for anything else in the deploy?** Checked: no. The only ACS references left are the dormant, gated Bicep and the optional `ACS_CALLBACK_JWT_AUDIENCE` parameter. That parameter is inert because no registered route reads it.
4. **The Twilio equivalent of D-036's concern was checked too.** The Twilio path puts no secret in a URL:
   - `/voice` and `/twilio/ws` are fixed paths.
   - The WebSocket token travels in the Media Streams `start` message's `customParameters`, not in the URL, so Quart's access log never records it.
   - `/voice` logs the stream URL (`event_handler.py` 57), which contains no token.
   - Request bodies (`From`/`To`) are not access-logged.

   So no D-036-class log-reader exposure exists on the Twilio path.

**Recommendation for STATUS.md:**
- **Q-005:** close as not applicable while `TELEPHONY_PROVIDER=twilio`.
- **Q-043:** close as not applicable.
- **D-036:** stands unchanged as the rule for *any* future ACS deploy. It reactivates automatically if ACS is ever reconfigured. The T1 invariant (ACS is never configured alongside Twilio) is what keeps it inactive.
- **Q-011** (the VoIP-only ACS smoke test): superseded by U15b's live Twilio smoke test.
- **Q-013** (the Event Grid filter): moot, along with U16.

---

## New Twilio-specific deploy requirements

These were raised in the coordinator's brief. They are not a sub-item of Q-044's text.

- **A public HTTPS endpoint Twilio can reach.** Already present, and verified in the files:
  - `containerapp.bicep` 95–99 sets `external: true`, `targetPort: 8000` and `transport: 'auto'`.
  - The FQDN is output at line 271.
  - `SERVICE_API_ENDPOINTS` for Twilio is `https://<fqdn>/voice` (`main.bicep` 296).

  No change is needed. Two things stay unverified until a live call: Host-header fidelity through ingress (T5) and long-lived WebSocket duration (T6). U15b covers both.
- **Twilio auth in Key Vault:**
  - **The accelerator's own Bicep already moves `TWILIO_AUTH_TOKEN` into Key Vault** (`keyvault.bicep` 50–56 → `containerapp.bicep` 114–120/196–200), the same way it handled the ACS connection string. No new Bicep is needed.
  - The work that *is* needed:
    - least-privilege Key Vault access (U14b, T4);
    - keeping the token out of the build context (U14c, T3);
    - entering it without leaving it in shell history (U15, T9);
    - rotating it without `azd provision` (U15's rotation note, T11).
  - **`TWILIO_ACCOUNT_SID` is not needed at runtime.** Only the postdeploy hook reads it, and it is deliberately left unset (T2). It is an identifier, not a secret, so it would not need Key Vault in any case.
  - **`MEDIA_WS_TOKEN` is not needed.** It is ACS-only.

---

## Definition of done and verification coverage — summary table

| Unit | Status | What's verified | Method |
| --- | --- | --- | --- |
| U14a | CLOSED (#33) | Existing AI resource referenced cross-subscription; Foundry User on the user-assigned identity; scale 1/1. ACS parts dormant under `twilio`. | (done) `az bicep build`, `azd provision --preview`, `what-if` |
| U14b | Re-scoped | T12 cleanup of the stale ACS environments. Key Vault role narrowed to Secrets User, with the GUIDs confirmed live and the assignment-name seed handled. D-031 shape and T1 negatives confirmed in a Twilio-mode preview (dummy token, throwaway environment). First-deploy proof deferred to U15. | `azd env get-value`, `az role definition list`, `az bicep build`, `azd provision --preview -e` |
| U14c | Re-scoped | `.env` excluded from the build context. Sizing and ACR tier set per the founder. `MEDIA_WS_TOKEN` and Foundry GUID items dropped, with reasons. | `.dockerignore` check, `azd provision --preview -e` |
| U14d | RETIRED | — | — |
| U-CFG | RETIRED | — | — |
| U15 | Redesigned (split) | Fresh-environment first deploy: the C1 Key Vault half at provision, the ACR half at deploy. The provision → deploy pairing (T11). Pre-cutover config and route probes (403/405/404/200). The real number is **not** cut over. | `azd … -e`, `az containerapp show`, role-assignment lists, `curl`, log queries |
| U15b | New | Founder-gated real-number cutover after the demo (Q-045 answered (b); staging option (d) deferred), with a full voice-config record and fresh-tunnel rollback. Live Twilio calls against UT02's five criteria, plus a long call, called- and caller-number masking checks, and Twilio Debugger checks. | Twilio console, log queries, `curl` |
| U16 | RETIRED | — | — |

**M6 definition of done (rev 5.1; supersedes rev 4's list).** M6 closes only when **both U15 and U15b** have merged. U15 can merge on its own, whatever the answer to Q-045. U15b's start is timed by Q-045's answer (b): after Friday's demo, on the founder's timeline. There is no escape hatch that lets M6 close with the cutover deferred indefinitely.
- [ ] **Deployment shape.** The bridge is deployed to a Container App in `rg-hireastra-voice-pilot` with 1 replica.
  - It points at the existing `hireastra-resource` (no second AI account), across the Voice Pilot/credits subscription split.
  - It runs on D-031's identity shape: one user-assigned identity for the registry pull, Key Vault, and Voice Live/Foundry User, and no system-assigned identity on the Container App.
- [ ] Foundry User is granted to that identity on `hireastra-resource`. Narrowing its scope is tracked separately (Q-041) and is not required for M6.
- [ ] Key Vault holds `TWILIO-AUTH-TOKEN`, and the identity holds Key Vault Secrets **User** (not Officer).
- [ ] `TELEPHONY_PROVIDER=twilio`. No ACS resource, secret, role or env var is configured by this deploy, and `/acs/*` returns 404 (T1; spec §7's "no second telephony provider"). No stale ACS azd environment is the default (T12).
- [ ] The standing operational rules (T11/T12) are recorded in DECISIONS.md: always target the environment with `-e`, provision always paired with deploy, never `azd down`, and token rotation done without provision.
- [ ] **Cutover (U15b).** The real Twilio number's voice webhook points at `POST https://<fqdn>/voice`. Its prior full voice configuration was recorded, and the fresh-tunnel rollback procedure was confirmed.
- [ ] **Live call (U15b).** A live Twilio call against the deployed Container App meets UT02's five criteria, judged from log evidence, plus the long-call check and the called- and caller-number masking checks. Failures are recorded, not waived.
- [ ] Monitoring per D-033: Log Analytics container logs are flowing, and the Foundry-side tracing state is confirmed.
- [ ] `ENABLE_WEB_CLIENT` and `AZURE_VOICE_LIVE_API_KEY` are both absent in the deployed environment (D-007/D-028, D-004).
- [ ] `config_validator.py` passes unedited (D-016, D-031).
- [ ] The $50/month budget alert is set by the founder (spec §6). The C11/U14c cost re-estimate is shared with the founder first.
- [ ] No unit touched TELEPHONY_BRIDGE_SPEC.md §7's "Do not build" list: no autoscaling beyond one replica, no multi-region, no second active provider, no outbound calling. A staging Twilio number (Q-045 (d), deferred), if ever adopted, would be a second *number* on the same provider, not a second provider.

*Removed from rev 4's DoD, with reasons:*
- The Event Grid subscription bullet: there is no Event Grid on the Twilio path.
- "ACS linked to the AI resource for text-to-speech": ACS doesn't speak on calls (C3 not applicable).
- The callback JWT (Q-005) bullet: there are no ACS routes (see "Do the ACS callback/JWT concerns still matter?" above).
- The VoIP-only smoke test bullet: superseded by U15b's live Twilio smoke test.

---

## Consistency with DECISIONS.md — explicit checks

- **D-041 (Twilio is the permanent provider):** this revision implements it. One precision point about D-041's own wording: it says the call reaches the bridge via "Twilio's HTTPS webhook to `/twilio/ws`". In fact the HTTPS webhook is `POST /voice`, and `/twilio/ws` is the Media Streams WebSocket that Twilio opens *after* `/voice` returns TwiML (`providers/twilio/__init__.py` 48, 78). This has no design consequence; it is flagged for accuracy only.
- **D-001 (superseded by D-041 for the number provider):** the *subscription topology* D-001 created stays as built:
  - the Container App in the Voice Pilot pay-as-you-go subscription;
  - the agent in the credits subscription.

  U14a's Bicep already adopts this topology, and it was verified with what-if. Consolidating into one subscription is now *possible*, since the separate subscription existed for buying an ACS number, but it isn't recommended. It would redo verified U14a work to remove a complexity that already works. Which subscription pays is also a spending question for the founder, not this plan. This is recorded only so it isn't mistaken for a requirement.
- **D-029:** its instruction that "the Event Grid subscription for `Microsoft.Communication.IncomingCall` is created now" is **superseded** (there is no Event Grid on the Twilio path). The rest of D-029 (M6 is unblocked ahead of a phone number) still holds.
- **D-002 / CLAUDE.md §10, and D-032's scoped exception:** the footprint shrinks. Upstream-origin files expected to be touched by the rest of M6:
  - `infra/modules/roleassignments.bicep` (U14b; already in D-032's list).
  - `infra/modules/containerapp.bicep` and `infra/modules/containerregistry.bicep` (U14c sizing/ACR; already in D-032's list).
  - `server/.dockerignore` (U14c). This is a one-line small hook, within §10's normal rule, so it needs no exception.

  `infra/modules/keyvault.bicep` and `hooks/preprovision.ps1` drop off the expected list, because `MEDIA_WS_TOKEN` is dropped.
- **D-004:** agent mode uses a pinned digit version in `AGENT_ROUTING_JSON`. There is no `AZURE_VOICE_LIVE_API_KEY` in the deployed environment (checked in U15). The UT02 local-testing key exception (UT02 spec) does not extend to the Container App. The optional `VoiceFallbackUrl` TwiML Bin (U15b) is static Twilio-account content, not bridge-authored agent behavior.
- **D-006 / D-021 / D-036:** no number or secret appears in any Twilio URL (see the ACS callback/JWT section above). Masking is verified in U15b step 4.7, for both the called and the caller number. The routing JSON contains the real number and is a plain Container App env var, so anyone with read access to the Container App can see it. That is acceptable, because it is config rather than a URL or a log line, but it is **redacted from every pasted verification output**.
- **D-007 / D-028:** `ENABLE_WEB_CLIENT` is absent from Bicep, and U15 checks that it is absent from the deployed environment.
- **D-016:** no upstream Python is edited. `config_validator.py` stays untouched.
- **D-031:** reconfirmed, not reopened. U14b narrows the Key Vault role *grant* (Officer → User), not the identity *choice*.
- **D-032 / D-039:** the trim to Foundry User stands. Q-041 (the breadth of its account scope) stays open, independent of the provider.
- **D-033:** unchanged. The App Insights component upstream `monitor.bicep` creates is not a violation (C8).
- **D-034:** every implementation unit runs on Opus (rev 4's Sonnet lines are corrected).
- **D-036:** stands as the ACS rule. It does not apply to this deploy, because no ACS route is registered (U15 checks this).
- **D-040:** Q-041 and Q-042 remain open. Q-042 now **gates U15's first real provision**, as D-040 already implied. Q-043 is recommended for closure (see above).

---

## Open questions — proposed updates for STATUS.md §3

*(Numbers for new questions are proposals. The partner assigns them in the status commit.)*

- **Q-044:** ANSWERED by this revision (rev 5.1).
- **Q-005:** recommend **CLOSED, not applicable** while `TELEPHONY_PROVIDER=twilio`: no ACS routes are registered, which U15's 404 probe verifies. It reopens only if ACS is reconfigured, in which case D-036 applies in full.
- **Q-011:** **superseded** by U15b's live Twilio smoke test. Rev 4's VoIP-only procedure is preserved in git (`0608658`).
- **Q-013:** **moot** (U16 retired).
- **Q-043:** recommend **CLOSED, not applicable**, for the same reason as Q-005.
- **Q-040:** remains OPEN until U15b's real-number cutover passes cleanly. It is then **proposed for closure**, because the laptop/tunnel single point of failure is removed from the call path.
- **Q-041:** unchanged, OPEN (the breadth of Foundry User's account scope). Not an M6 gate.
- **Q-042:** unchanged in substance, OPEN. **It now explicitly gates U15's first real provision.**
- **Q-034:** unchanged, OPEN. Append T8's note: a stable public FQDN slightly raises the exposure. Not an M6 gate.
- **Q-045 — ANSWERED by the founder (2026-09-28): option (b).**
  - **Friday's demo (2026-10-02) runs on the laptop path, however U15's cloud checks turn out.**
  - The real-number cutover (U15b) is **deferred past the demo**, on its own timeline, not gated by Friday or any fixed deadline.
  - **The staging-number option (d) is deferred to a later stage**: not decided now, not rejected.

  **What this settles for U15/U15b:**
  - U15 (deploy and pre-cutover verification; never touches the real number) runs whenever it is ready.
  - U15b (the real-number cutover, with the full voice-config record, rollback readiness, the full smoke test and a confirmation call) runs afterwards, whenever the founder chooses to cut over.
  - No staging number is bought or configured in M6.

  **The options that were offered, for the record:**
  - (a) Cut over as soon as U15 merged, and rehearse the demo on the cloud path.
  - **(b) Keep the demo on the laptop path, and run U15b after Friday — chosen.**
  - (c) Cut over only if the live calls passed by a pre-Friday deadline.
  - (d) A second, staging Twilio number, about $1–2/month (a founder spending call), for a zero-exposure smoke test before the real cutover. Deferred to a later stage. If it is adopted later, U15b's option-(d) sub-steps (step 2) describe how it would run: staging test, then the real cutover, in the same unit.

  The partner had recommended (d), otherwise (c). The founder's choice of (b) removes the pre-demo risk entirely, by keeping the known-working path for Friday.
- **Q-046 (founder; housekeeping, low priority, blocks nothing):** close Azure support ticket 2609250040002301, and decide what to do with the idle `hireastra-voice-pilot-acs` resource (keep it idle until production hardening, or delete it now). **Recommendation:** close the ticket now; keep the resource idle and revisit at hardening; never delete it via `azd down` (Q-042).
- **Q-047 (partner, for production hardening; not an M6 gate):** fix T11's root cause. `main.bicep` hardcodes the hello-world `imageName`, which makes `fetchLatestImage` dead code and makes every `azd provision` reset the app. The fix would be an upstream-Bicep change. Until it lands, the rule that provision is always paired with deploy is the mitigation.

## Decision entries (recorded as D-042 and D-043 in the status-only PR carrying this plan)

- **D-042 (recorded; partner, under D-030): M6 plan rev 5/5.1, the Twilio-first re-scope, plus standing deploy rules.** Record:
  - U14b's first-deploy proof moves into U15: the Key Vault half at provision, the ACR half at deploy.
  - Key Vault Secrets Officer is narrowed to Secrets User (T4), with the assignment-name seed caveat.
  - `MEDIA_WS_TOKEN` is dropped from U14c, because nothing on the Twilio path reads it.
  - The `.dockerignore` fix is added (T3).
  - **U15 is split into U15 (deploy and pre-cutover verification) and U15b (live smoke test and cutover).** M6 closes on both.
  - Q-005/Q-043 are not applicable while `TELEPHONY_PROVIDER=twilio`. The rule that ACS is never configured alongside Twilio (T1) is adopted as a standing deploy rule.
  - **The standing operational rules:**
    1. Every azd command targets `-e <env>`, and the first check is that `TELEPHONY_PROVIDER` prints `twilio` (T12).
    2. `azd provision` is always immediately followed by `azd deploy` in the same maintenance window, never during live-call hours, and never left mid-sequence (T11).
    3. Never run `azd down` on an environment that adopts existing resource groups (Q-042).
    4. Never deploy during a live call (T7).
    5. Rotate the Twilio token via `az keyvault secret set` plus a revision restart, never via provision.
  - D-041 supersedes TELEPHONY_BRIDGE_SPEC.md §6's ACS-specific rows for the call path (the Event Grid row, the Key Vault contents, and the "system-assigned identity" line, which D-031 had already superseded), §8's "test number" wording, and **D-029's "create the Event Grid subscription now" instruction**.
  - U14a's ACS Bicep is kept dormant, not reverted.
- **Founder-only, not decided here:**
  - the U14c sizing and ACR tier;
  - Q-046 (the ACS resource and support ticket);
  - Q-042 (resource locks);
  - the budget alert.

## Required follow-up files (for the partner's status commit alongside this plan; per D-015 the milestone doc must track the plan)

- **`docs/superpowers/plans/2026-09-25-telephony-bridge-milestones.md`:**
  - The unit→branch table (lines ~33–39) still describes the old ACS scope for U14c ("Key Vault media token…"), U15 ("VoIP-only smoke test… requires U10–U13") and U16 (listed as BLOCKED rather than retired).
  - The M6 section (lines ~155–173) still points at rev 4, and lists U15 and U16 in their ACS form.
  - Update both to rev 5.1: U14b and U14c re-scoped, U15 split into U15 and U15b, U16 retired, and the Twilio-based M6 DoD.
- **STATUS.md §1:**
  - U15's prerequisite changes from "U10–U13" to **UT01a/UT01b**.
  - U14c's description becomes "secrets hygiene (`.dockerignore`) + container/ACR sizing".
  - U14b becomes un-paused with its new scope, and its branch is renamed.
  - **Add a U15b row** (`feat/tb-m6-u15b-cutover`).
- **STATUS.md's M7–M8 roll-up line:** it still says the work "needs… the ACS number". Re-point it to the Twilio number. Also note T10: spec §8 tests 7 and 8 assume spoken fallback/goodbye messages, which the Twilio path doesn't produce on connect failure or when the call cap is hit.
- **STATUS.md §3:** apply the question updates above.

---

## What changed in rev 5.1 (against rev 5, after its Opus design review: 2 blocking, 8 should-fix, 9 nits)

**Blocking:**
- **B1:** added T11 and the evidence row. `azd provision` resets the app to the upstream hello-world image, because `main.bicep` 278 hardcodes it and `containerapp.bicep` 168 always uses it, leaving `fetchLatestImage` as dead code. In response:
  - a standing rule: provision is always immediately followed by deploy, in the same maintenance window, never during live-call hours, and never left mid-sequence;
  - the token-rotation procedure rewritten to use `az keyvault secret set` plus a revision restart, never provision;
  - U15 step 3 now warns that the hello-world revision after provisioning looks unhealthy against `targetPort: 8000` whatever the state of RBAC, and must not be misdiagnosed as RBAC propagation lag;
  - proposed Q-047 for the root cause.
- **B2:** added T12 and the evidence row. There are **two** stale ACS environments, `u14a-preview` and `u14b-preview`, and `u14b-preview` is `defaultEnvironment`. In response:
  - deleting both is now U14b's first precondition (re-pointing the default was dropped in the re-review, because it can't meet U14b's own evidence requirement);
  - every azd command is written with `-e <env>`;
  - "`azd env get-value TELEPHONY_PROVIDER -e <env>` prints `twilio`" is the first check in U14b, U14c, U15 and U15b.

**Should-fix:**
- **S1:** the rollback no longer relies on a recorded tunnel URL, which Cloudflare quick tunnels change on every restart. It now starts the laptop bridge and tunnel, reads the current URL, sets `VoiceUrl` to `<current>/voice`, and confirms with a call. The number's full voice config (`VoiceUrl`, `VoiceMethod`, `VoiceFallbackUrl`, `VoiceFallbackMethod`, `StatusCallback`) is recorded first. Optional `VoiceFallbackUrl` hardening with a TwiML Bin was added.
- **S2:** the contradiction between U15's DoD and M6's DoD is resolved by **splitting U15**. U15 now covers deploy and pre-cutover checks, and merges on those alone. The new **U15b** covers the cutover and live smoke test, timed by Q-045. M6 closes on both, with no indefinite-deferral escape hatch, and U15b is listed for STATUS §1.
- **S3:** Q-045 gains option (d), a staging Twilio number (the founder's spending call). It is the partner's recommendation, with (c) as the fallback.
- **S4:** U14b now states correctly that the role-assignment name is seeded with a literal string. Changing only the role ID keeps the same name, which fails with `RoleAssignmentUpdateNotPermitted` where the assignment already exists. The builder picks between changing the seed and deleting the old assignment first, and says which.
- **S5:** U14b's preview uses a dummy token in a throwaway environment that is deleted after the PR. U15 creates its own separate, real environment.
- **S6:** added a stop rule. If a failure needs a Bicep or code fix, the builder stops, ensures the real number is on the laptop path, hands back to the partner, and the fix becomes a new unit. U15 and U15b never turn into Bicep-fixing units.
- **S7:** the masking check now also covers the **caller's** own number, in both E.164 and bare 10-digit forms.
- **S8:** added "Required follow-up files" (the milestone doc, STATUS §1's U15/U14c/U15b rows, the M7–M8 roll-up line), and proposed closing Q-040 after the U15b cutover passes.

**Nits:**
- **N1:** a request with an empty `TWILIO_AUTH_TOKEN` gets a **404** (the routes never register), not a 503.
- **N2:** T1's failure mode was restated. The silent, lasting damage is at the infra level (a full-PUT of the existing ACS resource, or a second ACS resource). At the app level the container crash-loops loudly, because `MEDIA_WS_TOKEN` is unset.
- **N3:** removed the invented "Q-044 item N" numbering. Q-044's text is quoted where it applies, and items that came from the coordinator's brief are labeled as such.
- **N4:** C8 now says upstream `monitor.bicep` still creates an App Insights component and dashboard. D-033 defers instrumentation only, so seeing them in a preview is expected.
- **N5:** U15 step 3 proves only C1's Key Vault half. The ACR half is proven at step 4 (deploy).
- **N6:** running `cso` on U15/U15b's docs-only PRs is stated as a deliberate override of CLAUDE.md §5 step 6's docs-only skip.
- **N7:** T9 notes the brief exposure of the token on `azd env set`'s command line (argv), and asks the builder to prefer a stdin form if azd has one.
- **N8:** D-029's "create the Event Grid subscription now" instruction is added to the list of superseded items.
- **N9:** the U14a "never applied" headline is hedged to match its evidence (PR #33's stated commands, not a live Azure query).

**Focused re-review of rev 5.1 (B1/B2 confirmed fixed against the real `.azure/` state on disk). Four text fixes applied:**
1. **U15b under option (d):** U15b now covers both the staging smoke test **and** the real-number cutover in one unit. It starts only when the founder's timing allows the real cutover, and it never merges on the staging test alone. Adding the staging number to routing is an explicit U15b step: the provision+deploy pair followed by a `/health` check. Q-045 (d)'s text is corrected to match, and it now states the timing tradeoff this creates.
2. **T12:** deleting both stale environments is the required path. The "re-point instead" alternative is dropped, because it couldn't produce U14b's own DoD evidence, and its target environment wouldn't survive the unit. The DoD evidence is tightened to match.
3. **U14c** is added to the standing-rule scope (`-e` on every command, and the `TELEPHONY_PROVIDER` first check). U14c gets its own first check and a DoD item.
4. **C7** now says U10–U13 are irrelevant to U15's deploy and U15b's smoke test. The S1 summary now lists all five voice-config fields, including `VoiceFallbackMethod`.

## What changed in rev 5 (against rev 4)

**Re-targeted:** M6 now deploys the merged Twilio call path (`/voice` → `/twilio/ws` → Voice Live agent mode) instead of the ACS Event Grid/Call Automation path, per D-041. No new call-handling logic is designed. The Twilio pilot plan's UT01a/UT01b code is what gets deployed.

**Verified facts that changed the plan:**
- The accelerator's Bicep already handles Twilio end to end (Key Vault secret, `secretRef` and endpoint output), and every ACS piece of U14a is gated off under `twilio`.
- U14a was never provisioned live, so there is nothing to decommission in Azure. *(Hedged in rev 5.1: this rests on PR #33's stated verification commands, not a live Azure query.)*
- The provider choice drives Bicep, the Docker build arg and runtime detection separately, and ACS wins a tie (T1; rev 5.1 corrects the app-level failure to a loud crash-loop — the silent damage is at the infra level).
- The Twilio postdeploy hook would silently repoint the production number (T2).
- `.dockerignore` doesn't exclude `.env`, and a local `.env` holds live credentials (T3).
- The Key Vault role GUID is Secrets Officer (T4).
- The Twilio WebSocket token is keyed on `TWILIO_AUTH_TOKEN`, not `MEDIA_WS_TOKEN`.
- `preprovision.ps1` shows no provider prompt when `TWILIO_AUTH_TOKEN` is set. Rev 4's "press Enter at the prompt" instruction would now select ACS and is withdrawn.

**Units:**
- U14b is re-scoped: its requirement is reconfirmed, its build scope was already met on `main`, it gains the Key Vault least-privilege fix, and its first-deploy proof moves to U15.
- U14c is re-scoped: `MEDIA_WS_TOKEN` is dropped, the Foundry GUID item is already done per D-039, the `.dockerignore` fix is added, and the sizing recommendation is revised to 1.0 vCPU/2 GiB for the Twilio resampling path, still the founder's call.
- U15 is redesigned: a fresh env, a preview hard gate, HTTP route probes, a founder-gated reversible cutover, and live Twilio calls against UT02's criteria plus long-call, masking and Twilio Debugger checks. Its prerequisites change from U10–U13 to UT01a/UT01b.
- U16 is retired. All implementation units are on Opus (D-034).

**Findings:** C3 not applicable; C4 and C7 superseded; C5 half moot; C10 revised (no `communication` extension; `pwsh` missing); C1, C2, C6 (resource group), C8, C9 and C11 unchanged. New T1–T10 as above, with T10 flagged for M7 rather than M6.

**Questions:** Q-044 answered. Q-005/Q-043 recommended closed as not applicable, with the reason verified. Q-011 superseded. Q-013 moot. Q-042 now gates U15. Proposed Q-045 (cutover timing, founder) and Q-046 (ACS resource and ticket, founder).

**Not independently verified in this planning session (flagged, not asserted):** live Azure state (whether U14a was ever applied; Cowork can confirm), azd's remote-build `.dockerignore` handling (T3), Host-header fidelity through Container Apps ingress (T5), Container Apps WebSocket duration limits (T6), and the Key Vault Secrets User GUID (U14b confirms it live).

## What changed in rev 4 (against rev 3)

Resolved Q-014 (App Insights deferred, D-033), Q-015 (the Container App keeps the user-assigned identity for everything, including Voice Live/Foundry User, D-031, which retired U-CFG) and Q-017 (the scoped upstream-divergence exception and the role trim to Foundry User, D-032). U14a and U14b were unblocked. U14d was later folded into U14a once Cowork's inventory (Q-016) confirmed that the resource group and ACS resource already existed.

## What changed in rev 2 (against rev 1, for the record)

**Corrected factual claims:**
- `preprovision.ps1`'s interactive prompt (was backwards).
- `postdeploy.ps1`'s lack of prompts (was backwards).
- The `grep -ril eventgrid infra/` result.
- The Event Grid advanced-filter capability (real, just unused).
- The `called_number_from_event()` code quote.
- `GeneratePassword`'s availability under `pwsh`.
- The `az deployment sub what-if` command against `main.parameters.json`.

**Added:**
- The circular-dependency identity problem (C1) and U14b.
- The upstream `config_validator.py` crash-loop risk and U-CFG.
- The ACS text-to-speech wiring gap (C3).
- The `AGENT_ROUTING_JSON` placeholder decision (C4).
- The cross-subscription topology (C5) and the inventory step (C6, Q-016).
- U15's corrected prerequisites.
- The Key Vault RBAC-only verification method (C9).
- Tooling prerequisites (C10) and a cost flag (C11).

**Restructured:**
- Split U14 into U14a/b/c/d plus U-CFG.
- Restored the M6 DoD's text-to-speech bullet.
- Tightened the Q-011 procedure.
- Rewrote Q-013/Q-014 and added Q-015–Q-017.

## What changed in rev 3 (against rev 2, after the second Opus review round)

**Fixed:**
- C3's text-to-speech role now targets the ACS resource's own system-assigned identity, not the Container App's.
- C1's Key Vault half: secret references are resolved at revision creation, the same as the registry pull, so rev 2's "system-assigned for Key Vault" option didn't work.
- `containerregistry.bicep` is named as the `AcrPull` location.
- Purge-protection teardown is explained.

**Smaller fixes:**
- U-CFG's prerequisite and its sentinel-leak constraint.
- Q-017's full file list.
- The explicit `az account set` step.
- Connection-string handling in the Q-011 procedure.
- "Who runs the test" aligned with HANDOFF.md §7.
- Idempotent `MEDIA_WS_TOKEN` generation.
- An explicit statement that M6 can close without U16.

A third, focused Opus round confirmed the C3/C1 substance and applied text-only fixes. The full rev 1–4 text is in git history.
