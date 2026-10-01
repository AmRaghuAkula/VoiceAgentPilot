# VoiceAgentPilot — CLAUDE.md

> Claude Code reads this file automatically at the start of every session. It is the **startup guide**: the rules every agent follows, in every session, with no exceptions. Do not delete or rename it.
> Modeled on HireAstra's session protocol (see D-010 in [docs/DECISIONS.md](docs/DECISIONS.md)).
> Last updated: 2026-09-30 (USMS00: `agent-tools/sms-notify/` governance, D-062)

---

## 0. Model routing (read this first, every session)

**Implementing a unit (the builder), fixing review findings, the Opus code review, `cso`, and writing or revising a design spec all run on Opus, unconditionally** — not just when the unit looks architecturally subtle. This supersedes D-014's mechanical/architectural split for these specific task types.

Everything else — running the test suite, opening/merging a PR, sequencing the next unit, status/decision commits, the daily email, and session-start checks — stays on **Sonnet**.

| Task | Model |
| --- | --- |
| Implement a unit (code + tests) | **Opus** |
| Fix review findings | **Opus** |
| Opus code-review | **Opus** |
| `cso` security review | **Opus** |
| Write or revise a design spec | **Opus** |
| Design-review round on a spec | **Opus** |
| Run the test suite | Sonnet |
| Open the PR / merge + delete branch | Sonnet |
| Pick/sequence the next unit | Sonnet (escalate per D-014 only if genuinely subtle) |
| Status/Decisions commit, daily email | Sonnet |
| Session-start protocol checks | Sonnet |
| Architecture-tradeoff / D-002-tension decisions (D-030 delegation) | **Opus** |

Full reasoning and history: [D-034](docs/DECISIONS.md). Founder instruction, no stated end condition — applies until the founder says otherwise.

---

## 0.5 Work announcements and message prefixes (read this first, every session)

**Before dispatching the builder for any unit**, post a work announcement in chat with:
1. Task name and brief
2. Model it runs on
3. The spec path it's picked up from (file + section/line reference)
4. Any dependent task still pending/not started (real prerequisites not yet CLOSED)
5. Who approved and assigned the task (e.g. "founder go on <date>, sequenced by partner")

**After the unit's work is done**, append to that same announcement:
6. Tokens consumed
7. PR number
8. Reviews done and their verdicts (Opus code-review + `cso`, rounds and PASS/needs-fixes)

This is in addition to, not a replacement for, STATUS.md's own audit log (§5 step 8 still applies).

**Every message names the acting agent.** Prefix chat messages with the role tag (`[voice-agent-partner]`/`[voice-agent-builder]`) and, where a specific dispatched agent instance is acting, its identifying name/label — so the founder can tell which concrete agent produced which message when more than one is in flight.

**Source:** founder, direct instruction, 2026-09-27, standing effective immediately, no stated end condition.

---

## 1. What this repo is

This repo is a telephony bridge that connects a real inbound phone call (Azure Communication Services) to a Foundry agent through Voice Live. It is being built for Hireastra's real-estate voice-agent pilot, and the bridge code is client-agnostic.

- **Why and phases:** [HANDOFF.md](HANDOFF.md).
- **What to build:** [TELEPHONY_BRIDGE_SPEC.md](TELEPHONY_BRIDGE_SPEC.md).
- **Detailed design:** [docs/superpowers/specs/](docs/superpowers/specs/).
- **Task-by-task plan:** [docs/superpowers/plans/](docs/superpowers/plans/).

The repo also hosts **`agent-tools/`**, a library of agent tools that any Foundry agent can call, separate from the bridge (D-051, D-053). The first is `agent-tools/calendar/`, the calendar tool service ([spec](docs/superpowers/specs/2026-09-29-calendar-booking-design.md), [plan](docs/superpowers/plans/2026-09-29-calendar-booking-plan.md)), built as the **UC** track. The second is `agent-tools/sms-notify/`, a minimal SMS follow-up tool ([spec](docs/superpowers/specs/2026-09-30-sms-notify-design.md), [plan](docs/superpowers/plans/2026-09-30-sms-notify-plan.md)), built as the **USMS** track (D-062). Each tool is self-contained: its own `pyproject.toml`, lockfile, tests, `infra/` and `azure.yaml`. It never imports from `server/`, and `server/` never imports from it.

