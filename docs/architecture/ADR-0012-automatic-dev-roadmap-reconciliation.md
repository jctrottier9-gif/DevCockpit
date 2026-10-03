# ADR-0012 — Automatic DEV roadmap reconciliation after merged green delivery

- Status: Accepted
- Date: 2026-10-02
- Amends: ADR-0008 and ADR-0011 for deterministic post-merge delivery reconciliation only

## Context

ADR-0011 moved the DEV stop point to PR creation plus auto-merge and made DevCockpit responsible for observing CI and merge evidence. That eliminated wasteful agent polling, but it also removed a behavior that previously existed in the repositories: once a PR was merged and green, the same DEV updated the canonical roadmap to reflect the completed delivery and promote the real next item.

The current execution model already derives `ROADMAP_UPDATE_REQUIRED` only when GitHub provides strong WorkItem-associated evidence for a merged PR and green CI while the canonical WorkItem remains READY.

The existing RoadmapChangeProposal/Safe Writeback workflow protects structural or product roadmap changes such as scope, split, replacement, order or dependency changes. Requiring a second human confirmation for a purely deterministic delivery-state reconciliation adds friction without restoring a meaningful product decision.

## Decision

### Automatic follow-up

When an executable WORK item projects:

```text
ExecutionState.ROADMAP_UPDATE_REQUIRED
NextAction.RECONCILE_ROADMAP
```

DevCockpit automatically prepares an idempotent DEV PromptDispatch in the same logical session.

Its identity is bound to immutable delivery evidence:

```text
project
+ WorkItem
+ PR number
+ delivered head SHA
+ merged timestamp when available
```

Repeated polling must not create duplicate logical reconciliation work.

### DEV authority for deterministic reconciliation

The reconciliation DEV is authorized to edit the canonical GitHub roadmap directly without an additional human confirmation, after rereading current GitHub state.

The allowed mutation is deliberately narrow:

- verify the associated PR is merged and required CI is green;
- reread the current roadmap immediately before editing;
- mark the proven delivered WorkItem DONE;
- promote only the true next already-defined WorkItem(s) to READY according to the existing canonical order, dependencies and gates;
- keep later items BLOCKED when they are not yet authorized;
- keep required human roadmap prose/checklists consistent with the canonical block;
- reread GitHub after the edit and verify the resulting canonical state.

This path must not:

- change WorkItem identity;
- invent, split or delete WorkItems;
- reorder roadmap work;
- change dependencies;
- change REPLACES relations;
- reinterpret a failed/unproven delivery as DONE;
- overwrite a concurrent incompatible roadmap edit.

If any of those structural/product changes are required, the DEV stops and reports the blocker; the normal RoadmapChangeProposal / architecture / product-decision mechanisms remain authoritative.

### Architecture gates remain human-authorized

Delivery reconciliation may promote an already-defined ARCHITECTURE_GATE to READY when that is the true next canonical step.

That READY status is eligibility only.

DevCockpit must still require explicit human authorization before creating the ARCH PromptDispatch, exactly as defined by ADR-0011. The reconciliation DEV must not start the architecture analysis.

### Distinction from Safe Writeback

ADR-0008's exact-revision proposal/preview/confirmation flow remains required for structural/product roadmap mutations handled by DevCockpit's RoadmapChangeProposal machinery.

The deterministic post-merge reconciliation defined here is a separate agent-mediated GitHub edit that restores the historical DEV behavior for proven delivery completion. It does not require a second human confirmation because the authoritative delivery fact already exists in GitHub and the permitted mutation is status-only plus promotion of an existing next item.

## Consequences

- DEV agents no longer wait for CI but still complete the historical roadmap-reconciliation responsibility after DevCockpit wakes them post-merge.
- The cockpit can move from merged delivery to canonical roadmap truth without a manual confirmation click.
- The next WORK item can become READY and be picked up by deterministic orchestration.
- A next ARCHITECTURE_GATE can become READY, but still cannot launch without human authorization.
- Structural roadmap changes remain protected by the existing proposal/decision safeguards.
