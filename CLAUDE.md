# VoiceAgentPilot — CLAUDE.md

> Claude Code reads this file automatically at the start of every session. It is the **startup guide**: the rules every agent follows, in every session, with no exceptions. Do not delete or rename it.
> Modeled on HireAstra's session protocol (see D-010 in [docs/DECISIONS.md](docs/DECISIONS.md)).
> Last updated: 2026-09-25

---

## 1. What this repo is

This repo is a telephony bridge that connects a real inbound phone call (Azure Communication Services) to a Foundry agent through Voice Live. It is being built for Hireastra's real-estate voice-agent pilot, and the bridge code is client-agnostic.

- **Why and phases:** [HANDOFF.md](HANDOFF.md).
- **What to build:** [TELEPHONY_BRIDGE_SPEC.md](TELEPHONY_BRIDGE_SPEC.md).
- **Detailed design:** [docs/superpowers/specs/](docs/superpowers/specs/).
- **Task-by-task plan:** [docs/superpowers/plans/](docs/superpowers/plans/).

We build this over **many short sessions**. Nothing important may live only in someone's head or in a chat: status goes in [docs/STATUS.md](docs/STATUS.md), decisions go in [docs/DECISIONS.md](docs/DECISIONS.md), and design goes in the spec and plan.

## 2. Who does what

| Who | Does | Never does |
| --- | --- | --- |
| **Raghu (founder)** | Business decisions, approvals, Azure sign-in and purchases, test calls, "go" for each unit | — |
| **Cowork (Claude in chat)** | Azure portal work, region checks, pulling traces and logs | — |
| **voice-agent-partner** ([definition](.claude/agents/voice-agent-partner.md)) | Picks the next unit, sequences work, writes specs and plans, raises and tracks Open Questions, runs the session-end protocol and sends the daily email | Write or edit code |
| **voice-agent-builder** ([definition](.claude/agents/voice-agent-builder.md)) | Implements exactly one approved unit, runs the review pipeline, opens, merges and deletes its branch | Prioritize, re-sequence, override the partner, start without a "go", write specs |

When a single Claude session plays both roles, it still follows both sets of rules, and says which role it is acting in.

## 3. Unit of work (D-011, D-012)

**One unit = one plan task = one session = one branch = one PR.** There is no bundling, and no "while I'm here, let me also…". A session ends when its unit's PR has merged and the branch has been deleted.

**Only one non-`main` branch may exist at any time, locally and on the remote.** Branch names come from the units table in [docs/STATUS.md](docs/STATUS.md) §1.

---

## 4. Session-start protocol (every session, in this order)

1. **Sync and check branches.**
   ```bash
   git fetch --prune origin
   git checkout main && git pull --ff-only origin main
   git branch -a
   ```
   `git branch -a` must show only `main` (and `origin/main`), or else exactly the one branch that STATUS.md §1 marks `IN PROGRESS`. **If any other branch exists, stop** and raise it with the founder. Never start new work alongside a stray branch.
2. **Read [docs/STATUS.md](docs/STATUS.md).**
   - §1: which unit is `NEXT` (or `IN PROGRESS` from last time).
   - §3: any `OPEN` question that blocks it.
3. **Read [docs/DECISIONS.md](docs/DECISIONS.md).** The unit's work must not contradict any entry.
4. **Read the unit's task** in the current implementation plan, plus the design-spec sections that task cites.
5. **Read §8 (Code Verification Protocol) below.**
6. **Partner proposes; founder says go.** The partner states: "Next unit is Uxx (Task N — name). Blockers: none / Q-NNN. Model: Sonnet / Opus needed because …". **The builder does not start until the founder says go** (D-009).

### Day-0 / fresh-machine bootstrap

This applies if the `server/` folder doesn't exist yet or `uv` isn't installed. Unit U01 (plan Task 0) is the bootstrap: it installs `uv`, imports the accelerator and sets up the test harness. On a fresh clone after U01, run the following from `server/` before any code work:

```bash
python -m pip install --user uv
python -m uv sync --extra acs --group dev
```

## 5. Per-unit execution loop (builder)

1. **Verify prerequisites.** Every earlier unit that this one depends on is `CLOSED` in STATUS.md §1. If not, stop.
2. **Verify the plan's claims** (files, functions, signatures) with a quick grep or read before editing. Checking takes seconds; finding a mismatch mid-implementation costs hours.
3. **Cut the branch** from the latest `main`, using the name from STATUS.md §1, and mark the unit `IN PROGRESS`.
4. **Implement test-first**, exactly as the plan task specifies. If the plan is wrong or ambiguous, stop and hand it back to the partner. Do not redesign on the fly.
5. **Run the whole test suite:** `cd server && python -m uv run pytest -q`. It must all pass.
6. **Review pipeline (D-013):**
   1. Opus `/code-review` on the diff. Fix and re-run until it's clean.
   2. `cso` on Opus on the same diff. Fix and re-run until it's clean. This step is skipped for docs-only PRs.