We build this over **many short sessions**. Nothing important may live only in someone's head or in a chat: status goes in [docs/STATUS.md](docs/STATUS.md), decisions go in [docs/DECISIONS.md](docs/DECISIONS.md), and design goes in the spec and plan.

## 2. Who does what

| Who | Does | Never does |
| --- | --- | --- |
| **Raghu (founder)** | Business decisions, approvals, Azure sign-in and purchases, test calls, "go" for each unit | — |
| **Cowork (Claude in chat)** | Azure portal work, region checks, pulling traces and logs | — |
| **voice-agent-partner** ([definition](.claude/agents/voice-agent-partner.md)) | Picks the next unit, sequences work, writes specs and plans, raises and tracks Open Questions, writes the status and decision updates, sends the daily email | Write or edit code |
| **voice-agent-builder** ([definition](.claude/agents/voice-agent-builder.md)) | Implements exactly one approved unit, runs the review pipeline, opens, merges and deletes its branch | Prioritize, re-sequence, override the partner, start without a "go", write specs |

When a single Claude session plays both roles, it still follows both sets of rules, and says which role it is acting in.

## 3. Unit of work (D-011, D-012)

**One unit = one plan task = one session = one branch = one PR.** There is no bundling, and no "while I'm here, let me also…". A session ends when its unit's PR has merged and the branch has been deleted.

**Only one non-`main` branch may exist at any time, locally and on the remote.** Branch names come from the units tables in [docs/STATUS.md](docs/STATUS.md) §1 (bridge), §1b (calendar tools, UC) and §1c (SMS follow-up tool, USMS). All tracks share this one-branch rule.

**Branchless verification and ops units (D-053).** Some plan tasks produce evidence, not code: UC01 (feasibility), UC08a (Google setup), UC10 (test-agent checks), UC11 (production attach) and UC12 (live rehearsal) in the calendar plan, like UT02 before them. Such a unit cuts **no code branch**. Its only branch is its status branch.
- Any throwaway code it needs lives only in the session scratchpad, never in the repo.
- The session's status-only branch (`docs/status-YYYY-MM-DD`, below) is cut **first**. If the unit will create throwaway Azure resources, their list is committed and pushed on that branch before anything is created. Resources need the founder's go (Q-067 for the calendar track) and are **deleted and verified deleted in the same session**.
- Its evidence, and any new D-NNN, go on that same branch, which is merged and deleted in the same session. Evidence is redacted: artifacts with personal data or identifiers (screenshots, traces, token claim values) stay outside this public repo.
- If that status PR adds a D-NNN or revises a plan or spec, it gets the Opus docs review (D-013) before merging. A plain status-only PR still needs no review.
- If it finds something that needs a code fix, the fix is a separate branch unit, sequenced by the partner.
- **USMS02** (deploy and rehearsal of `agent-tools/sms-notify/`) is also a branchless ops unit, with one narrow carve-out (D-062): its Azure resources are **persistent**, so the same-session deletion rule above does not apply to them. Everything else above still applies: the status branch is cut first, the resource list is committed and pushed before anything is created, each resource needs the founder's go (Q-091), and evidence is redacted. The carve-out covers only the resources on that committed list; any other throwaway resource it creates is still deleted in the same session.

**Status updates ride inside the unit's own PR (D-017).** Before the PR merges, the partner commits the STATUS.md and DECISIONS.md updates to the unit's branch, so `main` is always accurate the moment it merges.

**The only exception is a status-only PR (D-018; extended for branchless units above, D-053).** It's used when status must change and no unit branch is in flight, for example a founder answer to a Q-NNN, or a partner-only planning session. The branch is `docs/status-YYYY-MM-DD`, it follows the same one-branch rule, and it is opened, merged and deleted in the same session. A plain status-only PR needs no review, because it has no code and no design content. The exception is a status PR that adds a D-NNN or revises a plan or spec: it gets the Opus docs review (see the branchless-units rule above, D-053).

