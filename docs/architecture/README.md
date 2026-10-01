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
