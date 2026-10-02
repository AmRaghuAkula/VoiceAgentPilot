# calendar

An agent-agnostic, provider-agnostic calendar tool service. Any agent that can call an OpenAPI tool gets two operations, `check_availability` and `book_appointment`, on one calendar fixed by the tool's URL. Design: [spec](../../docs/superpowers/specs/2026-09-29-calendar-booking-design.md) (rev 3.2). Plan: [plan](../../docs/superpowers/plans/2026-09-29-calendar-booking-plan.md) (rev 1.6). Decisions: D-051 to D-057, D-059, D-066.

Built unit by unit (UC02a onward). As of UC02a this directory holds the package skeleton, the bindings loader, the contract document, the provider port, the in-memory fake provider and its conformance suite, and the guards. There is no HTTP layer, no real provider and no infrastructure yet.

## Three layers (spec section 3)

| Layer | Contents | Changes per agent or client? |
| --- | --- | --- |
| Generic tool service (this code) | Tool contract, slot engine, booking algorithm, provider adapters, auth checks, logging | No. Reused by every agent |
| Bindings (config) | Binding ID to provider, calendar, credential secret name, timezone, hours, durations, appointment types, host display name, allowed callers | Yes. One entry per agent and calendar pairing |
| Agent behavior (Foundry) | When to offer a booking, how to read times back, what to say on failure, persona | Yes. Lives in each agent's Foundry instructions, never here |

## Layout

| Path | What |
| --- | --- |
| `calendar_tools/core/` | Framework-free core (G1): `ports.py` (spec 6.2 types, `CalendarProvider`, core exceptions with their diagnostic codes), `bindings.py` (spec 4.1 loader and validation), `clock.py`, `deadline.py` (request budget and the P17 constants) |
| `calendar_tools/providers/` | `base.py` (re-exports the port for adapter authors), `registry.py`, `fake.py` (`FakeCalendarProvider`) |
| `calendar_tools/obs.py` | `mask_phone` and the one structured log line per request |
| `openapi/calendar-tools.openapi.yaml` | The canonical contract (spec section 5), with a placeholder `servers[0].url` |
| `tools/render_openapi.py` | Renders one binding's copy; changes only `servers[0].url` (G3) |
| `bindings.sample.json` | Two fictional bindings |
| `tests/` | Offline tests, the provider conformance suite (`tests/contract/`), guards G1 to G3, and `genericity_denylist.txt` |

Rendering a binding's copy of the contract:

```bash
python -m uv run python -m tools.render_openapi --base-url https://<host>/api/v1/bindings/<binding_id> --output <file>
```

## Self-containment (spec section 15.2)

- Everything lives under `agent-tools/calendar/`: its own `pyproject.toml`, `uv.lock`, tests, and later its own `infra/` and `azure.yaml`.
- It never imports from `server/` (the telephony bridge) or from any other agent tool, and neither imports from it (G1b). No shared config files.
- No business-line words, agent names, project names, person names or real phone numbers anywhere here, tests included (G2). Test data uses fictional `555-01xx` numbers under a real area code and generic names.
- `tests/genericity_denylist.txt` is the only file exempt from G2. It contains the very words it bans (sensitive names only as SHA-256 hashes), so **review it at extraction time** before any `git subtree split` into a separate or public repo.
- The real bindings file, `local.settings.json`, secrets, OAuth client downloads and refresh tokens are never committed (see `.gitignore`).

## Tests

```bash
cd agent-tools/calendar
python -m uv sync --group dev
python -m uv run pytest -q
```

Everything runs offline. The opt-in markers `-m live_google` (UC08b) and `-m live_azure` (UC06) are excluded by default.

## Configuration

App settings, validated at startup (fail closed; errors name the variable or field, never the value). Filled in as units land (plan section 3). Examples are fictional.

| Setting | Example (fictional) | Required from | Notes |
| --- | --- | --- | --- |
| `CALENDAR_BINDINGS_JSON` | contents of `bindings.sample.json` | UC02a (loader), UC09 (deployed) | The spec 4.1 bindings document. Passed through azd as `CALENDAR_BINDINGS_JSON_B64` (plan P10). The real document is never committed |
| `CALENDAR_AUTH_TENANT_ID` | `<tenant GUID>` | UC02b | The token's `tid` must equal it |
| `CALENDAR_AUTH_APP_ID` | `<application GUID>` | UC02b | `aud` must be `api://<id>` or `<id>` |
| `CALENDAR_AUTH_REQUIRED_ROLE` | `Calendar.Invoke` | UC02b | Default `Calendar.Invoke` |
| `CALENDAR_AUTH_PRINCIPAL_CLAIM` | `oid` | UC02b | One of `oid` (default, D-059), `azp`, `appid`; never `azp` on Foundry v1 tokens |
| `CALENDAR_CLAIMS_BLOB_ENDPOINT` | `https://<account>.blob.core.windows.net` | UC06 | |
| `CALENDAR_CLAIMS_CONTAINER` | `claims` | UC06 | Default `claims` |
| `CALENDAR_KEY_VAULT_URI` | `https://<vault>.vault.azure.net` | UC07 | |
| `AZURE_CLIENT_ID` | `<client GUID>` | UC06 | The user-assigned identity's client ID |

Service keys (spec section 9.2) are Key Vault secrets with fixed names: `slot-token-key`, `slot-token-key-previous` (optional) and `fingerprint-key`, each the base64 of 32 random bytes (UC05, UC07).

## Binding rules (spec section 4.1, plan P17)

`load_bindings(raw_json, provider_names)` rejects, naming only the binding ID and the field: invalid JSON and duplicate keys anywhere; unknown binding fields (so a misspelt optional field is not dropped silently); binding IDs outside `^[a-z0-9][a-z0-9-]{2,39}$` or containing 7 or more digits in a run once dashes are ignored (so date-like IDs such as `cal-2026-10-01` are rejected too: fail closed against a dash-separated phone number); unregistered providers; invalid IANA zones; malformed, inverted or overlapping `bookable_hours` windows and unknown weekday keys; durations, step, buffer or window boundaries that are not multiples of 5; a default duration outside the allowed list; `max_slots_returned` outside 1 to 10; `max_days_ahead` outside 1 to 60; a largest duration plus buffer over 240 minutes, or over the request budget (P17: 50 minutes at the initial constants); bad appointment type IDs, duplicates or an empty list; template placeholders outside the documented set; an empty `allowed_principals`; `required_contact_fields` without `name` or without `phone`/`email`; non-English locales; and an unknown `slot_selection`. `host_display_name` is optional. A binding with `enabled: false` loads and is served as unknown.
