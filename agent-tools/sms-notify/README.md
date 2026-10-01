# sms-notify

A minimal HTTP tool that lets a Foundry agent send **one** agent-written text message to a fixed follow-up contact near the end of a call. Design: [spec](../../docs/superpowers/specs/2026-09-30-sms-notify-design.md) (rev 2.1). Plan: [plan](../../docs/superpowers/plans/2026-09-30-sms-notify-plan.md). Decisions: D-062, D-063.

The service is generic. It authors no content: it normalizes the text, checks it against a positive template (spec K12, D-064), applies limits, and forwards it through Twilio Programmable SMS. The text may hold only `Label: value` lines, with labels from `SMS_ALLOWED_LABELS` and values from a small character allowlist, so no link, web address, email address, handle or IP address can be sent. The message format and its labels live only in the agent's Foundry instructions and the azd environment, never here.

## Layout

| Path | What |
| --- | --- |
| `sms_notify/core/` | Framework-free core: `messages.py` (K11 normalization, the K12 template validator, segments), `limits.py` (dedupe with outcome, cooldown, hourly slots), `service.py` (`notify()`), `config.py`, `deadline.py`, `phone.py`, `errors.py` |
| `sms_notify/ports.py` | `StateStore`, `SecretSource`, `Clock`, and the `Notifier` shape (plan P1) |
| `sms_notify/adapters/` | `twilio_sms.py` (`TwilioSmsNotifier`), `blob_state.py`, `keyvault_secrets.py`, `azure_token.py` (all raw `httpx`) |
| `sms_notify/http/` | `auth.py` (Entra JWT in code) and `dispatcher.py` (always-200 envelope) |
| `function_app.py` | One anonymous Azure Functions route that adapts to the dispatcher |
| `openapi/sms-notify.json` | The tool's OpenAPI document (placeholder server host) |
| `infra/`, `azure.yaml` | Its own azd project and resource group |
| `tests/` | Offline tests, guards G1 to G3, and `genericity_denylist.txt` (the only file exempt from G2) |

Self-contained: it never imports from `server/` or `agent-tools/calendar/`, and neither imports from it (G1).

## Tests

```bash
cd agent-tools/sms-notify
python -m uv sync --group dev
python -m uv run pytest -q
```

Everything runs offline. The opt-in marker `-m live_twilio` (one real send) is excluded by default and is run only in USMS02.

## Configuration

App settings come from the azd environment (`infra/main.parameters.json`). No values are in the repo. Examples are fictional.

| Setting | Example (fictional) | Required | Notes |
| --- | --- | --- | --- |
| `SMS_FROM_NUMBER` | `+1XXX5550100` | yes | Sender number in E.164. The real one exists only in the azd env |
| `SMS_ALLOWED_COUNTRIES` | `CA` | no (default `CA`) | Only `CA` is supported (Q-086). Canadian geographic area codes are checked |
| `SMS_MAX_CHARS` | `480` | no (default `480`) | Cap on prefix + text after normalization. May only be lowered; the OpenAPI `maxLength` is 480 |
| `SMS_MAX_LINES` | `8` | no (default `8`) | |
| `SMS_ALLOWED_PRINCIPALS` | `<oid>` | yes | Comma-separated object IDs; the Foundry resource managed identity's `oid` (E6) |
| `SMS_AUTH_AUDIENCE` | `api://<app id>` | yes | Must be exactly `api://<application id GUID>`, otherwise `unavailable`. Both it and the bare app ID are accepted as `aud` |
| `SMS_AUTH_TENANT_ID` | `<tid>` | yes | |
| `SMS_REQUIRE_ROLE` | `false` | no (default `false`) | Set `true` once `roles: ["Sms.Send"]` has been seen on a Foundry token (E13) |
| `SMS_MIN_INTERVAL_SECONDS` | `90` | no | Cooldown between claimed sends |
| `SMS_MAX_PER_HOUR` | `6` | no | Hourly cap (UTC hour), 1 to 20 |
| `SMS_DEDUPE_MINUTES` | `30` | no | Same normalized text inside the window is not sent again |
| `SMS_PREFIX` | `""` | no | Optional fixed operational prefix: a single line obeying the K12 value rules (no `/`, `:`, `@`, no `.` except between digits, no `www`), otherwise `unavailable` |
| `SMS_ALLOWED_LABELS` | `Ref,Callback #,Note` | **yes** | Comma-separated line labels the text may use (K12 rule 1): 1 to `SMS_MAX_LINES` entries, each 1 to 32 characters, no `.` `,` `:` or double spaces, no duplicates (case-insensitive). Unset, empty or invalid means every request gets `unavailable`. Set only in the azd environment; the real labels are never committed |
| `KEY_VAULT_URI` | `https://kv-sms-xxxx.vault.azure.net/` | yes (set by Bicep) | Read with the managed identity |
| `STATE_BLOB_URL` | `https://stsmsxxxx.blob.core.windows.net/sms-state` | yes (set by Bicep) | Container for dedupe, cooldown and hourly slots |
| Key Vault secret `twilio-api` | `{"account_sid":"AC…","api_key_sid":"SK…","api_key_secret":"…"}` | yes | Standard API key, not the auth token. Set by the founder |
| Key Vault secret `sms-recipients` | `{"recipients":["+1XXX5550199"]}` | yes | 1 to 3 E.164 numbers in Canada. Set by the founder |

