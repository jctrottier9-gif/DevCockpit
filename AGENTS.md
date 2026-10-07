# AGENTS.md

This file defines the durable working rules for coding agents operating in the DevCockpit repository.

Its purpose is to let an agent complete approved development work autonomously while preserving product intent, architecture, tests, CI reliability and the GitHub roadmap.

---

## 1. Source of truth

For implementation work, use the following sources in this order:

1. current code and tests on `main`;
2. the GitHub Issue defining the requested work;
3. master roadmap GitHub issue #1 and its canonical `COCKPIT_PIPELINE_V1` block;
4. architecture/product documentation under `docs/`;
5. relevant PR discussions.

Do not treat previous ChatGPT conversations as more authoritative than the repository.

Do not redo an analysis already documented unless code or requirements materially changed.

Always synchronize with the current `main` before starting implementation.

---

## 2. Product authority

DevCockpit is a deterministic orchestration cockpit for software-delivery work. It prepares, tracks and routes work between the user, GitHub and role-specific ChatGPT conversations.

Durable product rules:

- GitHub and the canonical roadmap are authoritative for delivery state.
- ChatGPT conversations are work surfaces, never the source of truth.
- DevCockpit may prepare DEV prompts automatically when deterministic rules authorize them. DC-063A deterministically routes an already-authorized non-ARCH PromptDispatch to its exact bound conversation or a dedicated provisional tab; DC-063B automatically sends that already-authorized non-ARCH dispatch with per-session FIFO, durable SEND_ARMED, targeted confirmation and fail-stop recovery.
- A READY architecture gate is never sufficient authority to create its ARCH PromptDispatch; explicit human authorization in DevCockpit is required first. Even after authorization, ARCH/ASTRA PromptDispatch records are manual-only in the Firefox companion: the user must select an active ChatGPT conversation already placed in Work mode and launch the gate explicitly from the extension.
- The Firefox extension is a thin transport/UI adapter, not a product-state authority.
- CI, PR and merge evidence come from GitHub, not from statements made in ChatGPT.
- Orchestration rules should be deterministic whenever practical.
- AI/ChatGPT may analyze, design or implement, but DevCockpit owns routing, state derivation, dependencies and safety checks.
- No background scraping or continuous monitoring of ChatGPT conversations is part of the canonical design.
- Importing a ChatGPT response back into DevCockpit must be an explicit user action.

Do not introduce ChatGPT sending beyond the explicit ADR-0014/DC-063A/DC-063B boundaries, and do not introduce hidden conversation monitoring.

---

## 3. Target architecture

The target application is:

```text
React + TypeScript + Vite
          ↓
       FastAPI
          ↓
 Application / Domain
          ↓
 Infrastructure
   ├── SQLite / SQLAlchemy
   ├── GitHub adapter
   └── WebSocket prompt transport

Firefox WebExtension
   ↕ WebSocket
FastAPI
```

Expected source areas:

```text
frontend/                  React / TypeScript / Vite
app/main.py                FastAPI composition
app/application/           use cases and orchestration services
app/domain/                domain rules and state models
app/infrastructure/        persistence and external adapters
extension/                 Firefox WebExtension
tests/                     backend tests
docs/                      product/development documentation
docs/architecture/         ADRs
```

Preserve these boundaries unless an approved architectural change explicitly requires otherwise.

### Authority rules

- FastAPI is the mutation boundary for the web application.
- Python application/domain code is authoritative for orchestration rules.
- React must not duplicate roadmap, dependency, CI or dispatch rules.
- The browser extension must not own product state.
- GitHub access is performed through backend adapters.
- WebSocket transport failures must not silently change work state.
- External integrations must not be required for deterministic unit tests.

---

## 4. Core concepts

Keep the following concepts distinct:

- `Project`: a configured software project/repository.
- `WorkItem`: a stable roadmap step or sub-slice.
- `Dependency`: an explicit relation that can block a WorkItem.
- `Execution`: one attempt/run of a WorkItem by a role.
- `AgentSession`: logical ChatGPT conversation identity, e.g. `RessourcePlanner:DEV:502A`.
- `PromptDispatch`: a prompt prepared for delivery to the Firefox extension.
- `ConversationBinding`: durable mapping from one AgentSession to one opaque/canonical ChatGPT conversation identity; accepted by ADR-0014 and delivered by DC-063A. Firefox tab IDs remain reconstructible local cache only.
- `ChatGptPromptSend`: durable browser-to-ChatGPT send state/projection, separate from PromptDelivery; implemented by DC-063B with replayable browser status evidence and backend confirmation.
- `ExternalEvent`: an observable GitHub/CI event.
- `Decision`: an explicit imported product/architecture decision.
- `RoadmapChangeProposal`: a local, revisioned proposal for an exact roadmap mutation; it is never canonical merely because it exists or is previewed.
- `ResourceLock`: optional protection against unsafe parallel work.

