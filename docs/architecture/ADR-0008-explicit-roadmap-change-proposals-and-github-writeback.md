# ADR-0008 — Explicit roadmap change proposals and GitHub writeback

- Status: Accepted
- Date: 2026-10-01
- Gate: ASTRA-041

## Context

DC-040 delivered explicit Architect Handoffs and explicitly accepted Decisions without allowing imported ChatGPT prose to become product state or GitHub authority.

The Product Owner phase needs two additional capabilities that must remain distinct:

1. ask and accept a product clarification;
2. propose, preview, confirm and eventually apply an exact mutation of the canonical roadmap.

A Decision is not a GitHub mutation. A roadmap proposal is not canonical state. GitHub issue #1 remains authoritative until an explicitly confirmed proposal revision is actually applied and reconciled.

This ADR records the architecture accepted by ASTRA-041. The contracts below are targets for DC-041A and DC-041B; they are not claims about functionality already implemented by DC-040.

## Decision

### Reuse Handoff and Decision for Product Owner consultation

Product clarification extends the existing Handoff/Decision model instead of creating a parallel PO workflow.

The target Handoff roles are:

~~~text
ARCH
PO
~~~

The target purposes are:

~~~text
TECHNICAL_GUIDANCE
PRODUCT_CLARIFICATION
ROADMAP_REVIEW
~~~

RESCOPE is deliberately not a Handoff purpose. A scope change is a possible consequence of a consultation, not the question being asked.

The target Decision types are:

~~~text
ARCHITECTURE_GUIDANCE
PRODUCT_CLARIFICATION
SCOPE_DECISION
~~~

Decision effects remain:

~~~text
CONTINUE_IN_SCOPE
HOLD_FOR_AUTHORIZATION
~~~

Role/purpose/decision-type combinations must be validated explicitly by the backend. React does not invent valid combinations.

### Sequential ARCH to PO transfer

The invariant remains a maximum of one active blocking Handoff per Project/WorkItem. ARCH and PO Handoffs are not stacked concurrently.

The target lifecycle adds:

~~~text
TRANSFERRED
RESOLVED_NO_RESUME
~~~

A transferred Handoff records enough provenance to preserve the chain, including conceptually:

~~~text
predecessor_handoff_id
context_decision_id?
~~~

An ARCH to PO transfer is an explicit atomic command. It may originate from:

- an OPEN ARCH Handoff when a specific returned response is explicitly chosen as transfer context; or
- a DECIDED ARCH Handoff whose accepted Decision has effect HOLD_FOR_AUTHORIZATION.

A HOLD never creates a PO Handoff automatically.

### Deterministic resume routing

Resume routing is backend-owned and derived from provenance:

~~~text
DEV -> PO
=> resume DEV
~~~

~~~text
ARCH -> PO
=> create a linked ARCH consultation
=> reuse the same ARCH AgentSession for the WorkItem
~~~

~~~text
accepted rescope replaces the WorkItem
=> do not resume the superseded WorkItem
~~~

React never chooses an arbitrary resume_role.

A role session keeps the existing convention <project>:<role>:<work-item>. A new WorkItem key created by an accepted split gets new role sessions. Repeated consultation of the same role and WorkItem key reuses its existing logical session.

### Decision is not RoadmapChangeProposal

Decision and RoadmapChangeProposal have different authority.

A Decision is an explicitly accepted product conclusion. Examples such as “keep the current scope” or “clarify this criterion” may end with a Decision only.

A RoadmapChangeProposal is the exact, non-canonical remote mutation proposed for the configured roadmap issue.

For example:

~~~text
replace DC-X by DC-XA / DC-XB
=> SCOPE_DECISION / HOLD_FOR_AUTHORIZATION
=> explicit RoadmapChangeProposal creation command
~~~

Neither importing a ChatGPT response nor accepting a Decision automatically creates a RoadmapChangeProposal.

### Stable proposal identity and immutable revisions

A proposal has one stable proposal_id and immutable numbered revisions.

Each revision preserves conceptually:

~~~text
proposal_id
revision

project_id
repository_full_name
roadmap_issue_number

source_decision_id

base_body
base_body_hash
base_updated_at

proposed_body
proposed_body_hash

structured_operations

generator_version
validation_version

created_by
created_at
command_identity
~~~

Editing a proposal creates revision + 1. An already presented revision is never silently replaced.

### SUPERSEDED WorkItems

A WorkItem replaced by an accepted split or rescope must never be marked DONE merely to advance the roadmap.

The canonical future status is:

~~~text
SUPERSEDED
~~~

It means the historical WorkItem was replaced and was not itself delivered.

### COCKPIT_PIPELINE_V2

The accepted future canonical format is:

~~~text
<!-- COCKPIT_PIPELINE_V2 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES
...
<!-- /COCKPIT_PIPELINE_V2 -->
~~~

REPLACES is either "-" or one existing canonical WorkItem key.

Validation rules include:

- the referenced key exists;
- a replaced target is SUPERSEDED;
- no self-reference;
- no replacement cycles;
- every SUPERSEDED WorkItem has at least one replacement;
- the historical superseded row remains in the roadmap;
- REPLACES is a replacement relation, not a Dependency.

V1 remains explicitly readable and V2 becomes explicitly readable. Unknown versions, multiple canonical blocks, and a document containing both V1 and V2 are rejected.

The canonical roadmap must not be converted to V2 until DC-041A has actually delivered the V1/V2 reader and validators. Until then issue #1 remains COCKPIT_PIPELINE_V1, DC-041 remains a historical BLOCKED line, and its future conversion to SUPERSEDED is deferred to DC-041A reconciliation.

