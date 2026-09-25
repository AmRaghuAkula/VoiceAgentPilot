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
| **voice-agent-partner** ([definition](.claude/agents/voice-agent-partner.md)) | Picks the next unit, sequences work, writes specs and plans, raises and tracks Open Questions, writes the status and decision updates, sends the daily email | Write or edit code |
| **voice-agent-builder** ([definition](.claude/agents/voice-agent-builder.md)) | Implements exactly one approved unit, runs the review pipeline, opens, merges and deletes its branch | Prioritize, re-sequence, override the partner, start without a "go", write specs |

When a single Claude session plays both roles, it still follows both sets of rules, and says which role it is acting in.

## 3. Unit of work (D-011, D-012)

**One unit = one plan task = one session = one branch = one PR.** There is no bundling, and no "while I'm here, let me also…". A session ends when its unit's PR has merged and the branch has been deleted.

**Only one non-`main` branch may exist at any time, locally and on the remote.** Branch names come from the units table in [docs/STATUS.md](docs/STATUS.md) §1.

**Status updates ride inside the unit's own PR (D-017).** Before the PR merges, the partner commits the STATUS.md and DECISIONS.md updates to the unit's branch, so `main` is always accurate the moment it merges.

**The only exception is a status-only PR (D-018).** It's used when status must change and no unit branch is in flight, for example a founder answer to a Q-NNN, or a partner-only planning session. The branch is `docs/status-YYYY-MM-DD`, it follows the same one-branch rule, and it is opened, merged and deleted in the same session. It needs no review, because it has no code and no design content.

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

## 5. Per-unit execution loop

1. **Builder — verify prerequisites.** Every earlier unit this one depends on is `CLOSED` in STATUS.md §1. If not, stop.
2. **Builder — verify the plan's claims** (files, functions, signatures) with a quick grep or read before editing.
3. **Builder — cut the branch** from the latest `main`, using the name from STATUS.md §1.
4. **Builder — implement test-first**, exactly as the plan task specifies. If the plan is wrong or ambiguous, stop and hand it back to the partner. Do not redesign on the fly.
5. **Builder — run the whole test suite:** `cd server && python -m uv run pytest -q`. It must all pass.
6. **Builder — review pipeline (D-013).** Fix and re-run each step until it's clean.
   1. `/code-review` **on Opus** on our diff.
   2. `cso` **on Opus** on the same diff. This step is skipped for docs-only PRs.

   **How to get Opus when the session is on Sonnet:** dispatch each review as a subagent with the model set to Opus: `Agent(model: "opus", prompt: "Run the <code-review | cso> skill on <diff scope> …")`. If that's unavailable, ask the founder to `/model opus` for the review step, then switch back.

   **Review scope (D-016):** code imported from Microsoft's accelerator is out of scope for fixing.
   - **Normal units:** the scope is `main...HEAD`.
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
- **Don't touch** `infra/`, `hooks/` or `azure.yaml`, and don't run `azd`, until M6 is unblocked.
- **Commits:** author `Raghu Akula` (repo-local git config, see §4 bootstrap). Every commit message, including merge commits, ends with the `Co-Authored-By:` trailer of the model doing the work.

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
