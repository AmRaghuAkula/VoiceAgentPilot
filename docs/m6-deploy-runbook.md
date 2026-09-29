# M6 deploy runbook (Twilio path)

Operational notes recorded by U15 (first real deploy, 2026-09-29). The authoritative procedure is the M6 plan
([`2026-09-27-m6-deploy-plan.md`](superpowers/plans/2026-09-27-m6-deploy-plan.md), U15/U15b and the standing rules).
This file adds only the exact commands the plan leaves implicit. Never paste secret values, the routing JSON or
phone numbers into this file.

## Deployed environment

This repository is public. The environment's resource names carry an azd-generated resource token, written below as
`<suffix>`, and the Container App's DNS label is not recorded. Read the real names with
`az resource list -g rg-hireastra-voice-pilot --subscription 13b3dbed-e03a-4d01-8b88-ac5c80fc749e -o table`, and the
endpoint with `azd env get-value SERVICE_API_ENDPOINTS -e vp-twilio-prod`.

| Item | Value |
| --- | --- |
| azd environment | `vp-twilio-prod` (every command carries `-e vp-twilio-prod`) |
| Subscription | Voice Pilot, `13b3dbed-e03a-4d01-8b88-ac5c80fc749e` |
| Resource group | `rg-hireastra-voice-pilot` (adopted, `CanNotDelete` lock, never `azd down`) |
| Resources created by U15 | Container App `ca-vp-twilio-prod-<suffix>`, environment `cae-vp-twilio-prod-<suffix>`, ACR `cr<suffix>`, Key Vault `kv-vp-twilio-prod-<suffix>`, identity `vp-twilio-prod-<suffix>-id`, Log Analytics `log-vp-twilio-prod-<suffix>`, App Insights `insights-vp-twilio-prod-<suffix>` + dashboard |

## ARM validate gate (run before `azd provision --preview`)

`azd provision --preview` and `what-if` do not run ARM's sequencer checks (Q-058). `az deployment sub validate`
does. azd does not expose the resolved parameters, so build a parameters file from the azd environment:

- resolve every `${KEY}` / `${KEY=default}` placeholder in `infra/main.parameters.json` with
  `azd env get-value KEY -e <env>`. When the key is unset, use the `=default` value (for example
  `ACS_DATA_LOCATION` → `United States`, `TELEPHONY_PROVIDER` → `acs`), and use an empty string only when the
  placeholder has no default;
- take parameter names from `main.parameters.json`, not from the env keys: they differ (for example `appExists` comes
  from `SERVICE_APP_RESOURCE_EXISTS`, `modelName` from `AZURE_VOICE_LIVE_MODEL`);
- emit `appExists` and `debugMode` as JSON booleans, not strings;
- use a **dummy** non-empty `twilioAuthToken` (the template only tests it for non-empty; a 32-character value matches
  the real token's shape), so the real token is never written to disk;
- write the file to a scratch directory, run the command, and delete the file in a `finally` block.

```powershell
az deployment sub validate --subscription 13b3dbed-e03a-4d01-8b88-ac5c80fc749e --location eastus2 `
  --name "validate-<timestamp>" --template-file infra/main.bicep --parameters "@<scratch>/params.json" `
  --query "{state:properties.provisioningState, error:error}"
```

Pass criterion: `state` is `Succeeded` and `error` is null. The same parameters file works for
`az deployment sub what-if --no-pretty-print --result-format FullResourcePayloads`, which shows identities, role
assignments and secrets that the azd preview summary omits.

**Reading what-if's Key Vault secrets.** Every conditional secret in `keyvault.bicep` (including
`ACS-CONNECTION-STRING` and `TWILIO-AUTH-TOKEN`) is listed under `potentialChanges`, because its condition depends on
a `@secure()` module parameter what-if cannot evaluate. what-if therefore cannot show either secret definitively.
Under `twilio`, `main.bicep` passes `acsConnectionString: ''`, so the ACS secret is gated off. Confirm both after
provisioning with the control-plane listing below.

## Post-deploy verification commands (read-only, print no secret values)

```powershell
$sub = '13b3dbed-e03a-4d01-8b88-ac5c80fc749e'; $rg = 'rg-hireastra-voice-pilot'; $sfx = '<suffix>'
# Key Vault secret names, via ARM (no data-plane role needed; values are never returned)
az rest --method get --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.KeyVault/vaults/kv-vp-twilio-prod-$sfx/secrets?api-version=2023-07-01" --query "value[].name" -o tsv
# Role assignments of the Container App identity, in both subscriptions
$p = az identity show -g $rg -n "vp-twilio-prod-$sfx-id" --subscription $sub --query principalId -o tsv
foreach ($s in $sub, '82632bb8-e34b-41be-a7c7-a134f16c1c9c') { az role assignment list --assignee $p --all --subscription $s --query "[].{role:roleDefinitionName,scope:scope}" -o tsv }
# Revisions: the bridge revision must be Healthy with 100% traffic; a hello-world revision after provision is expected (B1)
az containerapp revision list -g $rg -n "ca-vp-twilio-prod-$sfx" --subscription $sub --all -o table
# Startup log markers
az containerapp logs show -g $rg -n "ca-vp-twilio-prod-$sfx" --subscription $sub --type console --tail 300 --format text
```

HTTP probes (`<fqdn>` from `SERVICE_API_ENDPOINTS`): `GET /health` → 200, unsigned `POST /voice` → 403,
`GET /voice` → 405, `POST /acs/incomingcall` → 404.

**Log Analytics without the CLI extension.** `az monitor log-analytics query` tries to install a preview extension
interactively and fails in a non-interactive shell. Query the REST API instead:
`az rest --method post --url "https://api.loganalytics.io/v1/workspaces/<customerId>/query" --resource "https://api.loganalytics.io" --body "@<file with {\"query\": ...}>"`.