### Backend-owned deterministic preview

Preview generation is backend-owned and deterministic. A preview identifies at minimum:

~~~text
target
proposal_id / revision
digest

body before
body after

human prose diff
pipeline diff

WorkItems added
WorkItems modified
WorkItems replaced

order
statuses
previous READY
new READY

REPLACES relations
issue mappings

blocking diagnostics
allowed actions
~~~

A syntactically valid parser result is not sufficient to authorize a transition. A separate transition validator rejects, at minimum:

- silent WorkItem-key reuse;
- deletion of historical rows;
- false DONE;
- hidden READY promotion;
- skipped architecture/product gates;
- incoherent replacement chains.

### Separate Decision acceptance and proposal confirmation

Two distinct human authorities exist:

1. accept a PO Decision;
2. confirm an exact RoadmapChangeProposal revision.

Confirmation names the exact immutable revision:

~~~text
proposal_id
revision
preview_digest
expected_proposal_version
confirmation_command_id
confirmed_by
~~~

The backend recalculates the preview digest.

Before confirmation the backend rereads GitHub. If the roadmap body has changed from the proposal base, confirmation is refused and a new proposal revision plus new preview is required.

### Separate targeted GitHub writer

The existing roadmap reader remains read-only.

DC-041B introduces a separate targeted RoadmapWriter port. Its only authorized roadmap mutation is the body of the configured roadmap issue.

The writer must not incidentally mutate title, labels, issue state or milestone.

Automatic creation of replacement GitHub issues is outside MVP-3. Any issue mappings required by a split must already exist before application; DC-041A may validate those mappings.

### GitHub concurrency guarantee and residual risk

The MVP concurrency protocol is explicit:

~~~text
full-body hash
+ reread before confirmation
+ reread immediately before PATCH
+ strict comparison
+ local serialization
+ reread after PATCH
~~~

This is not an atomic compare-and-swap. A residual race remains between the final GET and PATCH during which an external editor may change GitHub.

That residual race is an unresolved product acceptance decision, not an architecture assumption.

DC-041B must not enable direct writeback until an explicit product decision accepts this residual concurrency window for MVP-3.

If the risk is not accepted:

~~~text
proposal + preview
=> available

direct PATCH
=> blocked
~~~

until a stronger authority mechanism is adopted.

### Application state and reconciliation

SQLite and GitHub do not share one transaction.

The target proposal states are:

~~~text
DRAFT
CONFIRMED
APPLIED
CANCELLED
~~~

The target application states are:

~~~text
PREPARED
APPLYING
APPLIED
NOT_APPLIED
CONFLICT
RECONCILIATION_REQUIRED
~~~

An APPLYING record found after restart is never blindly retried.

Reconciliation follows observed remote state:

~~~text
remote == expected
=> APPLIED
~~~

~~~text
remote != base && remote != expected
=> CONFLICT
~~~

~~~text
GitHub unavailable
=> RECONCILIATION_REQUIRED
~~~

~~~text
adapter proves PATCH was never emitted
=> NOT_APPLIED
~~~

~~~text
remote == base
but PATCH may have been emitted
=> still ambiguous
=> no automatic retry
~~~

All confirmation and application commands require stable idempotency identities. Replays with incompatible immutable content fail explicitly.

### Poller fencing

While a roadmap application is active or uncertain, automatic work preparation for the affected target is inhibited.

The implementation uses a local generation/fence so that a projection calculated before writeback cannot create a PromptDispatch after writeback.

ExecutionProjection itself remains GitHub/roadmap-derived and is not extended with proposal/application states.

### DC-041 split and delivery sequence

The accepted sequence is:

~~~text
ASTRA-041
  ↓
DC-041A — Boucle PO, propositions révisées et preview
  ↓
DC-041B — Application GitHub et réconciliation
  ↓
DC-050
~~~

DC-041 remains the functional parent and is not marked DONE as a substitute for its children.

DC-041A owns:

- PO Handoff/Decision support;
- explicit ARCH to PO transfer;
- deterministic resume;
- V1/V2 parsing;
- SUPERSEDED and REPLACES validation;
- RoadmapChangeProposal revisions;
- preview and transition validation;
- local UI;
- no GitHub write.

After DC-041A is merged and reconciled, the canonical roadmap may move to V2, DC-041 becomes SUPERSEDED, DC-041A becomes DONE and DC-041B becomes READY.

DC-041B owns:

- exact-revision confirmation;
- application/attempt persistence;
- targeted RoadmapWriter;
- stale detection and local serialization;
- issue-body PATCH;
- restart-safe reconciliation;
- idempotence;
- poller fencing;
- post-split resolution;
- no automatic issue creation.

Direct writeback in DC-041B is additionally gated by the explicit product acceptance of the residual GitHub concurrency risk described above.

## Consequences

- Product clarification reuses the established orchestration model rather than creating a second authority path.
- Accepted product conclusions remain distinct from non-canonical proposal drafts.
- Historical WorkItems are preserved instead of being misrepresented as delivered.
- The V1 to V2 transition is backward-readable and deliberately staged.
- Preview and confirmation are tied to immutable proposal revisions and stale GitHub state fails closed.
- GitHub writeback is isolated behind a narrow writer and never hidden behind the read adapter.
- Restart after an uncertain PATCH favors reconciliation over automatic retry.
- The poller cannot prepare work from a projection invalidated by an in-flight roadmap mutation.
- MVP-3 is not considered complete until both DC-041A and DC-041B are delivered, the roadmap is reconciled, and any required product decision enabling direct writeback has been explicitly recorded.
