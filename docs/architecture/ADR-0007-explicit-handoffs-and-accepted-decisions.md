# ADR-0007 — Explicit handoffs and accepted decisions

- Status: Accepted
- Date: 2026-10-01
- Gate: ASTRA-040

## Context

DC-030 closes the explicit ChatGPT response-return loop. DevCockpit can now correlate one user-selected ChatGPT response to the PromptDelivery and PromptDispatch that caused it.

The next product capability is a controlled DEV ↔ Architect handoff. Imported ChatGPT prose must not become an orchestration command or an accepted decision merely because it contains words such as "architecture", "blocked" or "decision".

This ADR stabilizes only the contracts required for DC-040. Product Owner redécoupage and GitHub roadmap writeback remain separate decisions for DC-041.

## Decision

### Human-confirmed handoff

A Handoff is a persistent, explicitly confirmed request addressed to another role for one Project/WorkItem.

DC-040 uses only target role `ARCH` and purpose `TECHNICAL_GUIDANCE`.

A ChatGPT response may provide context, but importing that response never creates a Handoff automatically.

The user explicitly creates the Handoff and confirms its question/context.

### Handoff identity and provenance

A Handoff has a stable UUID and is tied to:

- `project_id`;
- `work_item_id`;
- the source DEV PromptDispatch;
- an optional source ImportedChatGptResponse;
- target role;
- purpose;
- confirmed question/context;
- one Architect request PromptDispatch;
- optional DEV resume PromptDispatch;
- stable creation-command identity;
- lifecycle state, timestamps and optimistic version.

Project and WorkItem are stable logical references, not local foreign keys. Source role/session are derived from the source dispatch.

For DC-040 there may be at most one active blocking Handoff per Project/WorkItem.

### Sessions

Existing logical session identity remains:

```text
<project>:<role>:<work-item>
```

A consultation for `DC-040` therefore uses:

```text
DevCockpit:DEV:DC-040
DevCockpit:ARCH:DC-040
```

Successive Architect consultations for the same WorkItem reuse the same ARCH AgentSession. Handoff identity, not conversation position, isolates individual consultations.

After an accepted Architect decision, the resume prompt returns to the original DEV AgentSession.

No AgentSession table is introduced.

### Handoff lifecycle

DC-040 persists this minimal state machine:

```text
OPEN
  ├─> DECIDED
  │     └─> RESUME_PREPARED
  └─> CANCELLED

DECIDED ─> CANCELLED
```

Creation and Architect prompt preparation occur atomically.

Importing a ChatGPT response does not transition the Handoff.

User-visible indications such as "waiting for response" or "response to review" are derived from Handoff + ImportedChatGptResponse data; they are not new authoritative states.

### Imported response is still not a decision

`ImportedChatGptResponse` remains historical evidence only.

A Decision is created only when the user explicitly selects a response and accepts a specific conclusion.

The backend must verify that:

- the Handoff is active and compatible with acceptance;
- the selected response originates from the Architect request dispatch for that Handoff;
- Project, WorkItem, role and session provenance all agree;
- no competing acceptance has already won.

Several imported responses may exist for the same Architect prompt. None is selected automatically.

### Decision

DC-040 persists an immutable Decision with:

- stable `decision_id`;
- unique `source_handoff_id`;
- selected `source_response_id`;
- `decision_type = ARCHITECTURE_GUIDANCE`;
- accepted summary/conclusion;
- effect:
  - `CONTINUE_IN_SCOPE`; or
  - `HOLD_FOR_AUTHORIZATION`;
- `accepted_by`, `accepted_at`;
- stable acceptance-command identity.

A Decision is local orchestration authority only within its declared effect. It does not edit ADRs, GitHub, the canonical roadmap or delivery evidence.

If the accepted conclusion requires a prior ADR/scope/roadmap authorization, the effect is `HOLD_FOR_AUTHORIZATION`; DevCockpit retains the Decision but does not prepare a DEV resume that would bypass the missing authorization.

Accepted Decisions are immutable in DC-040. Decision supersession/revision is deferred.

### Atomic preparation

The existing prompt-dispatch creation primitive must be refactored so it can participate in a caller-owned Unit of Work without an internal commit.

The existing public use case may remain as a transaction-owning wrapper.

Required atomic operations are:

1. Handoff creation + Architect PromptDispatch + links;
2. Decision acceptance + Handoff transition + DEV resume PromptDispatch when the effect permits immediate continuation;
3. Handoff cancellation + handling of local dispatches that must no longer be newly delivered.

A failure in any part of one operation rolls the whole operation back.

### Prompt inhibition during a blocking Handoff