Do not collapse canonical roadmap state, derived execution state and ChatGPT conversation state into a single field.

---

## 5. Prompt transport contract

The Firefox extension consumes this canonical payload:

```json
{
  "session": "nom_du_projet_ou_identifiant",
  "text": "Le prompt complet à envoyer à ChatGPT"
}
```

For the MVP, preserve this wire contract.

DevCockpit may persist richer metadata internally, including:

- prompt UUID;
- project/work item;
- role;
- execution;
- delivery status;
- timestamps.

Do not add transport fields to the canonical outbound payload unless the extension contract is deliberately versioned. ADR-0014 defines the protocol-v2 envelope delivered by DC-063A: prompt messages carry an exact `routing` snapshot or `null` while the functional `session` + `text` payload remains unchanged. Protocol v1 is explicitly incompatible with the v2 companion and must not be treated as auto-send capable.

Recommended logical session convention:

```text
<project>:<role>:<work-item>
```

Examples:

```text
RessourcePlanner:DEV:502A
RessourcePlanner:ARCH:502
TaskPlanner:PO:TP-011
```

A reconnect or retry must not silently create duplicate logical work. Delivery acknowledgement and idempotency belong to the transport/application layer.

---

## 6. ChatGPT companion boundary

The Firefox extension should remain deliberately small:

- maintain the WebSocket connection;
- receive prompt payloads;
- show a queue;
- automatically route and send only already-authorized non-ARCH PromptDispatch records, using exact ConversationBinding identity or one dedicated provisional tab, per-session serialization and fail-stop send idempotence from ADR-0014;
- keep every ARCH/ASTRA PromptDispatch manual-only after delivery to Firefox: no automatic tab creation, routing, recovery send or resume is permitted; the human selects an active ChatGPT tab in Work mode and explicitly launches the gate from the extension;
- persist SEND_ARMED before the irreversible ChatGPT click, require targeted SENT_CONFIRMED evidence, and never auto-resend an AMBIGUOUS send;
- allow the user to explicitly return the selected ChatGPT response to DevCockpit; response return remains manual even after automatic send.

Do not:

- continuously scrape all conversations;
- infer roadmap state from ChatGPT UI text;
- mark a WorkItem DONE because ChatGPT says it is done;
- persist canonical product state in browser storage;
- make the extension responsible for GitHub interpretation.

---

## 7. Delivery evidence and CI

GitHub evidence is authoritative for implementation delivery.

Typical execution progression:

```text
READY
→ DEVELOPING
→ PR_OPEN / CI_RUNNING
→ CI_RED or READY_TO_MERGE
→ MERGED
→ roadmap reconciliation
```

A ChatGPT developer message is not proof of delivery.

Before marking development complete, use observable evidence appropriate to the repository, such as:

- expected branch/PR exists;
- required CI is green;
- PR is merged when the workflow requires merge;
- canonical roadmap is reconciled.

CI failures normally route back to the same logical DEV session. Do not create a new session merely because CI failed.

A DEVELOPING work item may also be reactivated by the stale-DEV watchdog when the initial prompt has reached ChatGptPromptSend.SENT_CONFIRMED and the associated GitHub branch has shown no new commit activity for the configured inactivity window. The persisted confirmed_at timestamp is the relevant send lower bound. A restart/rebuild recovery exception applies when GitHub proves a strictly matched DEVELOPING branch with real commit activity but the local PromptDispatch/PromptDelivery/ChatGptPromptSend evidence needed to reconstruct the original send is absent: after the same inactivity window, DevCockpit may prepare one idempotent orphan-branch follow-up in the same logical DEV session, reusing the existing branch and inspecting its commits before any modification. Explicit local SEND_ARMED or AMBIGUOUS evidence always blocks this fallback and remains fail-stop. The watchdog must reuse the same logical DEV session, must be idempotent for the same stagnant branch SHA/activity evidence, and must not infer completion or start another WorkItem.

A normal implementation DEV turn ends after the PR is complete and auto-merge is armed when permitted. DevCockpit, not the DEV agent, observes CI/merge from that point. It reactivates the same DEV session automatically for actionable CI failures and, after a merged green delivery, for deterministic roadmap reconciliation.

---

## 8. Role boundaries

### Product Owner

May:
- interpret roadmap intent;
- clarify acceptance criteria;
- propose minimal redécoupage;
- decide whether work should be split or reprioritized.

