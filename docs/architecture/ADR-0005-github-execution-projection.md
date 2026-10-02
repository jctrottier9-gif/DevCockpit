# ADR-0005 — GitHub execution projection and strict WorkItem identity

- Status: Accepted
- Date: 2026-10-01

## Context

DC-021 connects the canonical roadmap to observable GitHub delivery evidence. The roadmap owns the stable WorkItem status (`READY / BLOCKED / DONE`), while branches, pull requests, workflow runs and merge state describe a derived execution phase.

Without a single identity rule and deterministic state priority, an incidental PR-body mention could be mistaken for delivery, stale workflow results could override the current commit, or polling could create duplicate DEV prompts.

## Decision

### Projection, not authority

Execution states are recalculated from the current canonical roadmap plus read-only GitHub evidence. They are not persisted as a second source of truth.

Initial derived states are:

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

PromptDispatch remains the persistent record of prompt actions. No Execution table is introduced by DC-021.

### Strong WorkItem identity

One centralized rule associates a pull request with a WorkItem only when at least one strong proof exists:

- the WorkItem key begins the PR title with an explicit boundary;
- a branch path segment begins with the WorkItem key and an explicit branch boundary;
- the PR body contains an exact structured line `Work-Item: <KEY>`.

Arbitrary body substrings are not identity evidence. In particular, text such as “DC-021 is intentionally out of scope” cannot associate a DC-020 PR with DC-021.

Multiple open pull requests strongly associated with the same WorkItem are ambiguous and fail closed.

### Current-head CI semantics

CI is derived only from relevant pull-request workflow runs for the current PR head SHA.

Priority is:

```text
current red conclusion
→ CI_RED

otherwise any current queued/running validation
→ CI_RUNNING

otherwise at least one observed current validation
and every current validation completed as success/neutral/skipped
→ GREEN
```

Zero observed workflows is not green.

### Execution-state priority

After roadmap/source validation, projection priority is:

```text
merged + green while roadmap still READY
→ ROADMAP_UPDATE_REQUIRED

open PR + current red CI
→ CI_RED

open PR + current running CI
→ CI_RUNNING

open PR + current green CI + mergeable
→ READY_TO_MERGE

open PR without a usable verdict
→ PR_OPEN

strong branch ahead of default branch without PR
→ DEVELOPING

no strong GitHub evidence
→ READY
```

A merged PR without sufficiently green observable CI remains `MERGED` rather than silently declaring roadmap reconciliation safe.

### Automatic observation and prompt idempotence

A bounded backend poller may invoke an explicit execution-evaluation use case. Simple GET projection routes remain read-only.

Automatic PromptDispatch creation is limited in DC-021 to:

- `READY → DEV INITIAL`;
- `CI_RED → DEV follow-up`.

The initial dispatch idempotency key is based on project + WorkItem + DEV + INITIAL + template version. A CI-red key is additionally based on immutable GitHub failure-cycle evidence: PR, current head SHA, workflow run ID and run attempt.

Both prompts reuse the logical AgentSession `<project>:DEV:<work-item>`.

## Consequences

- GitHub outages or ambiguous delivery evidence fail closed without creating automatic prompts.
- Repeated polling of unchanged evidence does not duplicate logical prompts.
- A new commit or a genuinely distinct workflow attempt can produce a new CI follow-up in the same DEV session.
- React and the Firefox companion consume the backend projection and do not reimplement GitHub identity or CI rules.
- DC-021 does not merge PRs, rerun workflows or write the roadmap as a product behavior.


## ASTRA-040 amendment — local orchestration inhibition

GitHub execution projection remains unchanged and fully observable while a blocking Handoff is active.

However, prompt preparation is a separate application concern: DC-040 may inhibit automatic DEV `INITIAL` and `CI_RED` PromptDispatch creation for a WorkItem while its local Handoff state requires Architect guidance or authorization. This inhibition does not add `NEEDS_ARCHITECT`/`NEEDS_PO` to `ExecutionState`, does not rewrite CI evidence, and must be checked transactionally with dispatch creation. See ADR-0007.


## ASTRA-041 amendment — proposal/application fencing

ExecutionProjection continues to derive only from the currently canonical roadmap plus observable GitHub delivery evidence. RoadmapChangeProposal, proposal revision, confirmation and writeback-application states are local orchestration state and are not new ExecutionState values.

The current canonical parser remains V1 until DC-041A delivers explicit V1/V2 support. SUPERSEDED and REPLACES therefore describe accepted future V2 semantics, not states understood by the current DC-021 implementation.

The existing GitHub reader remains read-only. DC-041B may add a separate targeted RoadmapWriter for the configured roadmap issue body only.

While a roadmap application is active or its remote outcome is uncertain, automatic work preparation for the affected target must be fenced locally so a projection computed before writeback cannot create a PromptDispatch after writeback. This inhibition does not mutate ExecutionProjection. See ADR-0008.
