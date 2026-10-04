# ADR-0015 — Hybrid cockpit and application projection contracts

- Status: Accepted
- Date: 2026-10-04
- Gate: ASTRA-070 / #42
- Parent initiative: DC-070 / #41
- Implementation: DC-070A / #43 through DC-070F / #48
- Related: ADR-0005, ADR-0009, ADR-0010, ADR-0011, ADR-0013, ADR-0014

## Context

DevCockpit already owns the canonical roadmap, dependency scheduler, parallel DEV capacity, ResourceLocks, GitHub execution evidence, architecture-gate authorization, PromptDispatch/PromptDelivery, ChatGptPromptSend, stale-DEV watchdog and explicit ChatGPT response import.

The historical RessourcePlanner cockpit provides useful UX ideas: role cards, contextual drawers, roadmap exploration and PR/CI visibility. It must remain a UX reference only. Its backend, activity heuristics and single-developer assumptions are not compatible with DevCockpit authority boundaries.

ASTRA-070 also identified three structural gaps that a visual-only redesign would not solve:

1. Orchestration is still coupled in places to the MAIN WorkItem and therefore cannot safely represent every parallel DEV selected in the cockpit.
2. Roadmap navigation does not yet expose a durable distinction between a WorkItem's own delivery issue and its parent initiative issue.
3. Existing transport/orchestration facts are projected inconsistently across Attention Center, Orchestration and the future role cards.

This ADR accepts the target cockpit architecture and the projection contracts required before the DC-070 implementation slices.

## Decision

### 1. DevCockpit remains the only runtime authority

The hybrid cockpit is a new DevCockpit presentation layer backed by DevCockpit application projections.

The RessourcePlanner cockpit may inspire navigation, hierarchy and visual treatment, but:

- its backend is not imported or called at runtime;
- its heartbeat/activity heuristics are not authoritative;
- its prompt/mission/writeback mechanisms are not reused as a second orchestration engine;
- React never reconstructs roadmap, scheduler, lock, CI or authorization rules.

No new generic `Execution` table or second workflow engine is introduced for the cockpit.

### 2. Cockpit reads are application projections and commands remain separate

The backend provides read-only cockpit projections composed from existing sources. A cockpit GET:

- creates no PromptDispatch;
- renews no ResourceLock;
- authorizes no architecture gate;
- evaluates no command as a side effect;
- does not mutate canonical roadmap state.

Commands continue to use their existing mutation paths and must revalidate current state before acting.

Periodic bounded refresh of read projections is allowed. Refresh must not call mutation/evaluation commands merely to update the screen.

### 3. Every projection carries explicit identity and observation metadata

Shared projection contracts include, where applicable:

- `project_id`;
- exact WorkItem/resource identity;
- observation timestamp;
- source availability and diagnostics;
- roadmap revision evidence, preferably body fingerprint and `updated_at`;
- GitHub evidence identity such as PR, head SHA, workflow run and attempt;
- backend-calculated permitted actions and reasons an action is blocked.

GitHub-backed projections are observed snapshots, not transactional global snapshots. The UI must preserve observation time and identity instead of implying stronger consistency than GitHub can provide.

### 4. Use one lightweight overview and load details on demand

The accepted conceptual projections are:

| Projection | Minimum responsibility | Primary slice |
|---|---|---|
| `CockpitOverview` | project, source availability, diagnostics, Attention Center summary, role summary, roadmap horizons | DC-070A |
| `DevPool` | parallel DEV projections, orchestration references, transport/watchdog summary | DC-070B |
| `RoadmapExplorer` | validated pipeline, scheduler, horizons, own issue and parent issue | DC-070C |
| `ArchitecturePanel` | gates, eligibility, authorization dispatch evidence, imported responses, explicit ADR references | DC-070D |
| `ReviewPanel` | PR/head SHA, CI/workflows/jobs, mergeability and observable auto-merge state | DC-070D |
| `InteractionSummary` | dispatch, Firefox delivery, ChatGptPromptSend and imported-response facts | minimal in DC-070B, complete in DC-070E |

