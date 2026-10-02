# ADR-0010 — ResourceLocks and deterministic conflict surfaces

- Status: Accepted
- Date: 2026-10-02
- Scope: DC-052

## Context

DC-050 makes canonical `DEPENDS_ON` relationships and the deterministic scheduler authoritative for work ordering. DC-051 allows more than one independent DEV WorkItem to be active when the configured DEV capacity permits it.

Dependencies and capacity are intentionally insufficient to describe every parallel-edit hazard. Two otherwise independent WorkItems can still need mutually exclusive access to a migration chain, an ADR, a roadmap mutation surface, an API contract, a critical domain module or an explicitly reserved file.

Resource locking must not become a second dependency graph, must not rely on AI guesses, and must not move orchestration authority into React or the Firefox extension.

## Decision

### 1. ResourceLock requirements are explicit local policy

A Project may declare ResourceLock requirements per WorkItem in `projects.json`.

A requirement contains:

- a stable textual `ConflictSurface` key, such as `migration:alembic`, `adr:0010`, `roadmap:#1`, `api:contracts`, `domain:critical` or `file:path/to/file`;
- one compatibility mode: `SHARED` or `EXCLUSIVE`.

These declarations are deterministic execution-safety policy only. They do not change canonical roadmap status, lane order or `DEPENDS_ON`. GitHub remains authoritative for delivery state and dependencies.

DevCockpit does not infer mandatory locks from arbitrary repository text and does not use AI as locking authority.

### 2. Compatibility is deliberately minimal

The compatibility matrix is:

| Existing | Requested | Compatible |
| --- | --- | --- |
| SHARED | SHARED | yes |
| SHARED | EXCLUSIVE | no |
| EXCLUSIVE | SHARED | no |
| EXCLUSIVE | EXCLUSIVE | no |

No additional lock modes are introduced by DC-052.

### 3. ResourceLocks are persisted

SQLite persists one stable ResourceLock identity per:

`(project_id, work_item_id, surface)`.

A record contains:

- stable lock UUID;
- Project and WorkItem owner;
- AgentSession;
- process lease-owner identifier;
- ConflictSurface;
- mode;
- state: `ACTIVE`, `RELEASED` or `STALE`;
- acquired, lease-expiration, update and optional release timestamps;
- release/recovery reason where applicable;
- optimistic version.

The unique owner/surface identity makes repoll and restart reconstruction idempotent. Multiple WorkItems may still hold the same surface only when all participating locks are SHARED.

Alembic migration `0007_resource_locks` is schema authority.

### 4. Acquisition and initial dispatch share one writer boundary

For SQLite, the existing `SqlAlchemyUnitOfWork` reserves the writer with `BEGIN IMMEDIATE`.

A new DEV start is therefore decided in this order:

```text
canonical scheduler permits WorkItem
→ DEV capacity is available
→ required ResourceLocks are compatible
→ all required locks are acquired
→ initial PromptDispatch is created
→ one transaction commits
```

ResourceLock acquisition and initial PromptDispatch creation cannot commit independently.

The repository explicitly flushes acquired locks because the UnitOfWork uses `autoflush=False`. A later candidate evaluated in the same transaction therefore observes locks acquired by an earlier candidate.

A lock conflict does not consume a DEV slot. Evaluation continues in canonical order so an independent later WorkItem may use the remaining capacity.

### 5. Active execution retains locks

CI transitions such as `CI_RUNNING`, `CI_RED` and `READY_TO_MERGE` do not release locks.

A CI-red follow-up stays in the same AgentSession and does not reacquire a second logical lock.

A normal release occurs when the WorkItem is no longer scheduler-executable according to the canonical roadmap/dependency projection.

An active Handoff inhibits new work but does not itself prove that an already-running execution ended.

### 6. Leases and stale recovery

Each application process has a unique lease-owner identifier. The default lease duration is controlled by `DEVCOCKPIT_RESOURCE_LOCK_LEASE_SECONDS`.

While the same process remains responsible for an active lock, normal execution polling renews its lease. A restarted process does not renew an old lease merely because a historical PromptDispatch exists.

Recovery rules are conservative:

- if GitHub gives positive evidence of an active execution, a restarted process may renew/take over the lease;
- if GitHub evidence is ambiguous or unavailable, an expired ACTIVE lock remains blocking rather than treating absence of proof as proof of completion;
- with determinate READY/no-active-GitHub evidence, an expired lock becomes STALE and is recoverable;
- if the same still-executable WorkItem is selected again, its stable owner/surface record is reactivated atomically without creating a duplicate initial PromptDispatch;
- if the WorkItem is no longer scheduler-executable, its ACTIVE locks are RELEASED.

The read projection can expose an expired ACTIVE lease as `LEASE_EXPIRED_PENDING_RECONCILIATION`. A mutation/evaluation cycle performs the deterministic reconciliation.

### 7. Restart behavior

ResourceLocks are persisted independently of process memory.

After restart:

- a non-expired ACTIVE lock remains authoritative and blocks incompatible new work;
- GitHub-active work can transfer the lease owner safely;
- an expired lock is reconciled using the rules above;
- existing PromptDispatch idempotency is preserved;
- no second initial PromptDispatch is created merely because a lock was recovered.

### 8. Backend owns the decision

FastAPI/application/domain code is authoritative for lock compatibility, acquisition, release and recovery.

The API exposes only projection data needed to explain:

- required surfaces;
- held locks;
- conflict surface/modes;
- holder WorkItem and AgentSession;
- lock state;
- blocking reason;
- stale/recovery state.

React renders that projection and does not duplicate compatibility rules.

The legacy `/execution/evaluate` mutation route delegates to the ResourceLock-aware parallel execution gate so it cannot bypass DC-052.

## Concurrency guarantee

DC-052 targets the existing single DevCockpit application with SQLite.

`BEGIN IMMEDIATE` serializes competing writers before lock-policy reads. Together with the persisted constraints and version field, two concurrent evaluations cannot both observe the same exclusive surface as free and commit two exclusive owners.

A real SQLite concurrency integration test demonstrates this guarantee.

DC-052 does not introduce distributed locking or multi-instance coordination.

## Consequences

Positive:

- independent DEV work remains parallel when surfaces are compatible;
- known risky surfaces can be guarded deterministically;
- conflicts are explainable and persisted across restart;
- scheduler, capacity and ResourceLocks remain separate authorities;
- no AI heuristic is needed for correctness.

Trade-offs:

- a surface must be declared to be protected;
- overly broad EXCLUSIVE declarations reduce parallelism;
- lease recovery is intentionally conservative when GitHub evidence is uncertain;
- multiple distributed DevCockpit instances are outside the guarantee of this ADR.

## Rejected alternatives

### Infer conflicts from changed files or AI analysis

Rejected as locking authority because it is incomplete, timing-dependent and not reliably explainable. Deterministic tooling may later suggest declarations, but suggestions cannot silently become authoritative locks.

### Encode conflict surfaces as DEPENDS_ON

Rejected because a dependency describes canonical work ordering, while a ResourceLock describes temporary execution compatibility. Collapsing them would corrupt the roadmap contract.

### Replace DEV capacity with locks

Rejected. Capacity remains an independent upper bound even when every surface is compatible.

### General distributed lock service

Rejected as unnecessary for the current local SQLite architecture and outside DC-052.
