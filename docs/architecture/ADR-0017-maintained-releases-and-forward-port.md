# ADR-0017 — Maintained releases, isolated hotfixes and deterministic forward-port

- Status: Accepted
- Date: 2026-10-10
- Gate: ASTRA-075 / #133
- Parent: DC-075 / #132
- Implementation sequence: DC-075A / #134 → DC-075B / #135 → DC-075C / #136 → DC-075D / #137
- Related: ADR-0001, ADR-0002, ADR-0004, ADR-0005, ADR-0007, ADR-0009, ADR-0010, ADR-0011, ADR-0012, ADR-0013, ADR-0014, ADR-0015, ADR-0016

## Context

An approved version of a consuming repository (notably RessourcePlanner) must remain testable and maintainable independently of continuing `main` development. A hotfix must target its maintained release without importing later features, and the delivered fix must be forward-ported separately to the current `main`. GitHub is authoritative for refs, pull requests, checks and merges. ChatGPT responses never prove delivery.

Today DevCockpit predominantly identifies an execution by project + WorkItem; it matches PRs by WorkItem identity, discovers branches against the default branch, assumes `main` in several DEV/recovery prompts and does not take an explicitly expected base *name* in all finalization paths. Reusing one WorkItem across two target branches would allow ambiguous PR association, false completion and unsafe recovery.

## Decision

### 1. Two delivery WorkItems, one functional correction

Represent the release hotfix and its forward-port as **two linked, predeclared WorkItems** under the same functional correction/parent. Each owns one immutable accepted target identity, one PR lifecycle, one logical DEV session (`<project>:DEV:<work-item>`) and its own roadmap reconciliation.

- The forward-port WorkItem depends on the hotfix delivery WorkItem and must exist or be explicitly authorized **before** its execution. Post-merge reconciliation cannot invent a WorkItem, scope or dependency (ADR-0012).
- Do not make one WorkItem simultaneously own multiple version-specific executions; do not model each release as a separate DevCockpit project.
- The V3 roadmap continues to own order, identity and `DEPENDS_ON`. The Git source/target context belongs to an independently versioned structured delivery contract, never inferred from free-text ChatGPT output.
- `LANE = MAIN` is scheduler ordering terminology, not proof that the PR must target Git `main`. Parallel lanes and conflict surfaces remain explicit.

### 2. Accepted source, target and provenance contract

DC-075A establishes the versioned contract and additive persistence, with an auditable accepted snapshot and validation diagnostics. Required fields or their unambiguous structured equivalents:

- stable repository identity plus observed `owner/name`; detect repository mismatch;
- explicit mode `NORMAL | RELEASE | HOTFIX | FORWARD_PORT`;
- requested ref and kind (`tag`, `branch` or full `SHA`), resolved qualified ref and exact commit SHA (dereference annotated tags to commits);
- maintained release identity, `release/x.y` branch, origin commit SHA and maintained/retired state;
- expected target PR base **name** and observed tip SHA separately; expected working branch, immutable starting SHA and subsequent observed tip;
- functional correction link, source WorkItem, source hotfix PR, *actually integrated* commit(s)/delta, delivery head SHA and forward-port method;
- when applicable, published tag/version, source SHA, build workflow and immutable image digest.

An approved *published* version/tag is frozen; the maintained release branch is permitted to advance through separately validated fixes. A different tip is a new observation, not a rewritten historic origin.

Fail closed on absent/ambiguous/moved tag, missing or deleted ref, contradictory repository, mismatched target/base, reused release without verifiable ancestry, or changed target after execution begins. `NORMAL` without a version context retains legacy `main` behavior; `HOTFIX` without explicit release context **never** falls back to `main`. Rebase of an accepted target or a change in repository identity requires a fresh explicit decision, not silent retargeting.

The accepted contract must be anchored to the GitHub delivery issue and referenced by the stable WorkItem. The backend keeps accepted snapshot/evidence locally; UI/Firefox is not an authority for selecting the target.

### 3. Safe branch, PR and CI evidence

For release and hotfix operations, prove the origin SHA and lineage, create/reuse the exact intended release idempotently, branch the hotfix from its **current verified release tip**, and open its PR only toward that release branch. Never merge `main` wholesale into the release or merge the whole release into `main`.

A PR association requires WorkItem + accepted context + repository + head and **expected base name**. PR titles/branch conventions alone cannot override contradictory target/provenance. Detect retargeting even when two target branches happen to point to identical SHA.

Extend ADR-0016's expected-head/current-base revalidation to compare expected base **name and SHA**, accepted delivery context, current branch tip and current-head required checks before synchronization, merge and incident recovery. A release hotfix may synchronize only against its release branch. No zero-workflow-as-green, skipped required validations, bypass of review, forced merge or destructive push. Distinguish checks merely observed from checks required for that target. Incomplete GitHub paging/evidence must fail closed; no assumption that the first 100 PRs are exhaustive.

GitHub update-branch does not atomically lock the entire head/base pair; rechecks and protections limit but do not remove races. Preserve durable mutation-attempt fencing and invalidate old-head CI after branch update.