---

## 4. Session-start protocol (every session, in this order)

1. **Check that the tree is clean, sync, and check branches.**
   ```bash
   git status --short                      # must be empty; if not, stop and ask
   git fetch --prune origin
   git checkout main && git pull --ff-only origin main
   git branch -a
   ```
   Then handle whatever `git branch -a` shows. Always-allowed entries: `main`, `remotes/origin/main`, `remotes/origin/HEAD -> origin/main`.

   | You see | What it means | Do this |
   | --- | --- | --- |
   | Nothing else | Normal | Continue to step 2 |
   | A local branch that is **already merged** into `main` (`git branch --merged main` lists it; its remote is gone) | The founder merged the PR between sessions | Delete it: `git branch -d <branch>`. Then continue |
   | The branch of the unit `main` shows as `NEXT`, **with an open PR** | Last session's PR is waiting on a merge | **Resume it**: make sure the partner's status commit is on it (§5 step 8), then merge and delete (§5 step 9). **This merge is this session's unit.** Send the email and stop; the newly `NEXT` unit waits for the next session |
   | The branch of the unit `main` shows as `NEXT`, **with no PR** | Last session was interrupted mid-unit | **Resume it**: check it out and continue at §5 step 4. This is this session's unit |
   | A `docs/status-YYYY-MM-DD` branch belonging to the **branchless** unit `main` shows as `NEXT` (§3) | Last session's verification/ops unit was interrupted | **Resume it**: read the resource list committed on it, and first verify or finish the teardown of any throwaway resources, then continue or close the unit on that branch |
   | Anything else | A stray branch | **Stop.** Don't create any branch. Report it to the founder in chat and in the email |
2. **Read [docs/STATUS.md](docs/STATUS.md).**
   - §1: which unit is `NEXT`.
   - §3: any `OPEN` question that blocks it.
3. **Read [docs/DECISIONS.md](docs/DECISIONS.md).** The unit's work must not contradict any entry.
4. **Read the unit's task** in the current implementation plan, plus the design-spec sections that task cites.
5. **Read §8 (Code Verification Protocol) below.**
6. **Partner proposes; founder says go.** The partner states: "Next unit is Uxx (Task N — name). Blockers: none / Q-NNN. Model: Sonnet / Opus needed because …". **The builder does not start until the founder says go** (D-009).

### Fresh machine / fresh clone bootstrap

Run this once per clone, before any commit on that machine. It is safe to re-run.

```bash
git config user.name "Raghu Akula"
git config user.email "raghunagendra.akula@hotmail.com"
git remote get-url upstream 2>/dev/null || git remote add upstream https://github.com/Azure-Samples/call-center-voice-agent-accelerator.git
git fetch upstream
python -m pip install --user uv
```

Once U01 has merged (after that, `server/` exists), also run: `cd server && python -m uv sync --extra acs --group dev`.

Once UT01a has merged (Twilio pilot work, D-037), the ACS-only sync above no longer covers all the tests in the suite — it **uninstalls** the `twilio` extra if it was previously synced. Use `cd server && python -m uv sync --extra acs --extra twilio --group dev` instead, so both provider test suites collect correctly.

Once UC02a has merged (after that, `agent-tools/calendar/` exists), also run: `cd agent-tools/calendar && python -m uv sync --group dev`. It is a separate uv project with its own lockfile; never sync it from `server/` or the other way round.

Once USMS01 has merged (after that, `agent-tools/sms-notify/` exists), also run: `cd agent-tools/sms-notify && python -m uv sync --group dev`. It is another separate uv project with its own lockfile, under the same rule.

## 5. Per-unit execution loop

