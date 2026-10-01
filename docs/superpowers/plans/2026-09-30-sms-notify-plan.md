# SMS Follow-up Notification Tool — Implementation Plan (USMS00–USMS02)

Last updated: 2026-10-01 (rev 1.1: amendment A1 for spec rev 2.2 / D-064, the positive template validator, and Q-094's answer; rev 1 was written in USMS00 from spec rev 2.1, after its Opus docs review) · Spec: [../specs/2026-09-30-sms-notify-design.md](../specs/2026-09-30-sms-notify-design.md) · Milestones: [2026-09-30-sms-notify-milestones.md](2026-09-30-sms-notify-milestones.md) · Decisions: D-062, D-063

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans (one task = one unit = one session). Steps use checkbox (`- [ ]`) syntax. [CLAUDE.md](../../../CLAUDE.md) §4–§6 is the authoritative lifecycle around every task; the steps below cover only the unit's own work.

**Goal:** build and rehearse `agent-tools/sms-notify/`, a minimal HTTP tool that lets a Foundry agent send **one** agent-written SMS to a fixed follow-up contact near the end of a call, for the founder's demo on **Friday 2026-10-02**.

**Architecture (one paragraph; the spec is the source of truth):** a framework-free core (`sms_notify/core/`: normalize and validate the message, dedupe, cooldown and hourly cap through a `StateStore` port, and `notify()`) is wrapped by thin shells: a framework-free HTTP dispatcher with in-code Entra JWT auth and an always-200 envelope (`sms_notify/http/`), a Twilio adapter that implements the `Notifier` shape in P1 over raw `httpx` (`sms_notify/adapters/twilio_sms.py`), a Blob state store and a Key Vault REST secret source. One anonymous Azure Functions route (`function_app.py`) hosts it on Flex Consumption in its own resource group. The bridge (`server/`) is untouched.

**Tech stack:** Python 3.12, uv, `httpx` (async) for every outbound call (Twilio, Blob, Key Vault, JWKS), `azure-identity` for managed-identity tokens, PyJWT + `cryptography` for token validation, `azure-functions` only in `function_app.py`, pytest. Versions are pinned from the installed packages in USMS01 step 1, not from memory.

---

## 0. How to use this plan

- **The spec decides behavior; this plan decides files, order, tests and done-ness.** Where a step says "per spec §5 step 4", implement exactly that text; do not re-derive it.
- **Where this plan had to fill a gap in the spec**, it says so in §2 (P1–P6). None of them changes a spec decision.
- **If a unit finds the spec or this plan wrong** (an SDK or API behaves differently, a test cannot be written as stated), the builder stops and hands back to the partner (CLAUDE.md §5 step 4, §7).

## 1. Global constraints (every USMS unit)

**Governance**
- One unit = one session = one branch = one PR (D-011, D-012). Branch names are in STATUS.md §1c. USMS02 is a **branchless ops unit** under CLAUDE.md §3, with D-062's carve-out: its Azure resources are **persistent**, not throwaway, but its resource list is still committed first on its `docs/status-YYYY-MM-DD` branch, each item founder-approved (Q-091).
- Implementation, review fixes, the Opus code review and `cso` run on **Opus** (CLAUDE.md §0, D-034). Status commits, PR mechanics, the daily email and USMS02's ops steps follow §0's Sonnet routing; any USMS02 finding that needs a code fix becomes its own Opus unit.
- Review pipeline per CLAUDE.md §5 step 6: Opus `/code-review` then `cso` (Opus) on `main...HEAD` (`agent-tools/` is entirely our code; D-016's upstream scope does not apply). `infra/main.bicep` gets `cso` too.
- **The whole suite passes on every code unit** (CLAUDE.md §5 step 5): the bridge suite, the calendar suite once UC02a has merged, and `cd agent-tools/sms-notify && python -m uv run pytest -q` once USMS01 has merged (USMS01 itself runs it as part of its own suite).
- **Sequencing (Q-088, D-062):** USMS00 → USMS01 → USMS02, all before calendar UC02a. A live-call bridge fix may still be sequenced ahead.

**Self-containment (D-053 pattern, D-062)**
- `agent-tools/sms-notify/` has its own `pyproject.toml`, `uv.lock`, tests, `infra/` and `azure.yaml`. It never imports from `server/` or `agent-tools/calendar/`, and neither imports from it (G1).
- No real-estate words, agent names, person names or real phone numbers anywhere under `agent-tools/sms-notify/`, tests included. Fictional NANP `555-0100`–`555-0199` only. The single exemption is `agent-tools/sms-notify/tests/genericity_denylist.txt`, which holds names only as hashes (CLAUDE.md §7, calendar P6 format).
- The founder's six-line message format and its labels never appear in code, tests, Bicep defaults or the OpenAPI document. The format lives only in the Foundry instructions (spec §11); the labels are set only in the azd environment as `SMS_ALLOWED_LABELS` in USMS02 (spec K12, D-064). Tests use made-up labels.

**Infra (CLAUDE.md §10, sms-notify rule)**
- `agent-tools/sms-notify/infra/` and `azure.yaml` change only in USMS01 (write and validate, provision nothing) and USMS02 (provision and deploy).
- `azd` runs only from `agent-tools/sms-notify/`, with `-e sms-<env>` on every command. The first command of every azd session is `azd env get-value AGENT_TOOL -e sms-<env>`, which must print `sms-notify`. `az deployment sub validate` and `--preview` run before any provision; provision is immediately followed by deploy once the app exists; never during a live call. No USMS unit touches the bridge's or the calendar's infra.

## 2. Plan-time reconciliations

| # | Spec text | Gap | Resolution |
| --- | --- | --- | --- |
| P1 | §6: "the Notifier-shaped types (plan §2, P1)" | The shape comes from a call-records draft that is not in this repo, so the builder can't read it there | The shape is copied here, verbatim apart from the comments, and is the contract USMS01 implements. `JsonValue` is a local alias (`str \| int \| float \| bool \| None \| list[...] \| dict[str, ...]`). |
| P2 | §5 step 4 (rev 2.1): dedupe records its outcome | Spec rev 2 returned `already_sent` (`ok:true`) for any duplicate, even when nothing had been sent | Implement rev 2.1: the dedupe blob body is `{"at", "outcome"}`; a duplicate inside the window returns `already_sent` only for `outcome == "sent"`, otherwise `rate_limited`. The outcome write-back after the send is best-effort (a failed write leaves `pending`, which fails closed). |
| P3 | §3 K9: "anonymous route with auth done in code (calendar P4)" | Calendar P4 describes calendar's dispatcher, not this one | Same pattern: `sms_notify/http/dispatcher.py` exposes `Request`/`Response` dataclasses and `async def handle(request) -> Response`; `function_app.py` only adapts `azure.functions.HttpRequest` to it. Every HTTP test goes through `handle()`, not the Functions package. |
| P4 | §3 K7: Key Vault "over REST (calendar P5)" | — | Same pattern: `GET {vault}/secrets/{name}?api-version=7.4` with `httpx` and an `azure-identity` token, mockable with `httpx.MockTransport`. |
| P5 | §3 K6: an Entra app `sms-notify-api` with app role `Sms.Send` | The spec's Bicep list (§6, step 7 below) doesn't create tenant objects | The Entra app registration, its app role, `appRoleAssignmentRequired=true`, and the role assignment to the Foundry resource's managed identity are created in **USMS02** with `az ad app` / `az ad sp` / Graph commands, as items on its committed resource list. USMS01 writes those commands into `agent-tools/sms-notify/README.md` (with placeholders only) and does not run them. |
| P6 | §4: per-recipient outcomes, worst code on partial success | P1's `send` returns one `SendResult` or raises, so a single call can't report "first recipient sent, second failed" | **The core calls `send` once per recipient**, each time with a single-recipient `NotifierConfig` (`recipients` of length 1), one after another under the K5 deadline. It collects each result or exception, logs each outcome (masked), and applies the worst-code rule: `send_unconfirmed` > `unavailable` > `send_failed`, and `sent` only if every recipient was sent. That final code is also the outcome written back to the dedupe blob. The adapter's `validate_config` rejects any config without exactly one recipient. The partial-success test lives in the core tests, not the adapter's. |

**P1, the `Notifier` shape (contract for USMS01):**

```python
@dataclass(frozen=True)
class NotifierConfig:
    notifier: str
    channel_config: Mapping[str, JsonValue]  # e.g. the sender number
    credential_secret_name: str | None       # None = the service's managed identity
    recipients: tuple[str, ...]              # from config only; validated by validate_config

@dataclass(frozen=True)
class OutboundMessage:
    subject: str | None                      # None for SMS
    text_body: str                           # plain text, already normalized
    idempotency_key: str                     # here: the sha256 of the normalized text

@dataclass(frozen=True)
class NotifierCapabilities:
    formats: frozenset[str]                  # SMS: {"text_short"}
    max_body_chars: int                      # SMS: 480
    native_idempotency: bool                 # SMS: False (Twilio doesn't dedupe)
    leaves_boundary: bool                    # SMS: True
    residency: str                           # "ca" | "us" | "global" | "configurable"; SMS: "global"

@dataclass(frozen=True)
class SendResult:
    provider_message_id: str | None          # opaque; logged for tracing

class Notifier(Protocol):
    name: ClassVar[str]
    def capabilities(self, cfg: NotifierConfig) -> NotifierCapabilities: ...
    def validate_config(self, cfg: NotifierConfig) -> None: ...   # pure; validates recipients for this channel
    async def send(self, cfg: NotifierConfig, message: OutboundMessage) -> SendResult: ...
```

Semantics the adapter tests pin: `send` delivers one message to every recipient in `cfg.recipients` (here always exactly one, P6), with the body exactly as given; it never adds recipients, never adds tracking, never follows links, and never retries. Errors: `NotifierUnavailable(maybe_sent: bool)` (timeout or dropped connection after sending → `send_unconfirmed`; before sending → `send_failed`), `NotifierAuthError` and `NotifierRejected` (→ `send_failed`), `NotifierConfigError` and `SecretStoreUnavailable` (→ `unavailable`). The core maps them to the spec §4 result codes; per-recipient results feed the worst-code rule (P6). If the K5 deadline has run out before a recipient's turn, the core doesn't call `send` for it and counts it as `send_failed`.

## 3. Units

| Unit | What | Kind / branch | Model | Prerequisites |
| --- | --- | --- | --- | --- |
| **USMS00** | The spec (rev 2.1), this plan and the milestone doc; D-062 and D-063 (both ACCEPTED); STATUS §1c; Q-085 to Q-093 (all ANSWERED) and Q-094 (OPEN); the CLAUDE.md governance lines | Docs PR, branch `docs/sms-notify-spec`, with the Opus docs review (D-013) | Opus (spec), Sonnet (PR) | UC01 merged (#52) |
| **USMS01** | Build spec §4–§9: code, tests, `infra/`, the OpenAPI document, and a README with the config table. **It provisions nothing** | Code branch `feat/usms01-sms-notify`, Opus `/code-review` and `cso` on `main...HEAD` | **Opus** | USMS00 merged |
| **USMS02** | Deploy and rehearse (§4 below): provision and deploy (`-e sms-demo`); the Entra app (P5); the founder loads the Key Vault secrets; the `live_twilio` send; a prompt-kind copy agent; production attach right before the demo; one live test call; detach after the demo; rotate the Twilio API key (Q-094) | Branchless ops, status branch `docs/status-YYYY-MM-DD` (session date). Resource list committed first. **Resources are persistent** (D-062 carve-out), each founder-approved (Q-091) | Sonnet (ops); Opus for any code fix (a separate unit) | USMS01 merged |
| **USMS03** | **Contingent.** Twilio API key rotation (§4 step 5, Q-094), only if USMS02's session ends before it. Changes no infra | Branchless ops, status branch `docs/status-YYYY-MM-DD` | Sonnet (ops) | USMS02 merged, tool detached |

**Timeline:** USMS00 on 2026-09-30. USMS01 on 2026-10-01, in one Opus session. USMS02 **starts** on the morning of 2026-10-02, before the demo, and its session **runs through the demo to the detach** (§4 step 4): its status branch is the only branch for that whole time and is merged and deleted only after the detach evidence is recorded. If the session is interrupted after the demo, the next session resumes that status branch (CLAUDE.md §4 step 1) and finishes the detach first. Calendar UC02a moves back by about two sessions (Q-088).

**Contingency, USMS02b (named here so CLAUDE.md §10 allows it).** If the rehearsal finds a code defect that must be fixed before the demo: USMS02 records its evidence so far, then merges and deletes its status branch (the resources stay, under D-062's carve-out). The partner sequences an Opus fix unit on branch `fix/usms01-<slug>`, which goes through the normal pipeline. **USMS02b** (branchless, its own `docs/status-YYYY-MM-DD` branch) then runs `azd deploy -e sms-demo` under the §1 infra rules and continues USMS02's remaining steps through the detach. If there isn't time, no fix is attempted, and the demo goes ahead without the SMS tool attached.

### USMS00 — Spec, plan and governance (docs)

- [ ] Land the spec, this plan and the milestone doc.
- [ ] Append D-062 and D-063 to DECISIONS.md; add STATUS §1c, Q-085 to Q-093 (ANSWERED) and Q-094 (OPEN); apply the CLAUDE.md governance lines (§1, §3, §4 bootstrap, §5 steps 1, 3 and 5, §7, §9, §10, §11).
- [ ] Opus docs review on `main...HEAD` until CLEAN; open the PR; status commit (USMS00 `CLOSED`, USMS01 `NEXT`); merge with a merge commit; delete the branch.

### USMS01 — Build `agent-tools/sms-notify/` (code; provisions nothing)

Implements spec §4 (contract), §5 (processing order), §6 (layout), §7 (config), §8 (security checks) and §9 (tests), with P1–P6.

- [ ] **Step 1 — Prerequisites.** USMS00 is `CLOSED`. Run `az functionapp list-flexconsumption-locations` and confirm East US 2 offers Flex Consumption with Python 3.12 (Q-085); if not, stop and hand back. Create the uv project and pin `httpx`, `PyJWT[crypto]`, `azure-identity`, `azure-functions` and `pytest` from what `uv add` installs, not from memory.
- [ ] **Step 2 — Scaffold and guards first.** `pyproject.toml` (with `addopts = -m "not live_twilio"` and the `live_twilio` marker registered), `.gitignore` covering `local.settings.json`, and the guard tests G1 (no `server` or `calendar_tools` import, AST scan), G2 (the phone regex from calendar P6 and the hashed-name denylist in `tests/genericity_denylist.txt`, including self-tests that pass on their own file) and G3 (caplog: no body, no full number, no secret). They fail first, then pass as code lands.
- [ ] **Step 3 — Core messages and limits, test-first.** `core/messages.py` (normalization, limits, the K12 template validator per amendment A1 below, GSM-7/UCS-2 segment helper) and `core/limits.py` (dedupe with outcome per P2, cooldown stamped at claim time, hourly slots) and `core/service.py` (`notify()`: the processing order of spec §5, the K5 deadline arithmetic, per-recipient sending and the worst-code rule per P6, against a fake `Notifier`) using `FakeStateStore` and `FakeClock`; all spec §9 "core" and "limits" tests.
- [ ] **Step 4 — Twilio adapter, test-first.** `adapters/twilio_sms.py` implementing P1 against `httpx.MockTransport`: exact URL and host, form fields `To`/`From`/`Body`, Basic auth with the API key, 201 → sent, 4xx/5xx → `send_failed` with only the Twilio error code, timeout or drop after sending → `send_unconfirmed` with no retry, failure before sending → `send_failed`, and `validate_config` rejecting anything but exactly one recipient (P6). Plus `adapters/blob_state.py` (`If-None-Match`/`If-Match` semantics) and `adapters/keyvault_secrets.py` (P4), each with mocked-transport tests.
- [ ] **Step 5 — Auth and dispatcher, test-first.** `http/auth.py` and `http/dispatcher.py` (P3): locally minted RS256 keys and JWKS; JWKS caching and the refresh on an unknown `kid`, at most once per 60 s (a second unknown `kid` inside that window is 401 with no fetch); both `aud` forms; the 60 s leeway; JWKS unreachable → 200 `unavailable`; the 401 cases; `forbidden` as 200; `SMS_REQUIRE_ROLE` on and off; the parametrized always-200 matrix; unknown fields and any `to`/`recipient` field → `invalid_request`; config errors → `unavailable` with no number in the message.
- [ ] **Step 6 — OpenAPI document and contract test.** `openapi/sms-notify.json` per spec §4 with a placeholder server host; the contract test pins one operation, only `message`, `additionalProperties:false`, `maxLength` 480 equal to `SMS_MAX_CHARS`'s default, the response `code` enum without `content_rejected`, the `reason` enum (A1), and the description (spec §4 rev 2.2 text) passing G2.
- [ ] **Step 7 — Infra, validated only.** `infra/main.bicep` + `main.parameters.json` + `azure.yaml`: RG `rg-sms-notify-<env>`, Flex app with a system-assigned MI, storage with shared-key access disabled (the MI gets Storage Blob Data Contributor on `sms-state`), Key Vault in RBAC mode (the MI gets Key Vault Secrets User; the founder gets Key Vault Secrets Officer), app settings from spec §7 (no secret values; `SMS_ALLOWED_LABELS` and `SMS_PREFIX` come from azd environment parameters with **no** default value in the repo, so an unset label list fails closed), HTTPS only, TLS 1.2, no CORS, and a storage lifecycle rule that deletes blobs under `sms-state/dedupe/` and `sms-state/rate/` one day after last modification (spec §5 "State retention"). `az bicep build` and `az deployment sub validate` pass. **Nothing is provisioned**, and no azd environment is created.
- [ ] **Step 8 — README.** The configuration table (every spec §7 setting and secret, including `SMS_ALLOWED_LABELS` with a made-up example and its fail-closed note; fictional examples only), the P5 Entra commands with placeholders, and the local test command.
- [ ] **Amendment A1 — positive template validator (spec rev 2.2, K12, D-064; founder direct instruction 2026-10-01; applied mid-unit on this branch).** Replaces the TLD link check.
  - **Builder changes:**
    1. In `core/messages.py`, delete `_LINK_TLDS`, `_TLD_LINK` and `contains_link`, and remove every link-specific test.
    2. Add the K12 validator with the rules exactly as spec §3 "Template validator" states them. Keep the K11 normalization in front of it, including Cf stripping and the full-width and ideographic dot mapping.
    3. Add `SMS_ALLOWED_LABELS` parsing and validation, with an empty or invalid list giving `unavailable`. Validate `SMS_PREFIX` against K12 rules 4 to 6.
    4. Retire `content_rejected` everywhere: the service, the OpenAPI enum and the tests. Every content failure becomes `invalid_request` with `reason`.
    5. Add the optional `reason` field to the response envelope, the dispatcher and the OpenAPI schema, as an enum of the eight values.
    6. Replace the OpenAPI operation description with the spec §4 rev 2.2 text.
    7. Add the `SMS_ALLOWED_LABELS` app setting in Bicep, with no default, and its README config row.
  - **Tests the builder must write** (made-up labels only, for example `SMS_ALLOWED_LABELS=Contact,Callback #,Reason,Amount,When,Channel`; never the founder's labels):
    - **T1, accepted.** A six-line message using all six labels, with fictional values. It includes `Amount: $650K`, a line with `$1.5M`, `Callback #: 416-555-0142` and an accented name such as `Zoë Example`. It passes unchanged. The same message with a label in another case, or with lines in another order, or with a label left out, also passes.
    - **T2, rejected values.** Each of these as a value gives `invalid_request` with `bad_character`: `pay-now.top`, `evil.ru`, `goo.gl/x`, `203.0.113.5/x`, `203.0.113.5`, `1.2.3`, `http://x`, `a@b.com`, `J.Smith`, `Jr.`, `3 months.`, `wwwexample`, `x_y` and `"quoted"`.
    - **T3, bad line shape.** Each of these gives `bad_line`: a bare `http://x` line, `Contact:` with nothing after it, `Contact:value` with no space, and a line with no colon.
    - **T4, unknown label.** `Website: x` gives `unknown_label`.
    - **T5, duplicate label.** The same label twice, including in another case, gives `duplicate_label`.
    - **T6, empty or invalid label config.** Each of these gives `unavailable`, with nothing claimed and nothing sent: `SMS_ALLOWED_LABELS` unset, empty, only commas, an entry containing `.` or `:`, an entry with a double space, duplicate entries, more than `SMS_MAX_LINES` entries, or an entry over 32 characters.
    - **T7, `SMS_PREFIX`.** A prefix containing `/`, `@`, `a.b` or a newline gives `unavailable`. A valid prefix still moves the 480 boundary down by its length.
    - **T8, Unicode tricks.** These are still rejected after normalization, as `bad_character`: `evil` + U+200B + `.ru`, `evil` + U+00AD + `.ru`, `evil` + U+FF0E + `ru`, `evil` + U+3002 + `ru`, and the full-width `ｅｖｉｌ．ｒｕ`.
    - **T9, no echo.** No rejection's response body contains any of the message text or the label names. Its keys are exactly `ok`, `code`, `retry` and `reason`. The G3 caplog test also covers these paths.
    - **T10, no claim.** Every `invalid_request` path makes no state-store call and no Twilio call.
    - **T11, check order.** A message that is both too long and has a bad character gives `too_long`. A message whose first line has an unknown label and whose second line has a bad character gives `unknown_label`.
    - **T12, contract.** The OpenAPI `code` enum has no `content_rejected`; the `reason` enum equals the eight K12 values; the description is the rev 2.2 text and passes G2.
- [ ] **Step 9 — Close.** The whole suite (bridge, calendar if present, sms-notify) passes; Opus `/code-review` and `cso` until clean; open the PR `USMS01: SMS follow-up notification tool`; partner status commit; merge with a merge commit; delete the branch.

### USMS02 — Deploy and rehearse (branchless ops)

See §4 for the procedure. Steps in order:

- [ ] Cut the `docs/status-YYYY-MM-DD` branch **first**, and commit and push the resource list (RG, Flex app and plan, storage, Key Vault, Log Analytics/App Insights if the Bicep creates them, the Entra app `sms-notify-api` and its service principal and role assignment, the copy agent) before anything is created. Each item is approved under Q-091.
- [ ] `azd env new sms-demo` from `agent-tools/sms-notify/`; set `AGENT_TOOL=sms-notify` and the non-secret settings, including `SMS_ALLOWED_LABELS` set to the six labels listed under spec §11 (exactly as the snippet writes them); confirm the guard prints `sms-notify`; `az deployment sub validate` and `azd provision --preview -e sms-demo`; then `azd provision` immediately followed by `azd deploy`.
- [ ] Create the Entra app and role assignment (P5); set `SMS_ALLOWED_PRINCIPALS` to the Foundry resource MI's `oid`.
- [ ] The founder loads `twilio-api` and `sms-recipients` with `az keyvault secret set` personally (§6 item 4).
- [ ] Run `-m live_twilio` once (fictional body, no personal data).
- [ ] §4 steps 1–5 (copy agent, failure drill, production attach, detach after the demo, Twilio API key rotation).
- [ ] Record redacted evidence (codes, latencies, segment counts; no numbers, bodies, SIDs, `oid`s or GUIDs), including the detach after the demo; then open, merge and delete the status branch in the same session (which runs through the demo; see the timeline above). Q-094 is answered (keep the service, detach, rotate the key); record the detach and the rotation (no key values) in the evidence. If the PR adds a D-NNN or revises this plan or the spec, it gets the Opus docs review first.

## 4. USMS02 rehearsal and production attach (D-049-safe)

D-049 and UC01 E9 mean any **saved** edit to the production agent goes live on the next call, and the Active-version selector doesn't hold it back.
1. **Copy agent first.** The founder duplicates the production agent as a new **prompt-kind** agent (a voice-kind agent returns empty responses, E1), attaches the tool (OpenAPI, managed identity, audience `api://…`), and pastes the spec §11 snippet. Test it in the playground with text, then with voice through a UC01-style harness kept in the session scratchpad (calendar P7 approach). The SMS should arrive, and a second attempt should get `already_sent` or `rate_limited`.
2. **Failure drill** on the copy agent: temporarily set `SMS_ALLOWED_PRINCIPALS` to a dummy value. The agent should get `forbidden` and say nothing alarming. Then restore it.
3. **Production attach** 30 to 60 min before the demo (Q-090, answered). Attach the tool and paste the snippet in **one save**. Make one test call from the founder's mobile and confirm the SMS arrives.
4. **After the demo:** detach the tool and remove the snippet in one save (Q-090, answered). Delete the copy agent and its Foundry agent identity.
5. **Rotate the Twilio API key (Q-094, answered).** The founder creates a new Standard API key in the Twilio console, loads it personally with `az keyvault secret set` on `twilio-api`, and then revokes the old key. No values pass through an agent or the repo. The service stays deployed, with the tool detached. If USMS02's session ends before this step, it runs as **USMS03** (branchless, its own `docs/status-YYYY-MM-DD` branch). USMS03 is named here so CLAUDE.md §10 allows it, and it changes no infra.

## 5. Definition of Done

- **USMS00:** the spec has passed its Opus docs review. D-062 and D-063 are recorded as ACCEPTED. STATUS §1c and Q-085 to Q-093 are added, all marked ANSWERED, and Q-094 is added as OPEN. CLAUDE.md is updated. It's merged and the branch deleted.
- **USMS01:** everything in the CLAUDE.md §9 universal DoD. Every spec §9 test exists and the whole suite passes. `/code-review` and `cso` on Opus are both clean. G1 to G3 pass, so there are no real-estate words, agent names, person names or real numbers under `agent-tools/sms-notify/` (fictional `555-01xx` only). Bicep validates, and nothing is provisioned. The README config table is complete.
- **USMS02:** the resource list is committed first. Provisioning is validated and previewed first. `live_twilio` passes. The copy agent sends exactly one SMS, and a repeat attempt is refused. The failure drill passes. The production test call delivers the SMS. **After the demo, the tool is detached from production** (Q-090), by USMS02 or, under the §3 contingency, by USMS02b. The Twilio API key is rotated (Q-094), by USMS02 or USMS03. Redacted evidence is recorded, with no numbers or bodies. The status PR is merged.

## 6. Founder checklist (in order)

1. ~~Answer Q-085 to Q-093~~: **done on 2026-09-30.**
2. ~~Paid account and Standard API key~~: **done.** Keep the SID and secret out of chat.
3. **Before USMS02:** in the Twilio console, set Messaging **Geo Permissions** to Canada only. Send one test SMS from the existing number to the recipient, and confirm it arrives.
4. After USMS02 provisions the service: `az keyvault secret set` for `twilio-api` and `sms-recipients`. The founder runs this personally, so the values never pass through an agent.
5. Create the **prompt-kind copy agent**, attach the tool, paste the spec §11 snippet, and run the copy-agent tests.
6. 30 to 60 min before the demo (Q-090): attach the tool to production and paste the snippet in **one save**. Make one test call from your mobile, and check that the six-line SMS arrives.
7. After the demo: **detach** the tool and remove the snippet from the production agent in one save (Q-090), then delete the copy agent.
8. Then rotate the Twilio API key (Q-094): create a new key, run `az keyvault secret set` on `twilio-api` yourself, and revoke the old key.

## 7. Consistency with DECISIONS.md (checked in USMS00)

- **D-004 / D-049:** the service authors no agent content (spec K1); the format lives in Foundry instructions. Production edits are one save, with a copy agent first, because unpinned mode makes any save live (D-049, UC01 E9).
- **D-011 / D-012 / D-053:** one branch at a time; USMS02 is branchless with D-062's persistent-resource carve-out, recorded in CLAUDE.md §3.
- **D-013 / D-034:** Opus docs review on USMS00; Opus build, code review and `cso` on USMS01.
- **D-042:** applied by analogy through the `sms-<env>` / `AGENT_TOOL=sms-notify` rule (CLAUDE.md §10).
- **D-050 / D-059:** the 5 s deadline keeps model time plus tool time inside the 15 s OpenAPI watchdog; OpenAPI-direct here does not decide calendar's D-059(a).
- **D-052 / D-057 / D-063:** the §7 lifts are narrow and per tool; the bridge still wires no tools; outbound calling stays banned.
- **TELEPHONY_BRIDGE_SPEC.md:** unchanged; D-063 records the lifts, as D-052 did for calendar.
- **Post-demo lifecycle:** the persistent resources outlive USMS02. Q-094 is ANSWERED (founder, 2026-09-30): keep the service, detach the tool, rotate the key (§4 step 5, USMS02 or USMS03). A teardown or Canada move is still its own unit, named in this plan or a new D-NNN (CLAUDE.md §10).
- **D-064 / D-063:** the K12 template validator only narrows what D-063's lift lets through; D-063's scope and data set are unchanged.

## 8. Review history

- **USMS00 Opus docs review (PR #53):**
  - Round 1: NEEDS-FIXES (1 blocking: per-recipient outcomes, leading to P6; 6 should-fix; 7 nits).
  - Round 2: NEEDS-FIXES from two independent reviewers (0 blocking, 3 should-fix, 6 nits).
  - Round 3: CLEAN from two reviewers, with their optional nits applied.
  - Every finding was fixed on Opus. Details are in STATUS.md §2 and the spec §16 changelog.
- **D-064 amendment (2026-10-01, mid-USMS01):** after USMS01's code review round 1, the founder ruled that the message holds nothing beyond the six fields. Spec rev 2.2 (K12) and amendment A1 above were written by the partner on Opus and got an Opus docs review on the amendment's own commit (STATUS.md §2).
