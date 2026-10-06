# ADR-0016 — Deterministic pull-request finalization

- Status: Accepted
- Date: 2026-10-05
- WorkItem: DC-071 / #106
- Related: ADR-0001, ADR-0005, ADR-0011, ADR-0015

## Context

The DEV lifecycle already stops after a pull request is delivered and auto-merge is armed when GitHub permits it. Auto-merge is not a reliable progression primitive: GitHub can reject arming it while a PR later becomes clean, and a PR can become behind its base after an earlier green CI result.

Prompting a DEV merely to click merge or update an otherwise conflict-free branch adds a non-deterministic agent dependency to operations that GitHub can perform safely itself.

## Decision

DevCockpit owns two narrowly-scoped GitHub mutations after DEV delivery:

1. synchronize an open PR branch with its observed base using GitHub's update-branch API and the observed expected_head_sha;
2. merge an open, current-head-green, mergeable PR with a repository-permitted merge method and the expected head SHA.

GitHub remains authoritative for PR state, head/base identity, CI, mergeability and the mutation result.

### Branch synchronization

Execution evidence records the PR base branch, resolves that branch to its current GitHub tip SHA, and compares that current tip to the PR head. A current-head-green PR with `behind_by > 0` or GitHub `mergeable_state = behind` projects BASE_OUTDATED / SYNC_BRANCH. The `base.sha` embedded in a PR payload is not authoritative for current-base freshness because it can remain historical after the base branch advances.

The synchronization attempt identity is project + WorkItem + PR + head SHA + observed current base-tip SHA. Only one mutation attempt is allowed for that immutable evidence pair. Immediately before synchronization or merge, DevCockpit resolves `base.ref` again and fails closed if its current tip no longer matches the observed base-tip SHA. A later base tip creates new evidence and may authorize one new attempt.

A successful branch update invalidates every CI result attached to the old head. The normal reader then observes the new head and waits for CI on that head. A conflict or GitHub refusal is persisted as BRANCH_SYNC_BLOCKED; only a conflict that actually requires source resolution may create a targeted same-session DEV follow-up.

### Merge finalization

A green, up-to-date PR with auto-merge already armed projects WAIT_AUTO_MERGE; DevCockpit does not race GitHub.

Without auto-merge, DevCockpit revalidates immediately before mutation that the PR is still open, the head SHA has not moved, current-head pull-request workflows are still fully green, GitHub still reports the PR mergeable and not aggregate-blocked, and the chosen merge method is enabled by repository configuration.

The merge request includes the expected head SHA. GitHub protection/review rules are never bypassed. A refusal is persisted and projected as MERGE_BLOCKED.

### Restart safety

Mutation attempts are claimed durably before the network mutation. Re-observing the same immutable evidence after restart never creates a second attempt. IN_PROGRESS is fail-closed until GitHub evidence proves the operation advanced; successful or blocked outcomes are also durable.

This local attempt record is orchestration evidence only. It never replaces GitHub as delivery authority and never marks a WorkItem DONE.

### DC-072 temporal recovery

DC-072 extends this decision with three watchdogs evaluated by the existing execution poller:

1. an open PR with no workflow on its current head after a configurable delay prepares one same-session DEV diagnostic follow-up;
2. a current-head workflow that remains in a running/waiting status without a newer attempt or update timestamp after a separate delay prepares one same-session DEV diagnostic follow-up;
3. `WAIT_AUTO_MERGE` has a separate grace period, after which the engine reuses this ADR's existing branch-sync/merge finalizer rather than waiting indefinitely.

The watchdog identity includes the current PR head and the GitHub activity evidence relevant to the wait. A new head, workflow attempt/update or other newer GitHub proof invalidates the previous identity. A stale PREPARED watchdog dispatch is cancelled while it is still unacknowledged; after Firefox ACK there is no remote-revocation protocol, so the same-session prompt must reread GitHub and stop if its proof is stale. DEV follow-ups use deterministic `PromptDispatch` idempotency keys, while auto-merge fallback reuses the durable `PullRequestFinalizationAttempt` identity and fail-closed revalidation defined above.

No watchdog authorizes a bypass: zero workflows is never green, a stalled CI watchdog does not launch a concurrent workflow blindly, and auto-merge fallback still requires current-head green CI, mergeability and repository protections immediately before merge. These states are projections only and are surfaced in Reviewer, DEV Pool and Attention Center.

## Consequences

- The nominal READY_TO_MERGE -> DEV MERGE_PR prompt path is removed.
- Auto-merge remains an optimization, not a progression dependency.
- A stale green CI result cannot authorize merge after branch synchronization changes the head.
- Branch conflicts and merge/protection refusals become visible operator states instead of silent stalls.
- Post-merge roadmap reconciliation remains a separate existing mechanism.
- Architecture gates and human product decisions are unchanged.
