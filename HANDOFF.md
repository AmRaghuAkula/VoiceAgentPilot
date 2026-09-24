# Handoff to Claude Code — Hireastra Voice Agent Telephony Bridge

**Read this file first, in full, before writing any code.** It exists so a fresh Claude Code session has full context without Raghu having to re-explain the project. The detailed build spec this handoff points to is [`TELEPHONY_BRIDGE_SPEC.md`](./TELEPHONY_BRIDGE_SPEC.md) — that file is the source of truth for *how* to build; this file is the source of truth for *why* and *what phase we're in*.

If anything here and the spec disagree, ask Raghu — do not guess.

---

## 1. The business objective

Hireastra is Raghu Akula's AI SaaS platform (built separately, via a different Claude Code workflow — not this repo). This repo is a new B2B product-line extension: an **AI voice-answering agent for real estate agents** that answers inbound calls, qualifies the caller, and (eventually) books a showing. It is being built and tested as a **paid pilot**, not a finished product.

Raghu is a non-technical founder (AVP at a product company in his day job). He directs the Azure portal and architecture work through a Cowork/Claude assistant session, and hands off the actual coding to Claude Code sessions in this repo. He does sign-in, purchases, and approval steps himself; he does not write code.

## 2. What "done" looks like for this repo

**Pilot success definition:** Raghu dials a real test phone number from his own mobile phone and holds a full intake conversation with the voice agent ("Alex"), with the transcript visible in Azure AI Foundry Traces. One test number, one concurrent call, inbound only, English, no calendar booking yet.

This repo's job is narrow: **build and deploy the telephony bridge that connects a real phone call to the existing Foundry agent.** It is not building the agent's conversational behavior (that already exists and lives in the Azure AI Foundry portal, not in code) and it is not building calendar/booking (a later, separate phase).

## 3. Where this fits in the overall pilot — phases

| Phase | What it covers | Status |
| --- | --- | --- |
| Stage 0 — Conversation quality | Prompt engineering for the agent's persona, ambiguous-input handling, distress handling, speech-timing config. Done entirely in the Azure AI Foundry portal (no code, not in this repo). | **Done.** Agent is at version 10, live. |
| Stage 1a — Platform feasibility | Verified Azure AI Foundry has no platform blocker for a real pilot (tool-calling, multilingual, concurrency, publish paths, guardrails all checked). One real gap found: no native telephony/phone-number publish path — hence this repo. | **Done.** |
| **Stage 1b — Telephony bridge (THIS REPO)** | Steps 1–4 below. | **In progress — this is your job.** |
| Stage 2 — Calendar/booking | An OpenAPI tool wired into the Foundry agent so it can check availability and book a showing. Separate spec, not started. | **Not started. Do not build any of it here.** |
| Stage 3 — Production hardening | Multi-client routing, Canadian production number, "end call" tool, caller-ID recognition, security review, scaling. | **Not started, not in scope for the pilot.** |

Stage 1b (telephony) itself has four steps:

1. **Decide and provision the phone number** — a business/billing decision Raghu makes (see §5), not a coding task.
2. **Create the ACS (Azure Communication Services) resource and acquire the test number** — done by the Cowork/Claude assistant in the Azure portal, not by Claude Code.
3. **Build the bridge** — **this is Claude Code's work**, detailed in `TELEPHONY_BRIDGE_SPEC.md` §5.
4. **Deploy the bridge** — **this is Claude Code's work**, detailed in `TELEPHONY_BRIDGE_SPEC.md` §6.

## 4. Work already done (so you don't redo it or second-guess it)

- **The agent itself is built and live.** `re-intake-pilot-agent` (persona "Alex"), Foundry project `hireastra`, resource `hireastra-resource`, resource group `rg-hireastra`, currently at **version 10**. Its instructions cover: persona, an ambiguous-speech-recognition confirm heuristic (e.g. "buy" heard as "bye"), a distress-priority path, an Ontario-service-area referral rule, and intake ordering. Its Interim Response threshold is set to 2500ms to avoid filler-before-reaction issues found during testing. **None of this is something you build or touch** — it lives entirely in the Foundry portal's Instructions field and Voice Configure panel. The bridge must never override agent behavior (see spec §5, modification 3).
- **A full platform feasibility audit was run** against Azure AI Foundry — no platform blocker found; telephony was the one identified gap this repo exists to close. See the "Platform Feasibility Checklist" tab of the project's Claude Docs artifact if you want the full detail (ask Raghu for the link if you need it — it's a living doc, not a file in this repo).
- **The telephony architecture and this exact build spec were designed and written** — see `TELEPHONY_BRIDGE_SPEC.md`, which is the authoritative "how to build" reference for steps 3 and 4.
- **This GitHub repo was created empty by Raghu** and is being populated with this handoff and the spec as the first commit. No application code exists yet — you are building from scratch by importing Microsoft's reference accelerator (spec §5).
- **A Raghu-facing interactive visualization** of the whole pilot (architecture, caller journeys, feasibility, decisions, acceptance tests) was built as a web page for his own understanding. It is not part of this codebase and you don't need to reference it — mentioned only so you know it exists if Raghu refers to "the blueprint."

## 5. What is NOT yet done — open prerequisites

Do not start Step 3 (the build) until these are resolved; check with Raghu/Cowork if you're unsure whether they've cleared:

- [ ] **Phone number provider decision (A/B/C)** — see spec §2. Recommended: Option B, a separate pay-as-you-go Azure subscription just for ACS, in the *same Entra tenant* as `hireastra-resource` (this matters for the managed-identity role assignment in spec §6 to work). Not yet confirmed as done at the time of this handoff.
- [ ] **ACS resource created and test number acquired** — Cowork drives this in the portal once the provider decision is made.
- [ ] **Region of `hireastra-resource` confirmed** — the bridge must deploy in the same region.
- [x] **Private GitHub repo created** — done: this repo, `AmRaghuAkula/VoiceAgentPilot`.
- [ ] **Raghu signed in with `az login` and `azd auth login`** on whatever machine actually runs the deploy.

If you're picking up this repo and any of the unchecked items above are still open, **stop and ask Raghu** rather than proceeding or making an assumption about the provider, region, or number.

## 6. Ground rules

- **No real-estate words, agent names, or phone numbers in code.** Everything client-specific is config (spec §4's reuse-boundary table). This bridge is meant to be reused for every future Hireastra voice-agent vertical, not just real estate.
- **Pin the agent version explicitly (v10), never "latest."** A portal edit to the agent must not silently change what callers hear.
- **Do not build anything in spec §7's "Do not build" list** (calendar tools, outbound calling, a new web UI, custom STT/TTS, a second telephony provider's active config, autoscaling/HA, storing transcripts outside Foundry, or changing agent behavior from the bridge) without asking Raghu first.
- **The 9 acceptance tests in spec §8 are the definition of done** for this repo's work. The pilot passes at 1–8; test 9 is a latency measurement, not pass/fail.

## 7. Who does what

| Who | Responsible for |
| --- | --- |
| Raghu | Business decisions (provider choice, subscription/repo creation, purchase approvals), signs in to Azure, places test calls |
| Cowork (Claude in chat) | Azure portal work: agent configuration, ACS resource setup, region checks, pulling traces/logs after tests |
| **Claude Code (you)** | Steps 3–4: import the accelerator, apply the modifications in spec §5, deploy per spec §6, fix failures against the acceptance tests in spec §8 |

---

Next file to read: [`TELEPHONY_BRIDGE_SPEC.md`](./TELEPHONY_BRIDGE_SPEC.md).