1. **Builder — verify prerequisites.** Every earlier unit this one depends on is `CLOSED` in STATUS.md §1, §1b or §1c. If not, stop.
2. **Builder — verify the plan's claims** (files, functions, signatures) with a quick grep or read before editing.
3. **Builder — cut the branch** from the latest `main`, using the name from STATUS.md §1, §1b or §1c.
4. **Builder — implement test-first**, exactly as the plan task specifies. If the plan is wrong or ambiguous, stop and hand it back to the partner. Do not redesign on the fly.
5. **Builder — run the whole test suite.** "The whole suite" means **both** suites, on every code unit, whichever directory the unit touches (D-053):
   - `cd server && python -m uv run pytest -q`
   - `cd agent-tools/calendar && python -m uv run pytest -q` (once UC02a has merged; before that it does not exist)
   - `cd agent-tools/sms-notify && python -m uv run pytest -q` (once USMS01 has merged; USMS01 itself runs it as part of its own suite)

   Every suite that exists must pass. Opt-in live markers (`-m live_google`, `-m live_azure`, `-m live_twilio`) run only where the calendar or sms-notify plan's unit says so.
6. **Builder — review pipeline (D-013).** Fix and re-run each step until it's clean.
   1. `/code-review` **on Opus** on our diff.
   2. `cso` **on Opus** on the same diff. This step is skipped for docs-only PRs.

   **How to get Opus when the session is on Sonnet:** dispatch each review as a subagent with the model set to Opus: `Agent(model: "opus", prompt: "Run the <code-review | cso> skill on <diff scope> …")`. If that's unavailable, ask the founder to `/model opus` for the review step, then switch back.

   **Review scope (D-016):** code imported from Microsoft's accelerator is out of scope for fixing.
   - **Normal units:** the scope is `main...HEAD`. This includes every `agent-tools/` unit: that directory is entirely our code, and upstream has no such directory.
   - **U01 and any later upstream merge:** the scope is **`git diff upstream/main HEAD`**. That is our tree compared with the pure upstream tree, which is exactly our changes.

   Log any finding in upstream code as a Q-NNN for the production security review. Never edit upstream code to satisfy a review.
7. **Builder — open the PR** (automatically, once both reviews pass). Title: `Uxx: <task name>`. The body lists the unit, the tests added and passing, the review results, and the DoD items met.
8. **Partner — status updates on the same branch.** Update STATUS.md: this unit becomes `CLOSED` with its PR number, the next unit becomes `NEXT`, and the §2 audit rows and §3 questions are updated. Append any DECISIONS.md entries. Commit and push to the unit's branch. This is **part of the unit's PR, not a new branch.**
9. **Builder — merge with a merge commit, then delete the branch** (D-016: never squash or rebase, because that would break the upstream history). The builder merges its own PR once every gate above is clean (D-019) — no separate founder OK is needed.
   ```bash
   gh pr merge <PR> --merge --delete-branch \
     --subject "Merge Uxx: <task name> (#<PR>)" \
     --body "Co-Authored-By: <the trailer of the model doing the work>"
   git checkout main && git pull --ff-only origin main
   git fetch --prune origin
   git branch -d <branch> 2>/dev/null || true
   git branch -a
   ```
   After this, `git branch -a` must show `main` only.
10. **Partner — send the daily summary email** (§6). **Stop.** Never start the next unit in the same session.

## 6. Daily summary email (partner; end of every session, even short ones)

Send it through the Gmail connector to **raghu.akula@hireastra.ai** (D-015). It covers:

- **This session:** what was done, with unit IDs and PR numbers and links.
- **Milestone status:** each milestone with DoD met or not, and its units' status.
- **Tests:** passing / planned, overall and per milestone.
- **Reviews:** Opus and `cso` results per PR.
- **Blockers and open questions** that need the founder, especially anything marked "Founder" in STATUS.md §3.
- **Next session:** the `NEXT` unit, and whether it needs Opus.

**If the unit's PR couldn't merge this session** (waiting on the founder), say so in the email. The branch stays as the one allowed branch, and the next session resumes it (§4 step 1).

Never skip the email because "not much happened". Send a short one instead.

---

## 7. Stop-and-ask triggers (don't guess; raise an Open Question in STATUS.md §3)

