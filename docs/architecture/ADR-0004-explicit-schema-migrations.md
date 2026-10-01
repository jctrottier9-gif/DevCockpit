# ADR-0004 — Explicit schema migrations with Alembic

- Status: Accepted
- Date: 2026-10-01

## Context

DC-001 provided SQLAlchemy metadata and `Base.metadata.create_all()` only while no business tables existed. DC-010 introduces the first persistent business concept, `PromptDispatch`, so schema evolution now needs a reproducible authority independent from runtime model discovery.

## Decision

- Alembic is the explicit schema-migration mechanism for the SQLAlchemy persistence layer.
- Business-schema changes are delivered as ordered migration revisions under `migrations/`.
- A new database is initialized by applying migrations through `alembic upgrade head` or the repository bootstrap wrapper.
- SQLAlchemy declarative models remain the runtime persistence mapping, but `Base.metadata.create_all()` is not an authority for business-schema evolution.
- Migration tests use SQLite, matching the initial persistence target.

## Consequences

- DC-010 can create `prompt_dispatches` reproducibly from an empty database.
- Future slices must add migrations for persistent schema changes rather than mutating schema implicitly at application startup.
- Model/migration drift must be caught by tests and code review.
- No production database change beyond SQLite is implied by this decision.