The names are contractual design targets, not a requirement that each become one endpoint or persisted model.

The aggregator should reuse a coherent roadmap read and collected evidence during one computation when practical, avoiding redundant GitHub calls that can produce contradictory cards.

### 5. Navigation identity is always project + resource/WorkItem

All cockpit navigation, drawer state and detail caches are scoped by project and the selected resource identity.

At minimum:

- role/WorkItem navigation carries `project_id`;
- DEV detail carries `project_id + work_item_id`;
- PR/job/ADR/response detail caches include project plus their concrete resource identity;
- late async responses are rejected when context generation changes, including A → B → A project switches;
- opening a drawer never triggers a command;
- a detail failure stays local and does not blank the whole cockpit.

DC-062 remains the baseline for AbortController/context-reset behavior.

### 6. DEV Pool cards represent WorkItems, not assumed live ChatGPT agents

A DEV card is identified by:

~~~text
project_id + work_item_id + role DEV
~~~

The AgentSession is referenced from that work. Dispatches, CI attempts and other evidence form history around it.

The following dimensions remain distinct:

- canonical roadmap status;
- scheduler/dependency eligibility;
- capacity/slot state;
- GitHub execution/CI state;
- orchestration/handoff state;
- transport/ChatGptPromptSend state.

For example, a DEV may hold an active capacity slot while waiting for an architecture answer. `ACTIVE` must never be rendered as proof that ChatGPT is currently generating.

Absence from the executable DEV projection does not prove absence of roadmap history. BLOCKED, DONE and SUPERSEDED items remain visible through roadmap projections without inventing active slots.

### 7. Orchestration must become explicitly WorkItem-targeted for parallel DEV

DC-070B must provide/read an execution projection targeted by:

~~~text
(project_id, work_item_id)
~~~

and adapt Orchestration/handoff lookup so a selected parallel DEV does not silently fall back to `pipeline.active_ready_item` or MAIN.

This is not implemented by merely removing a MAIN guard. Targeted handoff/reprise continues to validate:

- canonical WorkItem identity;
- dependencies/scheduler eligibility;
- provenance;
- inhibition;
- AgentSession identity;
- applicable lock/capacity invariants.

A test must prove that a handoff opened from one parallel DEV cannot read or mutate the MAIN WorkItem by mistake.

### 8. Roadmap Explorer separates own delivery issue from parent issue

A WorkItem may have:

- `work_issue`: the issue defining/delivering that WorkItem;
- `parent_issue`: the initiative/parent reference carried by the canonical pipeline.

These are not interchangeable. For example, an architecture gate and its child slices can all share one parent while each has its own delivery issue.

DC-070C therefore resolves issue mappings in the backend using a bounded, validated documentary mapping contract. The resolver must:

- accept the roadmap's supported issue-number forms;
- scope parsing to the intended mapping section;
- diagnose duplicate/conflicting mappings instead of silently overwriting;
- expose missing mappings as missing;
- never change pipeline eligibility because an issue mapping is absent.

The roadmap horizons are backend-derived:

- Maintenant: current MAIN step and its operational projection;
- Parallèle: actual parallel-lane work and state;
- Ensuite: next MAIN step in canonical order, as perspective only;
- Plus tard: remaining future work;
- historical DONE and SUPERSEDED items remain distinct.

`Ensuite` is never execution authorization.

### 9. Architect and Reviewer are different supervision surfaces

The Architect surface preserves ADR-0011 and ADR-0014:

- READY means eligible, not authorized;
- explicit human authorization creates the ARCH PromptDispatch;
- transport may send an already-authorized dispatch automatically;
- imported ChatGPT response does not close the gate.

Displayed authorization must correlate to the dispatch produced by the gate-authorization command. Generic ARCH consultation dispatches must not be mistaken for gate authorization.