Must not silently mutate implementation state.

### Architect

May:
- perform read-only architecture analysis;
- identify contradictions and durable decisions;
- recommend or author ADR changes when requested.

Architecture gates should precede dependent implementation slices when the roadmap requires them.

A READY architecture gate is an eligibility signal, not execution authority. The ARCH prompt requires an explicit human authorization action in DevCockpit; polling, dependency satisfaction, CI state and roadmap refresh must never substitute for that authorization. Authorization prepares the ARCH PromptDispatch but does not auto-send it: ARCH/ASTRA execution requires a second explicit human action in the Firefox companion against an active ChatGPT Work-mode tab.

### Developer

May:
- implement an approved slice;
- run targeted tests;
- create/update PRs;
- diagnose and correct normal CI failures.

Must not start future blocked slices without roadmap authorization.

### DevCockpit

Owns deterministic orchestration:
- active/ready work derivation;
- dependencies;
- prompt construction;
- delivery queue;
- CI/GitHub event interpretation;
- role routing;
- eventual parallel-execution safety.

---

## 9. Normal development lifecycle

When asked to implement an approved issue or roadmap tranche, the DEV turn proceeds through:

```text
understand scope
→ inspect relevant code/docs
→ implement
→ targeted tests
→ broader relevant validation
→ create/update PR
→ enable auto-merge when permitted
→ report PR + head SHA
→ STOP DEV TURN
```

After that handoff, DevCockpit observes GitHub PR/CI/merge evidence. The DEV agent must not stay active merely to poll CI.

If current-head CI becomes red, DevCockpit may automatically prepare a corrective prompt for the same logical DEV session. The DEV fixes only the observed failure within scope, pushes the correction, ensures auto-merge remains armed when permitted, reports the new head SHA, and stops again.

If CI becomes green and GitHub auto-merges, DevCockpit observes the merge and automatically prepares a ROADMAP_RECONCILE follow-up in the same DEV session. That follow-up is authorized to update the canonical GitHub roadmap directly, without an additional human confirmation, but only for deterministic delivery-state reconciliation: mark the proven delivered WorkItem DONE, promote the true next already-defined item to READY, keep later items blocked, and keep canonical/human roadmap text coherent. It must not change scope, ordering, dependencies, replacements or WorkItem identity. A DEV agent does not start the next WorkItem during reconciliation.

Architecture gates are different: DevCockpit may detect a READY `ARCHITECTURE_GATE`, but it must not create its ARCH PromptDispatch until a human explicitly authorizes that gate in the cockpit.

If repository rules prevent auto-merge or a product/architecture decision is unresolved, report the blocker rather than bypassing it.

---

## 10. Scope discipline

Implement the smallest coherent change that satisfies the issue.

Prefer incremental changes over broad refactors.

Do not:

- redesign unrelated components;
- add speculative abstractions;
- expand an issue into adjacent roadmap work;
- change product semantics merely to simplify implementation;
- introduce autonomous AI behavior as a shortcut around deterministic orchestration.

Useful neighboring cleanup that is not necessary belongs in a separate issue.

---

## 11. Persistence and migrations

SQLite is the initial persistence target. SQLAlchemy is the persistence abstraction.

Once persistent tables exist:

- use explicit migrations rather than ad-hoc schema mutation;
- preserve data unless destructive change is explicitly approved;
- use stable IDs;
- keep event/evidence data separate from canonical roadmap state;
- make idempotency keys and uniqueness constraints explicit where dispatch/replay requires them.

Do not introduce a different production database merely by assumption.

---

## 12. GitHub integration

GitHub integration must be behind an adapter/port.

Rules:

- canonical roadmap state comes from the roadmap issue;
- PR/CI/commit evidence is observed, not invented;
- a GitHub API outage must not corrupt local state;
- webhook/event replay must be idempotent;
- polling may be used initially when simpler, provided state derivation remains deterministic;
- stale-DEV detection must use GitHub branch activity evidence plus ChatGptPromptSend.SENT_CONFIRMED/confirmed_at; PromptDelivery acknowledgement alone is no longer sufficient after DC-063B, and progress must never be inferred from ChatGPT prose;
- structural roadmap writeback must be explicit and protected against stale updates;
- deterministic post-merge delivery reconciliation is a distinct DEV-mediated path: after rereading current GitHub state, the same DEV session may directly update the roadmap statuses and next READY item without human confirmation; it must fail closed instead of overwriting concurrent or structurally incompatible roadmap changes.

Do not use incidental PR-body mentions as strong work-item identity when a stricter branch/title/structured reference is available.

---