`ExecutionProjection` remains derived exclusively from roadmap + GitHub and is not extended with `NEEDS_ARCHITECT` or `NEEDS_PO`.

While a Handoff is `OPEN` or `DECIDED` without an authorized resume:

- GitHub observation continues;
- ExecutionProjection remains visible;
- automatic DEV `INITIAL` and `CI_RED` PromptDispatch creation for that WorkItem is inhibited;
- backend delivery must not newly deliver locally superseded/inappropriate DEV prompts;
- GitHub evidence remains observable and is not rewritten as Handoff state.

The eligibility check and PromptDispatch creation must share the relevant transaction boundary so a poller cannot race a newly created Handoff.

A prompt already durably accepted into Firefox cannot be remotely revoked by the current protocol. DC-040 must represent this limitation honestly; remote send validation/revocation is deferred.

### Resume after Architect decision

For an accepted `CONTINUE_IN_SCOPE` Decision, DevCockpit prepares one idempotent DEV resume prompt in the original DEV AgentSession.

The prompt includes:

- Handoff identity/question;
- accepted Decision identity and conclusion;
- relevant constraints;
- current WorkItem/scope;
- useful current GitHub evidence;
- instruction to continue the same WorkItem and not start the next tranche.

If the canonical roadmap no longer authorizes that WorkItem, the Decision remains recorded but resume preparation is held.

A resume records the GitHub failure-cycle identity it intentionally covers when relevant so the execution evaluator does not immediately create a duplicate CI_RED prompt for the same already-consumed evidence.

### Idempotence

Stable command identities are required:

- Handoff creation: `creation_command_id`;
- Architect request dispatch: logical key `handoff:<id>:REQUEST:ARCH:v1`;
- Decision acceptance: `acceptance_command_id`;
- DEV resume dispatch: logical key `decision:<id>:RESUME:DEV:v1`.

Repeated identical commands return the same logical result. Reusing an identity with incompatible immutable content is an explicit conflict.

Template-version changes do not create a second dispatch for a Handoff/Decision already served.

### Persistence

DC-040 adds two tables via an additive Alembic migration after `0003_imported_chatgpt_response`:

#### handoffs

Stores identity, Project/WorkItem references, source dispatch/response provenance, target role/purpose, confirmed question/context snapshot, request/resume dispatch links, status, command identity, attribution/timestamps, optimistic version and resume-covered GitHub evidence where applicable.

#### decisions

Stores identity, unique source Handoff, selected source response, decision type, accepted conclusion/effect, attribution/timestamp and acceptance-command identity.

Constraints include:

- `ON DELETE RESTRICT` foreign keys to historical dispatch/response/Handoff records;
- no local FK to Project or WorkItem;
- unique request/resume dispatch relationships as applicable;
- unique Decision per Handoff in DC-040;
- uniqueness enforcing one active blocking Handoff per Project/WorkItem;
- check constraints for states/effects/non-empty required text;
- indexes for Project/WorkItem/status;
- optimistic concurrency through Handoff version.

Relational provenance that cannot be expressed by simple foreign keys is validated transactionally and covered by negative tests.

### Audit

No generic ExternalEvent table is introduced by DC-040.

The domain records themselves provide the required audit trail:

- Handoff creation;
- frozen Architect request PromptDispatch;
- ImportedChatGptResponse;
- accepted Decision;
- frozen DEV resume PromptDispatch;
- cancellation actor/date/reason.

### Role boundaries

- DEV handles implementation and ordinary CI correction.
- ARCH handles durable or cross-cutting technical guidance and contradictions between architecture decisions.
- PO remains responsible for product intent, acceptance criteria, scope, work breakdown and roadmap priority.

DC-040 does not route canonical `ARCHITECTURE_GATE` WorkItems generally and does not remove the existing `ROLE_ROUTING_NOT_AVAILABLE` guard for such roadmap items.

### Firefox companion

The companion remains a thin transport/UI bridge.

It does not:

- decide that an Architect is needed;
- create Handoffs;
- accept Decisions;
- choose orchestration roles;
- mutate roadmap/GitHub state.

Its existing explicit prompt send and explicit response return behavior is reused.

## Consequences

- A blocking Architect consultation is locally authoritative without contaminating GitHub-derived ExecutionProjection.
- ChatGPT prose remains non-authoritative until the user explicitly creates/accepts the corresponding orchestration records.
- The Architect and DEV can preserve conversation context using stable role/WorkItem sessions.
- Handoff/Decision records provide enough durable state for DC-060 attention signals later.
- The future scheduler may combine canonical WorkItem eligibility with local orchestration waits without treating a Handoff as a WorkItem dependency.
- Product Owner clarification, rescoping proposals and GitHub roadmap writeback remain outside DC-040.
