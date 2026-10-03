# Architecture decisions

DevCockpit uses Architecture Decision Records for structural decisions that are durable or costly to reverse.

Current decisions:

- [ADR-0001 — Authority boundaries and sources of truth](ADR-0001-authority-boundaries.md)
- [ADR-0002 — Orchestration model: WorkItem, Execution, Session and Dispatch](ADR-0002-orchestration-model.md)
- [ADR-0003 — Manual ChatGPT companion and WebSocket transport](ADR-0003-manual-chatgpt-companion.md)
- [ADR-0004 — Explicit schema migrations with Alembic](ADR-0004-explicit-schema-migrations.md)
- [ADR-0005 — GitHub execution projection and strict WorkItem identity](ADR-0005-github-execution-projection.md)
- [ADR-0006 — Explicit ChatGPT response import](ADR-0006-explicit-chatgpt-response-import.md)
- [ADR-0007 — Explicit handoffs and accepted decisions](ADR-0007-explicit-handoffs-and-accepted-decisions.md)
- [ADR-0008 — Explicit roadmap change proposals and GitHub writeback](ADR-0008-explicit-roadmap-change-proposals-and-github-writeback.md)
- [ADR-0009 — Canonical dependencies and deterministic scheduler](ADR-0009-canonical-dependencies-and-deterministic-scheduler.md)
- [ADR-0010 — ResourceLocks and deterministic conflict surfaces](ADR-0010-resource-locks-and-conflict-surfaces.md)
- [ADR-0011 — DEV GitHub handoff and explicit human authorization of architecture gates](ADR-0011-dev-handoff-and-human-architecture-gates.md)
- [ADR-0012 — Automatic DEV roadmap reconciliation](ADR-0012-automatic-dev-roadmap-reconciliation.md)
- [ADR-0013 — Stale DEV watchdog and bounded ChatGPT retry](ADR-0013-stale-dev-watchdog-and-bounded-retry.md)
- [ADR-0014 — Automatic ChatGPT routing, durable ConversationBinding, and fail-stop send idempotence](ADR-0014-automatic-chatgpt-routing-and-send-idempotence.md)

## Rule

Do not create an ADR for every implementation detail.

Create or update one when changing a durable boundary such as:

- source of truth / authority;
- orchestration state semantics;
- persistence;
- WebSocket protocol;
- GitHub event ingestion;
- concurrency and locking;
- response import / decision semantics;
- authentication or authorization.

The current work order belongs in GitHub issue #1, not in an ADR.
