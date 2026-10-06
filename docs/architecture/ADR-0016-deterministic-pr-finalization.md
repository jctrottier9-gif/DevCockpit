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

Execution evidence records the PR base branch/SHA and the compare behind_by value. A current-head-green PR with behind_by > 0 projects BASE_OUTDATED / SYNC_BRANCH.

The synchronization attempt identity is project + WorkItem + PR + head SHA + base SHA. Only one mutation attempt is allowed for that immutable evidence pair. A later base SHA creates new evidence and may authorize one new attempt.

A successful branch update invalidates every CI result attached to the old head. The normal reader then observes the new head and waits for CI on that head. A conflict or GitHub refusal is persisted as BRANCH_SYNC_BLOCKED; only a conflict that actually requires source resolution may create a targeted same-session DEV follow-up.

### Merge finalization

A green, up-to-date PR with auto-merge already armed projects WAIT_AUTO_MERGE; DevCockpit does not race GitHub.

Without auto-merge, DevCockpit revalidates immediately before mutation that the PR is still open, the head SHA has not moved, current-head pull-request workflows are still fully green, GitHub still reports the PR mergeable and not aggregate-blocked, and the chosen merge method is enabled by repository configuration.

The merge request includes the expected head SHA. GitHub protection/review rules are never bypassed. A refusal is persisted and projected as MERGE_BLOCKED.

### Restart safety

Mutation attempts are claimed durably before the network mutation. Re-observing the same immutable evidence after restart never creates a second attempt. IN_PROGRESS is fail-closed until GitHub evidence proves the operation advanced; successful or blocked outcomes are also durable.

This local attempt record is orchestration evidence only. It never replaces GitHub as delivery authority and never marks a WorkItem DONE.

## Consequences

- The nominal READY_TO_MERGE -> DEV MERGE_PR prompt path is removed.
- Auto-merge remains an optimization, not a progression dependency.
- A stale green CI result cannot authorize merge after branch synchronization changes the head.
- Branch conflicts and merge/protection refusals become visible operator states instead of silent stalls.
- Post-merge roadmap reconciliation remains a separate existing mechanism.
- Architecture gates and human product decisions are unchanged.
