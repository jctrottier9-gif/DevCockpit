# ADR-0001 — Authority boundaries and sources of truth

- Status: Accepted
- Date: 2026-10-01

## Context

DevCockpit coordinates work spanning GitHub, local application state, a Firefox extension and role-specific ChatGPT conversations.

Without explicit authority boundaries, the system could become fragile: a ChatGPT message could accidentally become product state, extension storage could disagree with GitHub, or UI heuristics could silently advance a roadmap.

## Decision

Authority is separated as follows:

1. **Canonical roadmap state** is owned by the roadmap GitHub issue and its `COCKPIT_PIPELINE_V1` block.
2. **Delivery evidence** is owned by observable GitHub artifacts: issues, branches, commits, pull requests, workflow runs and merge state.
3. **DevCockpit persistence** owns local orchestration records that are not canonical roadmap facts, such as prompt dispatch attempts, configured projects and explicit imported decisions.
4. **ChatGPT conversations** are work surfaces. Their prose is not authoritative evidence of delivery.
5. **Firefox extension state** is transport/UI state only. It never becomes the canonical source for roadmap or execution truth.
6. Significant writeback to GitHub must be explicit and protected against stale state.

## Consequences

- A developer saying “done” in ChatGPT does not mark a WorkItem `DONE`.
- CI failure/success is derived from GitHub rather than conversation text.
- A dropped WebSocket connection does not advance or roll back roadmap state.
- DevCockpit may cache projections, but must be able to reconstruct authoritative delivery state from GitHub.
- Future automation must preserve explicit product/architecture gates.

## Rejected alternatives

### ChatGPT conversation as execution authority
Rejected because conversation text is not a reliable transactional system.

### Browser extension as state owner
Rejected because browser-local state is difficult to reconcile and should remain disposable.

### Fully local roadmap database
Rejected for the initial product because it would duplicate the GitHub roadmap and create two competing authorities.