- A prerequisite unit isn't `CLOSED`.
- A stray branch exists, or the working tree isn't clean (§4 step 1).
- The plan or spec contradicts the actual code or the installed SDK.
- The work would contradict an entry in DECISIONS.md.
- The work touches anything in TELEPHONY_BRIDGE_SPEC.md §7 "Do not build". The only lifts are narrow and per tool: D-052/D-057 for `agent-tools/calendar/` and D-063 for `agent-tools/sms-notify/`. Each applies only inside its own tool and only as worded in its entry; none applies to the bridge, and anything outside those words is still a stop.
- The work needs a HANDOFF.md §5 prerequisite that isn't done (ACS number, region, `az login`).
- The work would change agent behavior from the bridge (instructions, voice, VAD), or use `"latest"` as an agent version.
- The task turns out more complex than planned: ask the founder to switch to Opus or Fable (D-014), and never switch silently.
- Any real-estate words, agent names, project names, person names or real phone numbers would end up in application code, or anywhere under `agent-tools/` (tests included; fictional NANP `555-01xx` numbers are fine). The only exemptions are each tool's own G2 denylist file, `agent-tools/calendar/tests/genericity_denylist.txt` and `agent-tools/sms-notify/tests/genericity_denylist.txt`; they hold sensitive names only as hashes (calendar plan P6 format), so no real name is ever committed in plaintext.
- Calendar-track or SMS-track work (any `agent-tools/` tool) would change or bypass the telephony bridge (`server/`), or make `agent-tools/` import from `server/` (or the reverse), or make one `agent-tools/` tool import from another.

## 8. Code Verification Protocol (mandatory)

Modeled on HireAstra's rule, which was written after a real incident: code was reported as "missing" based on commit history alone.

1. **Never infer code presence from commit history, PR titles, branch names or memory.** Grep or read the file on the current checkout.
2. **Every claim about code names its evidence.**

   | Evidence | Maximum confidence |
   | --- | --- |
   | File read or grep on the current checkout | High |
   | Commit or PR title | Low (≤50%) — say so |
   | File or branch name only | Very low |
   | Memory or a prior session | None — re-verify |

   If you haven't read the file, say: **"I have not verified this in the actual file — confidence is low."**
3. **Before declaring a branch merged:** check `git diff origin/main..origin/<branch> --name-only`, then read the key lines on `main`.
4. **SDK behavior** is verified against the installed package source or docs, not remembered.

## 9. Universal Definition of Done (every unit, on top of the task's own DoD)

- [ ] Every test the task specifies exists, and **the whole suite passes**: the bridge suite, the calendar suite once UC02a has merged, and the sms-notify suite once USMS01 has merged (§5 step 5).
- [ ] No real-estate words, agent names or phone numbers in `server/app/` or `server/server.py`. No real-estate words, agent names, person names or real phone numbers anywhere under `agent-tools/`, tests included (fictional numbers only, NANP `555-0100`–`555-0199`), except the hashed denylist (§7).
- [ ] No secrets or `.env` files committed. New bridge env vars are added to `server/.env.sample`; new calendar settings are added to the configuration table in `agent-tools/calendar/README.md`, and new sms-notify settings to the one in `agent-tools/sms-notify/README.md`. `local.settings.json`, real bindings files and OAuth client downloads are never committed.
- [ ] The Opus code review is clean. `cso` is clean (for code PRs). Both are scoped to our changes (D-016).
- [ ] STATUS.md and DECISIONS.md are updated **inside the unit's PR** (§5 step 8), or in a status-only PR (§3) when no unit is in flight.
- [ ] The PR is merged with a merge commit, and the branch is deleted locally and on the remote. `git branch -a` shows `main` only.
- [ ] The daily summary email is sent.

## 10. Locked rules (quick reference; details in DECISIONS.md)

