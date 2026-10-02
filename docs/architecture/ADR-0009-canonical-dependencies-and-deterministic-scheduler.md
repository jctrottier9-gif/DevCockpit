# ADR-0009 — Canonical dependencies and deterministic scheduler

- Status: Accepted
- Date: 2026-10-02
- Slice: DC-050

## Context

ADR-0002 defines Dependency as an explicit concept distinct from WorkItem replacement, execution, Handoff and ResourceLock, but COCKPIT_PIPELINE_V2 has no durable dependency representation. DC-050 requires explicit dependencies, graph validation and an explainable scheduler without parallel DEV execution.

## Decision

### COCKPIT_PIPELINE_V3

V1 and V2 remain readable. V3 adds DEPENDS_ON after REPLACES:

~~~text
<!-- COCKPIT_PIPELINE_V3 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES | DEPENDS_ON
...
<!-- /COCKPIT_PIPELINE_V3 -->
~~~

DEPENDS_ON is either `-` or a comma-separated ordered list of existing WorkItem keys. REPLACES remains historical replacement only. DEPENDS_ON is the only canonical dependency relation. PARENT, row order, PRs, file similarity and AI inference never create a Dependency.

Unknown versions, mixed canonical versions, missing targets, self-dependencies, duplicate dependencies and dependency cycles fail closed.

### Dependency satisfaction

A dependency is satisfied only when the referenced canonical WorkItem is DONE. SUPERSEDED is historical replacement, not delivery, and does not satisfy a dependency.

### Scheduler projection

The scheduler is a pure backend/domain projection of one canonical roadmap snapshot. For each WorkItem it exposes canonical status, declared dependencies, unsatisfied dependencies, scheduler state, executable flag, reason, expected role when derivable and next authorized action.

Results preserve canonical row order. If several items are theoretically executable, all are exposed; no model chooses among them.

A READY WorkItem with all dependencies DONE is executable. A READY WorkItem with an unfinished dependency is blocked with WAITING_FOR_DEPENDENCY. A BLOCKED WorkItem never becomes executable merely because its dependencies are DONE; it remains blocked until an explicit READY promotion. DONE satisfies dependents but is not executable. SUPERSEDED is never executable.

### Scheduler vs ExecutionProjection

Scheduler determines authorization from the roadmap and dependency graph. ExecutionProjection independently derives the active GitHub/CI delivery phase. The execution evaluator must pass scheduler authorization before GitHub evidence can cause a new initial PromptDispatch.

Existing Handoff inhibition and roadmap-application fencing remain application barriers and are reused rather than converted into Dependencies.

### Persistence

No dependency table or migration is introduced. Dependencies are fully derivable from the authoritative GitHub roadmap; duplicating them in SQLite would create a second source of truth.

### Mono-DEV limit

DC-050 does not activate parallel DEV execution. The scheduler may expose multiple theoretical candidates, but the existing execution loop continues to operate on the canonically READY MAIN WorkItem until DC-051.

## Consequences

Dependency semantics are explicit, versioned and fail closed. Existing V1/V2 roadmaps remain readable. React renders scheduler results but does not decide eligibility. PromptDispatch creation cannot bypass scheduler authorization. Multi-DEV execution and ResourceLocks remain deferred to DC-051 and DC-052.
