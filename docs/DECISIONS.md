# Decisions Log

Append-only. Each entry records a decision future sessions must honor without reopening it. To change a decision, add a **new** entry that supersedes the old one (`Supersedes D-NNN`). Never edit or delete past entries.

If a unit's work would contradict an entry here, **stop**, raise an Open Question in [STATUS.md](STATUS.md) §3, and wait for the founder.

Format: `D-NNN` · date · decision · why · source.

---

### D-001 · 2026-09-25 · Phone number provider: Option B
A separate pay-as-you-go Azure subscription, in the same Entra tenant as `hireastra-resource`, holds ACS and the test number. The Foundry agent stays on the credits subscription.
**Why:** it's the fastest all-Microsoft path. The credits subscription may be refused number purchases. Using the same tenant lets the managed-identity role assignment work. **Source:** founder; TELEPHONY_BRIDGE_SPEC.md §2.

### D-002 · 2026-09-25 · Import the accelerator with git history
Microsoft's `call-center-voice-agent-accelerator` is merged in through an `upstream` remote (`--allow-unrelated-histories`). It is not forked and not copied.
**Why:** a fork of a public repo can't be private, and keeping the history keeps `git merge upstream/main` clean. **Source:** founder; spec §5.

### D-003 · 2026-09-25 · Spec env var names win; accelerator names are the fallback
`MAX_CALL_SECONDS` takes precedence over `MAX_CALL_DURATION`, and `VOICE_LIVE_ENDPOINT` over `AZURE_VOICE_LIVE_ENDPOINT`. The resolution happens in `bridge_config.py` only; upstream files are not renamed.
**Why:** acceptance test 8 and spec §6 use the spec names, while renaming upstream files would make upstream merges harder. **Source:** founder chose "keep accelerator names"; the first Opus design review showed test 8 would break, so both names are read. Design spec §1.

### D-004 · 2026-09-25 · Voice Live agent mode, pinned version, no behavior overrides
The bridge connects with `agent_name`, `project_name` and `agent_version` (`azure-ai-voicelive` ≥1.3.0). The version must match `^\d+$`; `"latest"` is never allowed. `session.update` carries only the PCM16 audio formats, plus `interim_response` if `INTERIM_RESPONSE_JSON` is set. Authentication is Entra ID only.
**Why:** Foundry must stay the single source of the agent's behavior, and an edit in the portal must never silently change what callers hear. **Source:** spec §5 mods 1–3; design spec §3.6.

### D-005 · 2026-09-25 · Every way a call ends goes through `CallSession.request_end()`
The first reason wins, and the call's shutdown runs in its own task. Webhook handlers never wait for a call to end. The Voice Live close runs as a single shared task, bounded to 5 s. Every call's in-memory session is removed on `CallDisconnected`.
**Why:** four Opus design reviews found repeated race and lifecycle bugs until every end path went through one idempotent function. **Source:** design spec §3.3.

### D-006 · 2026-09-25 · Per-call HMAC in the URL path; no numbers or secrets in URLs
The media URL is `/acs/ws/{call_key}/{HMAC("ws:"+key)}` and the callback URL is `/acs/callbacks/{call_key}/{HMAC("cb:"+key)}`. The ACS callback JWT check is optional, and only on when `ACS_CALLBACK_JWT_AUDIENCE` is set.
**Why:** access logs record URLs, and the per-call signature is single-use and useless after the call ends. **Source:** design spec §3.5.

### D-007 · 2026-09-25 · The web debug client is off by default
`/web/ws` and `/` are registered only when `ENABLE_WEB_CLIENT=true`, and that is never set in a deployed environment.
**Why:** the web client is unauthenticated, would sit on external ingress, and opens billed Voice Live sessions. **Source:** second Opus design review; design spec §3.7.

### D-008 · 2026-09-25 · Design-spec review stopped after four Opus rounds
The founder accepted rev 5 of the design spec without a fifth review round.
**Why:** by round 4 the findings were narrow edge cases, not architecture problems. Anything left is caught by the ~151 unit tests and by the code-level Opus review on each PR. **Source:** founder (Option A).

### D-009 · 2026-09-25 · Two roles: voice-agent-partner and voice-agent-builder
The **partner** plans, prioritizes, sequences and writes specs, and never writes code. The **builder** implements approved specs only, never prioritizes, never overrides the partner, and never starts without a go-ahead from both the partner and the founder. The definitions are in `.claude/agents/`.
**Why:** it keeps "what should we build and in what order" separate from "build it". **Source:** founder.

### D-010 · 2026-09-25 · Share the governance pattern with HireAstra, not the files
HireAstra's `Partner` and `Builder` skills belong to that platform and aren't reused. This repo's agents are prefixed `voice-agent-` and carry voice-agent domain knowledge. The session protocol is modeled on HireAstra's `CLAUDE.md` and `docs/CLAUDE_CODE_HANDOFF.md`.
**Why:** the domain knowledge differs between the two platforms, but the governance pattern carries over. If a third project needs the same setup, extract a shared template then. **Source:** founder chose the `voice-agent-` prefix.

### D-011 · 2026-09-25 · One unit = one session = one branch = one PR
A unit is one implementation-plan task. There is no bundling. A session ends when its unit's PR has merged and the branch has been deleted. **Supersedes** the earlier one-PR-per-milestone cadence.
**Why:** it matches the HireAstra discipline, and small reviewed PRs catch problems close to where they were introduced. **Source:** founder chose "Match HireAstra".

### D-012 · 2026-09-25 · Only one branch at a time; delete on merge
At most one non-`main` branch may exist, locally or on the remote. A branch's lifecycle is: cut from the latest `main` → implement → Opus code review → `cso` (Opus) → auto-PR → merge → delete local and remote.
**Why:** HireAstra's 2026-05-09 incident, where parallel stale branches diverged from `main`. **Source:** founder.

### D-013 · 2026-09-25 · Per-PR review pipeline
For every PR, the Opus `/code-review` runs on the diff, then `cso` on Opus runs on the same diff. The PR is opened automatically once both pass. A docs-only PR gets the code review but no `cso`, since there's no code attack surface. Specs get the Opus design review before implementation.
**Why:** the founder doesn't review code line by line, so this pipeline stands in for that review. **Source:** founder standing rule.

### D-014 · 2026-09-25 · Model use
Mechanical work runs on Sonnet. Complex, architectural or subtle work pauses to ask the founder to switch to Opus (or Fable), and the model is never switched silently.
**Why:** the founder controls the cost-versus-quality tradeoff. **Source:** founder standing rule.

### D-015 · 2026-09-25 · Milestones, test readiness and the end-of-session email
Every implementation plan gets a milestone doc with, for each milestone, a definition of done, test coverage, test readiness and PR slots. At the end of every session, the partner sends the founder a summary email containing that status.
**Why:** the founder follows progress from the email, without reading the repo. **Source:** founder standing rule.