### 4. Separate proofs of completion and publication

A hotfix merge is **not** a completed forward-port, a published image or a deployment. Track independently: (1) merged code, (2) available versioned artifact, (3) validated version, (4) deployed version. Define the exact hotfix WorkItem completion policy in DC-075A and enforce it in DC-075B: if a testable image is required, green CI + merged PR alone cannot authorize DONE/reconciliation until publish/tag/digest evidence exists. Failed publication resumes publishing without recreating the hotfix or declaring success.

A deploy or SQL schema rollback is **never implicit**. Images must be associated with source SHA and immutable digest. Verify target branch protections and mandatory checks before *enabling automatic release finalization*; absence of branch protection/rulesets is an operational activation blocker, not a blocker for DC-075A's contract design. Protections on `main` do not imply protections on `release/*`.

### 5. Forward-port of the integrated fix only

The second WorkItem starts on current observed `main` and owns a second PR to `main`, separate current-head CI, merge and roadmap reconciliation. Transfer only the **actually integrated fix**, including release merge resolutions:

- squash: source is the integrated squash commit;
- rebase: identify the actual integrated commits, not pre-rebase head SHAs;
- merge commit: calculate the verified release-relative delta or an equivalent proven adaptation, including merge resolutions; never guess the mainline parent for cherry-pick;
- use `cherry-pick -x` when meaningful, but also persist structured provenance.

Idempotency identity is source delivery + exact target + forward-port WorkItem; an advancing `main` tip changes observed evidence but never creates another logical forward-port. After an ambiguous timeout, rediscover the exact branch/PR before attempting creation. Conflicts or partial presence require same-session DEV adaptation, justification and new CI. If the fix is already present, do not manufacture an empty commit or declare DONE automatically; hold for explicit equivalence evidence/decision and define an accepted terminal path.

### 6. Sessions, watchdogs, locks and authorizations

Every initial, CI-red, stale-DEV, orphan-branch, branch-sync/conflict and roadmap-reconcile prompt must carry accepted target context and instruct the DEV to reread and revalidate GitHub **before editing**. A prompt already ACKed by Firefox cannot be remotely revoked; do not solve a stale context by dispatching a second initial prompt. Watchdog branch activity for a hotfix is measured against its release, not default `main`. Keep stable sessions, idempotency, fail-stop `SEND_ARMED/AMBIGUOUS`, human ARCH authorization and explicit response import.

ResourceLocks apply to real conflict surfaces: serialize per-release publication, globally protect shared roadmap/contract/migrations where needed, and never infer disjoint safe changes solely from different branch names.

### 7. Consumer compatibility and release activation

DevCockpit cannot infer SQL Server backward/forward compatibility or prove a rollback from a prior Docker image. The consuming repository must provide evidence of migration requirements, supported schema versions, old-app-after-migration behavior, destructive/irreversible changes and tested backup/restore. Missing/incompatible evidence is a visible release blocker, never an implicit migration or rollback.

Align the consumer repository's own `AGENTS.md` (including RessourcePlanner) in a separate authorized delivery **before its first orchestrated hotfix**; do not change other repositories incidentally in a DevCockpit tranche.

## Delivery allocation and acceptance

- **DC-075A**: structured source/target and two-WorkItem contracts, validation and additive persistence, completion policy, all DEV/recovery prompts, ADR/DevCockpit `AGENTS.md`. Keep release automation disabled.
- **DC-075B**: release creation/reuse, hotfix PR association/finalizer/watchdogs by target, check/protection gates, versioned artifact publication and safe recovery.
- **DC-075C**: integrated delta provenance, idempotent forward-port, squash/rebase/merge/conflicts/already-present cases, independently reconciled PR to `main`.
- **DC-075D**: observable separate states in cockpit, consumer documentation and complete end-to-end acceptance.

Required regression scenarios: `main` advances without release contamination; two PRs cannot be misassociated; PR retarget is caught even with identical target SHAs; moved tag is rejected; changing base is revalidated; missing/partial CI cannot authorize merge; timeout/restart does not duplicate delivery; squash/conflict provenance survives; publish failure is not confused with merge; incompatible migration blocks; ARCH and ambiguous-send guarantees remain intact.

## Amendments and consequences

This ADR **extends** ADR-0005 (strict association additionally binds accepted target), ADR-0012 (release completion is policy/evidence specific; reconciliation never creates the second WorkItem), ADR-0013 (target-relative branch activity and recovery) and ADR-0016 (explicit expected base name and SHA). Their existing safety guarantees remain binding. ADR-0002, ADR-0007, ADR-0009, ADR-0010 and ADR-0014 continue to govern sessions, decisions, dependencies, locks and transport; no new general `Execution` table is accepted by this gate.

Only the **architecture gate** is accepted by this ADR. No release/hotfix/forward-port mutation capability is implemented or authorized by recording the decision. After roadmap reconciliation ASTRA-075 becomes `DONE`, only DC-075A becomes `READY`, and DC-075B/C/D remain `BLOCKED`.