## 13. Validation

Backend baseline, once the application exists:

```bash
python -m compileall -q app tests
pytest -q
```

Frontend baseline:

```bash
cd frontend
npm install --no-audit --no-fund
npm run build
```

Extension validation should include:

- manifest validity;
- build/lint/typecheck if introduced;
- WebSocket receive/queue behavior;
- ADR-0014/DC-063B automatic-send state, SEND_ARMED barrier, targeted confirmation, replayable status and fail-stop recovery behavior;
- reconnect/idempotency behavior, including no automatic resend after ambiguous SEND_ARMED outcomes.

Run the smallest relevant tests first, then the broader validation required by the change.

Do not weaken a meaningful test only to make CI green.

---

## 14. Privacy and secrets

Never commit:

- passwords;
- API keys;
- GitHub tokens;
- OAuth client secrets;
- access/refresh tokens;
- production credentials;
- private certificates;
- local `.env` files.

Use `.env.example` for non-secret configuration examples.

Diagnostics must not print secret values.

The extension must not expose backend secrets to ChatGPT pages.

---

## 15. Pull requests

A PR should represent one coherent issue or sub-tranche.

The description should summarize:

- requested change;
- implementation;
- important decisions;
- validation performed;
- migration/compatibility implications;
- known limitations.

Merge only after required CI is green and no explicit product/architecture gate remains unresolved.

---

## 16. GitHub roadmap updates

The canonical roadmap is GitHub issue #1. There is no canonical `ROADMAP.md`.

After a merged issue/sub-tranche:

- update the roadmap when its state/order changed;
- mark only work actually complete;
- keep human roadmap prose aligned with the canonical block;
- promote only the true next MAIN item to `READY`;
- keep later MAIN items `BLOCKED` unless explicitly parallelized.

### Canonical pipeline contract

The current roadmap remains `COCKPIT_PIPELINE_V1`, and when it contains that block it is the machine-readable work-order contract. ASTRA-041 accepts a future `COCKPIT_PIPELINE_V2` with `SUPERSEDED` and `REPLACES`, but V2 must not become canonical until DC-041A has delivered explicit V1/V2 parsing and validation. Unknown versions, multiple canonical blocks, or simultaneous V1+V2 fail closed.

Any change to work order or completion state must:

- update the canonical block in the same roadmap edit;
- preserve stable keys;
- mark merged/completed work `DONE`;
- promote the actual next MAIN step to `READY`;
- keep later MAIN steps `BLOCKED`;
- never substitute PR numbers, CI runs or commit SHAs for step identity.

Do not store the active/next roadmap item in this file. `AGENTS.md` contains durable rules; the roadmap issue contains current product state.

Parallel lanes may be introduced explicitly later. Parallel work must never be inferred merely because two items exist.

A `RoadmapChangeProposal` is local orchestration state for structural/product roadmap changes, not the canonical roadmap. Proposal preview/confirmation does not change GitHub. Structural roadmap writeback must remain an explicit, exact-revision action protected against stale state and reconciled against the remote body.

A deterministic post-merge delivery reconciliation is intentionally separate: when GitHub proves a WORK item merged with green required CI while the canonical roadmap still shows it READY, DevCockpit prepares an idempotent follow-up in the same DEV session. That DEV may reread and directly edit the roadmap to mark only the delivered item DONE and promote only the true next already-defined item to READY, with no additional human confirmation. It must not alter scope, ordering, dependencies, REPLACES or WorkItem identity. If the next item is an ARCHITECTURE_GATE it may become READY, but its ARCH prompt still requires explicit human authorization.

A future `SUPERSEDED` status means replaced-but-not-delivered and must never be substituted with `DONE`.

---

## 17. Architecture decisions

Use ADRs under `docs/architecture/` for decisions that are structural, durable or costly to reverse.

Create/update an ADR when work changes, for example:

- authority boundaries;
- roadmap/execution state model;
- persistence strategy;
- WebSocket protocol/versioning;
- GitHub event ingestion;
- concurrency/resource locking;
- ChatGPT response import semantics;
- authentication/authorization.

Do not create an ADR for every small implementation detail.

---

## 18. Initial product direction

The initial delivery sequence is intentionally incremental:

```text
foundation
→ PromptDispatch persistence
→ WebSocket transport
→ Firefox prompt queue
→ GitHub projection / CI follow-up
→ explicit ChatGPT response return
→ Architect handoff
→ Product Owner redécoupage
→ dependency scheduler
→ parallel DEV executions
→ resource/conflict guards
→ observability / analytics
```

Do not jump directly to multi-agent parallel execution before the single-execution vertical slice is stable.