7. **Open the PR** (automatically, once both reviews pass). Title: `U0N: <task name>`. The body lists the unit, the tests added and passing, the review results, and the DoD items met.
8. **Merge, then delete the branch** locally and on the remote. Who merges is governed by Q-001 until it is answered; until then, ask the founder.
9. **Stop.** Hand over to the partner for the session-end protocol. Never start the next unit in the same session.

## 6. Session-end protocol (partner; every session, even short ones)

1. **Branch hygiene:** `git fetch --prune && git branch -a` shows only `main`. If the unit's PR couldn't merge, the unit stays `IN PROGRESS`, the reason is logged in STATUS.md §2, and the branch is the one allowed open branch.
2. **Update [docs/STATUS.md](docs/STATUS.md):**
   - §1: unit status, PR number, tests passing, review results, next `NEXT`.
   - §2: one audit row per event this session.
   - §3: open, answer or close questions.
3. **Update [docs/DECISIONS.md](docs/DECISIONS.md)** with any non-obvious choice made this session (append only).
4. **Update the milestone doc** only if a milestone's *definition* changed. Live status belongs in STATUS.md.
5. **Commit those doc updates** on the unit's branch before merge. If they come after merge, use a short `docs/status-YYYY-MM-DD` branch → PR → merge → delete, so they never sit uncommitted and the one-branch rule holds.
6. **Send the daily summary email** to the founder at the address recorded in Claude's memory for this project (D-015). The email contains:
   - **This session:** what was done, with unit IDs and PR numbers and links.
   - **Milestone status:** each milestone with DoD met or not, and its units' status.
   - **Tests:** passing / planned, overall and per milestone.
   - **Reviews:** Opus and `cso` results per PR.
   - **Blockers and open questions** that need the founder, especially anything marked "Founder" in §3.
   - **Next session:** the `NEXT` unit, and whether it needs Opus.

   Never skip the email because "not much happened". Send a short one instead.

---

## 7. Stop-and-ask triggers (don't guess; raise an Open Question in STATUS.md §3)

- A prerequisite unit isn't `CLOSED`.
- A stray branch exists (session-start step 1).
- The plan or spec contradicts the actual code or the installed SDK.
- The work would contradict an entry in DECISIONS.md.
- The work touches anything in TELEPHONY_BRIDGE_SPEC.md §7 "Do not build".
- The work needs a HANDOFF.md §5 prerequisite that isn't done (ACS number, region, `az login`).
- The work would change agent behavior from the bridge (instructions, voice, VAD), or use `"latest"` as an agent version.
- The task turns out more complex than planned: ask the founder to switch to Opus or Fable (D-014), and never switch silently.
- Any real-estate words, agent names, project names or phone numbers would end up in application code.

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

- [ ] Every test the task specifies exists, and **the whole suite passes**.
- [ ] No real-estate words, agent names or phone numbers in `server/app/` or `server/server.py`.
- [ ] No secrets or `.env` files committed. New env vars are added to `server/.env.sample`.
- [ ] The Opus code review is clean. `cso` is clean (for code PRs).
- [ ] The PR is merged and the branch is deleted locally and on the remote.
- [ ] STATUS.md §1/§2/§3 are updated; DECISIONS.md is appended if needed.
- [ ] The daily summary email is sent.

## 10. Locked rules (quick reference; details in DECISIONS.md)

- **Agent version:** pinned string of digits; never `"latest"`. No agent-behavior overrides from the bridge (D-004).
- **Phone numbers** are masked `***1234` in every log. No numbers or secrets in URLs (D-006).
- **Env vars:** the spec's names win over the accelerator's (D-003).
- **Web debug client** is off unless `ENABLE_WEB_CLIENT=true`, never in a deployed environment (D-007).
- **Upstream:** changes go in new files where possible; upstream files get small hooks only. `git merge upstream/main` must stay clean (D-002).
- **Don't touch** `infra/`, `hooks/` or `azure.yaml`, and don't run `azd`, until M6 is unblocked.
- **Commits:** author `Raghu Akula` (repo-local git config), plus the `Co-Authored-By:` trailer of the model doing the work.

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
| `.claude/agents/voice-agent-partner.md` | Partner role |
| `.claude/agents/voice-agent-builder.md` | Builder role |