ADR lists shown as applicable are based on explicit references. Heuristic discovery may aid navigation but cannot be presented as an exhaustive authoritative list.

Reviewer remains a supervision surface, not a new execution role. No `PromptDispatchRole.REVIEWER` is introduced by this initiative.

DC-070D may enrich GitHub readers for workflow/job detail and auto-merge observability. Correlation preserves project, PR, current head SHA, workflow run and attempt. Stale green evidence from an older head must never replace current-head evidence. Partial/paginated GitHub results must be indicated or fully paginated as required by the view.

### 10. InteractionSummary reuses existing ChatGptPromptSend semantics

The cockpit uses the existing separation between PromptDispatch, PromptDelivery, ChatGptPromptSend and imported response.

Representative UI facts include:

- prompt prepared;
- received by Firefox;
- routing / waiting ready;
- SEND_ARMED;
- SENT_CONFIRMED;
- RETRYABLE_FAILURE;
- BLOCKED;
- AMBIGUOUS;
- imported response available.

`SENT_CONFIRMED` proves only that the intended user message was observed as sent. It does not prove comprehension, active generation or completion.

When no imported response exists, the UI says so explicitly rather than claiming that ChatGPT is responding.

No new persistence table or WebSocket protocol version is required merely to display facts already delivered by DC-063B.

### 11. The stale-DEV watchdog is projected, not reimplemented in React

The UI exposes the backend rule and its evidence, including:

- relevant branch activity;
- initial confirmed ChatGPT send;
- inactivity threshold/deadline;
- whether a bounded relaunch has already been prepared.

React does not recompute a competing "silent" or "working" state.

### 12. Migration is progressive and preserves technical surfaces until parity

The hybrid shell is introduced incrementally. Existing technical views remain accessible until their replacement provides equivalent diagnostics and actions.

The RessourcePlanner cockpit is not a runtime dependency and is not automatically deleted by this initiative. Any later inter-repository deprecation/removal is explicit and separate.

### 13. Delivery sequence remains A → F

ASTRA-070 keeps the existing canonical order. The accepted scope refinements are:

- **DC-070A** — shell, reusable drawer, project/WorkItem navigation, first backend overview; retain technical surfaces.
- **DC-070B** — DEV Pool plus WorkItem-targeted Orchestration/handoffs for parallel DEV.
- **DC-070C** — backend roadmap horizons plus validated own-issue/parent-issue resolution.
- **DC-070D** — Architect gate projection plus Reviewer PR/CI/job and ADR detail readers.
- **DC-070E** — shared interaction projection across cards, Attention Center and Orchestration; remove contradictory labels.
- **DC-070F** — functional parity, accessibility/responsive work and removal/relegation of transition duplicates.

DC-070B, DC-070C and DC-070D are not parallelized now because they share navigation identity, projection contracts and detail components. Any later parallelization requires an explicit roadmap decision.

### 14. Required validation scenarios

The implementation slices must collectively cover at least:

- a handoff on a parallel DEV that does not target MAIN;
- a cockpit GET with no side effects;
- absent and ambiguous issue mappings;
- project switching with late responses, including A → B → A;
- new CI attempt or new head while a detail panel is open;
- AMBIGUOUS send without automatic resend;
- viewing a READY architecture gate without creating a dispatch.

## Consequences

- The cockpit can become richer without creating a second source of truth.
- Multi-DEV visualization is grounded in WorkItems and evidence rather than guessed agent activity.
- Orchestration must be generalized beyond MAIN before the DEV Pool is considered complete.
- Roadmap navigation gains a durable distinction between delivery issue and parent initiative.
- Reviewer remains a read/supervision concept, avoiding a new orchestration role.
- Existing ChatGptPromptSend data becomes the common interaction vocabulary.
- The initiative remains incremental and compatible with the current multi-project workspace.