azd environment values used by `infra/main.parameters.json`: `AGENT_TOOL` (must be `sms-notify`; the Bicep rejects anything else), `SMS_FROM_NUMBER`, `SMS_ALLOWED_PRINCIPALS`, `SMS_AUTH_AUDIENCE`, `SMS_ALLOWED_LABELS` (required, no default), optional `SMS_VAULT_OFFICER_PRINCIPAL_ID` (gets Key Vault Secrets Officer) with `SMS_VAULT_OFFICER_PRINCIPAL_TYPE` (`User` by default; `ServicePrincipal` or `Group` otherwise), `SMS_ALLOWED_COUNTRIES`, `SMS_REQUIRE_ROLE`, `SMS_PREFIX`.

`local.settings.json` is git-ignored and must never be committed.

## Entra app and role (plan P5; run in USMS02, not here)

Placeholders only. Run them in USMS02, as items on its committed resource list.

```bash
# 1. App registration with an app role and assignment required.
az ad app create --display-name sms-notify-api \
  --app-roles '[{"allowedMemberTypes":["Application"],"description":"Send follow-up SMS","displayName":"Sms.Send","isEnabled":true,"value":"Sms.Send","id":"<new-guid>"}]'
az ad app update --id <app-id> --identifier-uris api://<app-id>
az ad sp create --id <app-id>
az ad sp update --id <app-id> --set appRoleAssignmentRequired=true

# 2. Assign the role to the Foundry resource's system-assigned managed identity.
az rest --method POST \
  --uri https://graph.microsoft.com/v1.0/servicePrincipals/<foundry-mi-object-id>/appRoleAssignments \
  --body '{"principalId":"<foundry-mi-object-id>","resourceId":"<sms-notify-api-sp-object-id>","appRoleId":"<new-guid>"}'

# 3. Allowlist the same object ID in the app settings (takes effect at once, unlike roles; E13).
azd env set SMS_ALLOWED_PRINCIPALS <foundry-mi-object-id> -e sms-<env>
```

## Deploy (USMS02 only)

From `agent-tools/sms-notify/` only, with `-e sms-<env>` on every command. First run `azd env get-value AGENT_TOOL -e sms-<env>`, which must print `sms-notify`. Then run `az deployment sub validate` and `azd provision --preview`, then `azd provision` immediately followed by `azd deploy`. Never during a live call (CLAUDE.md section 10).

`requirements.txt` is exported from `uv.lock` for the Functions remote build:
`python -m uv export --no-dev --no-emit-project --format requirements-txt -o requirements.txt` (with hashes, so the remote build checks package integrity).
