# ADR-0002 — Orchestration model: WorkItem, Execution, Session and Dispatch

- Status: Accepted
- Date: 2026-10-01

## Context

DevCockpit must eventually coordinate multiple roles and parallel development without conflating roadmap identity, a single execution attempt, a ChatGPT conversation and an individual prompt.

## Decision

The core model keeps these concepts distinct:

### Project
Configured software project/repository.

### WorkItem
Stable roadmap identity such as `502A`, `TP-011C` or `DC-020`.

A WorkItem has canonical status derived from the roadmap, not from the most recent ChatGPT message.

### Dependency
Explicit relationship that can prevent a WorkItem from becoming executable.

Dependencies are deterministic and later form the basis of the scheduler.

### Execution
A concrete run/attempt of a WorkItem by a role.

An Execution may span several prompts and several CI cycles.

### AgentSession
Logical ChatGPT conversation identity used to keep context together.

Initial convention:

```text
<project>:<role>:<work-item>
```

Examples:

```text
RessourcePlanner:DEV:502A
RessourcePlanner:ARCH:502
TaskPlanner:PO:TP-011
```

A CI retry for the same WorkItem normally reuses the same DEV AgentSession.

### PromptDispatch
One prepared prompt destined for the Firefox companion.

The MVP wire payload is:

```json
{
  "session": "...",
  "text": "..."
}
```

Internally, a PromptDispatch may also carry stable IDs, role/work-item links, status and timestamps.

### Handoff
Persistent, explicitly confirmed request from one role to another for one Project/WorkItem.

Handoff is local orchestration state. It does not become an ExecutionState and it is not a WorkItem dependency.

### ExternalEvent
Observable event from GitHub/CI. Replays must be idempotent.

### Decision
Persistent conclusion explicitly accepted by the user from a correlated response/Handoff.

A Decision is not the imported ChatGPT response itself and does not mutate GitHub or the roadmap by its mere existence.

### ResourceLock
Future mechanism for preventing unsafe concurrent work against shared high-risk surfaces.

## Execution state

Initial derived states may include:

```text
READY
DEVELOPING
PR_OPEN
CI_RUNNING
CI_RED
READY_TO_MERGE
MERGED
ROADMAP_UPDATE_REQUIRED
BLOCKED
```

The exact state machine may evolve through an architecture gate, but canonical roadmap state and derived execution state remain separate concepts.

## Consequences

- Parallelism can later be added by allowing several Executions without changing WorkItem identity.
- Prompt history does not become execution history implicitly.
- Retries and reconnects can be made idempotent.
- PO/Architect/DEV conversations can be isolated cleanly.


## ASTRA-040 clarification

DC-040 keeps `ExecutionProjection` GitHub-derived and introduces persistent Handoff/Decision records for local orchestration waits. AgentSession remains the logical string `<project>:<role>:<work-item>`; no AgentSession table is introduced. See ADR-0007.
