# Hireastra Voice Agent — Telephony Bridge (Pilot)

Bridge that connects a real inbound phone call to the `re-intake-pilot-agent` ("Alex", v10) running in Azure AI Foundry, for the Hireastra real-estate voice-agent pilot.

**Start here:** [`HANDOFF.md`](./HANDOFF.md) — objective, phases, what's done, what's open, ground rules.

**Build spec:** [`TELEPHONY_BRIDGE_SPEC.md`](./TELEPHONY_BRIDGE_SPEC.md) — the detailed, authoritative build and deploy instructions (Steps 3–4).

## Bridge implementation notes

**Upstream:** imported from [Azure-Samples/call-center-voice-agent-accelerator](https://github.com/Azure-Samples/call-center-voice-agent-accelerator) at `a4f40bc3da8cd9c523dd1109e1c7175f90ec0839`. The accelerator's own README is at [docs/ACCELERATOR_README.md](docs/ACCELERATOR_README.md). To pull Microsoft's fixes:

```bash
git fetch upstream
git merge upstream/main
```

Merge upstream with a merge commit, never squash or rebase, so the shared history stays intact.

**Where our code lives:**
- **New files (ours):** `server/app/bridge_config.py`, `server/app/routing.py`, `server/app/log_mask.py`, and `server/app/providers/acs/{signing,call_session,bridge_calls,callback_auth}.py`, plus the tests under `server/tests/`.
- **Upstream files we hook into:** `server/server.py`, `server/app/call_loop.py`, `server/app/call_manager.py`, `server/app/handler/voicelive_media_handler.py`, `server/app/providers/acs/__init__.py` (the ACS routes) and `server/app/providers/acs/media_handler.py`, plus `server/pyproject.toml`, `server/uv.lock` and `server/.env.sample`. The changes are kept as hooks into our new files where possible; the ACS routes, the ACS media handler and the Voice Live handler carry the largest edits.
- `server/app/providers/acs/event_handler.py` is upstream code the bridge no longer uses. The other telephony providers (Twilio, Infobip, Genesys, Sinch, Bandwidth) are upstream code the bridge does not change.

**Voice Live:**
- The bridge uses `azure-ai-voicelive` `>=1.3.0,<2` (locked at 1.3.0 in `server/uv.lock`) in **agent mode**: it connects with `agent_name`, `project_name` and a pinned `agent_version` taken from `AGENT_ROUTING_JSON`. The version must be a positive whole number written as a string with no leading zero (for example `"10"`); `"latest"` is rejected at startup.
- Agent mode authenticates through `DefaultAzureCredential` only (Entra ID). `AZURE_VOICE_LIVE_API_KEY` is used only by the accelerator's model-mode path (which the web debug client and the other upstream providers still use), and only when `AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID` is unset.
- The API version is the SDK default (`2026-07-15` in 1.3.0) unless `VOICE_LIVE_API_VERSION` is set. Because an SDK upgrade can change that default, deployed environments should set `VOICE_LIVE_API_VERSION=2026-07-15` explicitly. Before production, confirm in Microsoft's docs whether agent mode is GA or still preview.
- In agent mode the bridge sends only the PCM16 input and output audio formats in `session.update`. It sends no instructions, voice or turn detection; Foundry is the only source of the agent's behavior.
- `interim_response` is sent only if `INTERIM_RESPONSE_JSON` is set, and that is only done if acceptance test 4 fails. **Current state: not set.**

**Configuration:** every bridge variable is listed in [`server/.env.sample`](server/.env.sample); the defaults live in `server/app/bridge_config.py`. When `ACS_CONNECTION_STRING` is set (ACS configured), `AGENT_ROUTING_JSON`, `MEDIA_WS_TOKEN` (at least 32 characters) and `ACS_COGNITIVE_SERVICES_ENDPOINT` are required, and the bridge refuses to start if any is missing or invalid.

**Environment variable aliases (the spec name wins, D-003):**

| Spec name (preferred) | Accelerator fallback | Notes |
| --- | --- | --- |
| `MAX_CALL_SECONDS` | `MAX_CALL_DURATION` | The fallback is read only when `MAX_CALL_SECONDS` is unset or blank. Default 600, maximum 3600. |
| `VOICE_LIVE_ENDPOINT` | `AZURE_VOICE_LIVE_ENDPOINT` | The fallback is read only when `VOICE_LIVE_ENDPOINT` is unset or blank. |

Both names are resolved in `server/app/bridge_config.py` only; upstream files keep the accelerator names.

**Security:**
- The media WebSocket and callback URLs carry a per-call HMAC signature in the path, keyed by `MEDIA_WS_TOKEN`, with a different purpose prefix for each so one can't be replayed as the other. Phone numbers and secrets never appear in URLs.
- The media WebSocket signature is single-use: the socket can be opened only once per call. The callback signature is **not** single-use; it stays valid for the whole call.
- Because of that, the ACS callback JWT check (on when `ACS_CALLBACK_JWT_AUDIENCE` is set) **must be enabled in every deployed environment** (D-036). Only the audience is configurable; the issuer and JWKS URL are constants in `server/app/providers/acs/callback_auth.py`. Confirm all three against a live ACS callback at deploy time (Q-005).
- `ENABLE_WEB_CLIENT=true` exposes an unauthenticated `/web/ws`. It is for local debugging only; the bridge refuses to start if it is on while a telephony provider is configured (D-028).

**Logs:**
- Phone numbers appear as `***1234`.
- Each call logs `call_ended reason=<...>` once, plus `call_key`, `conn`, the Voice Live `session_id` and `conversation_id`.
- `voicelive_closed_ms` is the time from the call ending to the Voice Live session closing; acceptance test 6 needs it under 5000.

**Tests:**

```bash
cd server
python -m uv sync --extra acs --group dev
python -m uv run pytest -q
```
