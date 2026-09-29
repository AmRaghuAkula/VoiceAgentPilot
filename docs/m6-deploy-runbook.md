# M6 deploy runbook (Twilio path)

Operational notes recorded by U15 (first real deploy, 2026-09-29). The authoritative procedure is the M6 plan
([`2026-09-27-m6-deploy-plan.md`](superpowers/plans/2026-09-27-m6-deploy-plan.md), U15/U15b and the standing rules).
This file adds only the exact commands the plan leaves implicit. Never paste secret values, the routing JSON or
phone numbers into this file.

## Deployed environment

| Item | Value |
| --- | --- |
| azd environment | `vp-twilio-prod` (every command carries `-e vp-twilio-prod`) |
| Subscription | Voice Pilot, `13b3dbed-e03a-4d01-8b88-ac5c80fc749e` |
| Resource group | `rg-hireastra-voice-pilot` (adopted, `CanNotDelete` lock, never `azd down`) |
| Resources created by U15 | Container App `ca-vp-twilio-prod-q3s4v`, environment `cae-vp-twilio-prod-q3s4v`, ACR `crq3s4v`, Key Vault `kv-vp-twilio-prod-q3s4v`, identity `vp-twilio-prod-q3s4v-id`, Log Analytics `log-vp-twilio-prod-q3s4v`, App Insights `insights-vp-twilio-prod-q3s4v` + dashboard |
| Public endpoint | Not recorded here (public repo). Read it with `azd env get-value SERVICE_API_ENDPOINTS -e vp-twilio-prod` |

## ARM validate gate (run before `azd provision --preview`)

`azd provision --preview` and `what-if` do not run ARM's sequencer checks (Q-058). `az deployment sub validate`
does. azd does not expose the resolved parameters, so build a parameters file from the azd environment:

- map each `infra/main.parameters.json` entry to its `azd env get-value <KEY> -e <env>` value (empty string when
  unset; `appExists`/`debugMode` as JSON booleans);
- use a **dummy** 32-character `twilioAuthToken` (the template only tests it for non-empty), so the real token is
  never written to disk;
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
`ACS-CONNECTION-STRING`) is listed under `potentialChanges`, because its condition depends on a `@secure()` module
parameter what-if cannot evaluate. That is not a real create: under `twilio`, `main.bicep` passes
`acsConnectionString: ''`. Confirm after provisioning with the control-plane listing below.

## Post-deploy verification commands (read-only, print no secret values)

```powershell
$sub = '13b3dbed-e03a-4d01-8b88-ac5c80fc749e'; $rg = 'rg-hireastra-voice-pilot'
# Key Vault secret names, via ARM (no data-plane role needed; values are never returned)
az rest --method get --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.KeyVault/vaults/kv-vp-twilio-prod-q3s4v/secrets?api-version=2023-07-01" --query "value[].name" -o tsv
# Role assignments of the Container App identity, in both subscriptions
$p = az identity show -g $rg -n vp-twilio-prod-q3s4v-id --subscription $sub --query principalId -o tsv
foreach ($s in $sub, '82632bb8-e34b-41be-a7c7-a134f16c1c9c') { az role assignment list --assignee $p --all --subscription $s --query "[].{role:roleDefinitionName,scope:scope}" -o tsv }
# Revisions: the bridge revision must be Healthy with 100% traffic; a hello-world revision after provision is expected (B1)
az containerapp revision list -g $rg -n ca-vp-twilio-prod-q3s4v --subscription $sub --all -o table
# Startup log markers
az containerapp logs show -g $rg -n ca-vp-twilio-prod-q3s4v --subscription $sub --type console --tail 300 --format text
```

HTTP probes (`<fqdn>` from `SERVICE_API_ENDPOINTS`): `GET /health` → 200, unsigned `POST /voice` → 403,
`GET /voice` → 405, `POST /acs/incomingcall` → 404.

**Log Analytics without the CLI extension.** `az monitor log-analytics query` tries to install a preview extension
interactively and fails in a non-interactive shell. Query the REST API instead:
`az rest --method post --url "https://api.loganalytics.io/v1/workspaces/<customerId>/query" --resource "https://api.loganalytics.io" --body "@<file with {\"query\": ...}>"`.