- **Agent version:** pinned string of digits; never `"latest"`. No agent-behavior overrides from the bridge (D-004).
- **Phone numbers** are masked `***1234` in every log. No numbers or secrets in URLs (D-006).
- **Env vars:** the spec's names win over the accelerator's (D-003).
- **Web debug client** is off unless `ENABLE_WEB_CLIENT=true`, never in a deployed environment (D-007).
- **Upstream:** changes go in new files where possible; upstream files get small hooks only. `git merge upstream/main` must stay clean (D-002). PRs are merged with merge commits, never squashed (D-016).
- **Bridge infra:** don't touch the **bridge's** root `infra/`, `hooks/` or root `azure.yaml`, and don't run `azd` against the bridge's azd project, outside an approved M6-style unit that says to (M6 plan, D-042 standing deploy rules).
- **Calendar infra (D-053):** `agent-tools/calendar/infra/` and `agent-tools/calendar/azure.yaml` are a separate azd project. They are changed only in the calendar plan's IaC units (UC05, UC09), and `azd` runs only from `agent-tools/calendar/`, under D-042's rules applied by analogy: `-e cal-<env>` on every command; first `azd env get-value AGENT_TOOL -e <env>` must print `calendar`; `az deployment sub validate` and `--preview` before any provision; provision immediately followed by deploy once an app exists; never `azd down` on an adopted resource group; never during a live call. No calendar unit ever touches the bridge's infra, and no bridge unit touches the calendar's.
- **SMS-notify infra (D-062):** `agent-tools/sms-notify/infra/` and `agent-tools/sms-notify/azure.yaml` are another separate azd project, changed only in USMS01 (written and validated, nothing provisioned) and provisioned and deployed only in USMS02. `azd` runs only from `agent-tools/sms-notify/`, under the same D-042-by-analogy rules as the calendar's: `-e sms-<env>` on every command; first `azd env get-value AGENT_TOOL -e sms-<env>` must print `sms-notify`; `az deployment sub validate` and `--preview` before any provision; provision immediately followed by deploy once an app exists; never `azd down` on an adopted resource group; never during a live call. No sms-notify unit touches the bridge's or the calendar's infra, and neither of their units touches sms-notify's.
- **Commits:** author `Raghu Akula` (repo-local git config, see §4 bootstrap). Every commit message, including merge commits, ends with the `Co-Authored-By:` trailer of the model doing the work.
- **Model routing:** see §0 above (D-034) — build/review/spec-drafting work is Opus-only, unconditionally.

## 11. File map

| File | Purpose |
| --- | --- |
| `CLAUDE.md` | This startup guide |
| `HANDOFF.md` | Business objective, phases, prerequisites |
| `TELEPHONY_BRIDGE_SPEC.md` | Authoritative build and deploy brief (steps 3–4) |
| `docs/STATUS.md` | **Live status**: unit dashboard, audit log, open questions |
| `docs/DECISIONS.md` | Append-only decisions log |
| `docs/superpowers/specs/*-design.md` | Detailed design specs |
| `docs/superpowers/plans/*-step3.md` | Task-by-task implementation plan (TDD, exact code) |
| `docs/superpowers/plans/*-milestones.md` | Milestone definitions: DoD, test coverage, unit → branch map |
| `docs/superpowers/specs/2026-09-29-calendar-booking-design.md` | Calendar tool service design (accepted rev 3.1, D-051) |
| `docs/superpowers/plans/2026-09-29-calendar-booking-plan.md` | Calendar track (UC) implementation plan |
| `docs/superpowers/plans/2026-09-29-calendar-booking-milestones.md` | Calendar track milestones C0–C5 |
| `agent-tools/calendar/` | The calendar tool service (self-contained uv project, its own `infra/` and `azure.yaml`); created from UC02a |
| `docs/superpowers/specs/2026-09-30-sms-notify-design.md` | SMS follow-up tool design (accepted rev 2.1, D-062, D-063) |
| `docs/superpowers/plans/2026-09-30-sms-notify-plan.md` | SMS follow-up track (USMS) implementation plan |
| `docs/superpowers/plans/2026-09-30-sms-notify-milestones.md` | SMS follow-up track milestones SM0–SM2 |
| `agent-tools/sms-notify/` | The SMS follow-up tool (self-contained uv project, its own `infra/` and `azure.yaml`); created from USMS01 |
| `.claude/agents/voice-agent-partner.md` | Partner role |
| `.claude/agents/voice-agent-builder.md` | Builder role |
