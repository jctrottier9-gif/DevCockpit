# DevCockpit

DevCockpit is a local development-orchestration cockpit designed to reduce the manual coordination needed between a Product Owner, an Architect, one or more Developers, GitHub and ChatGPT.

The first goal is not full autonomy. DevCockpit prepares the right next prompt, routes it to a Firefox extension, observes GitHub/CI evidence, and proposes the next action while the user remains the explicit gate for sending prompts to ChatGPT.

## Product principles

- GitHub and the canonical roadmap are the source of truth for delivery state.
- ChatGPT is a work surface, not the source of truth.
- Prompt sending is user-triggered.
- The Firefox extension is a thin companion, not an orchestration engine.
- CI/PR/merge evidence is derived from GitHub.
- Orchestration is deterministic whenever possible.
- Architecture and product decisions are explicit.
- Parallel development comes only after the single-execution loop is reliable.

## Current application foundation

DC-001 establishes the executable application shell and DC-010 adds the first persistent domain concept:

~~~
frontend/                  React + TypeScript + Vite
        ↓ /api
app/main.py                FastAPI composition and HTTP boundary
        ↓
app/application/           PromptDispatch creation / idempotence
app/domain/                PromptDispatch invariants and transitions
        ↓
app/infrastructure/        SQLite / SQLAlchemy repositories + Alembic
~~~

`PromptDispatch` is transport-independent. DC-010 does not add WebSocket, Firefox-extension, GitHub, ChatGPT or OpenAI integration.

## Prerequisites

CI uses Python 3.12.7 and Node 22.20.0. Local development should use compatible Python 3.12+ and Node 22 versions.

## Backend setup

Create and activate a virtual environment, then install the backend and test dependencies:

~~~bash
python -m venv .venv
~~~

On Linux/macOS:

~~~bash
source .venv/bin/activate
~~~

On PowerShell:

~~~powershell
.\.venv\Scripts\Activate.ps1
~~~

Install dependencies:

~~~bash
python -m pip install -e ".[dev]"
~~~

Copy the local configuration template if you want to override defaults:

~~~bash
cp .env.example .env
~~~

The default SQLite database is `./devcockpit.db` and is ignored by Git.

Apply explicit schema migrations and verify local persistence:

~~~bash
python -m app.infrastructure.database
~~~

Equivalent migration-only command:

~~~bash
alembic upgrade head
~~~

Alembic migrations are authoritative for business-schema evolution. `Base.metadata.create_all()` is not used as a migration mechanism.

Start FastAPI:

~~~bash
uvicorn app.main:app --reload
~~~

Health endpoint:

~~~
GET http://127.0.0.1:8000/api/health
~~~

Run backend validation:

~~~bash
python -m compileall -q app tests
pytest -q
~~~

## Frontend setup

Install frontend dependencies:

~~~bash
cd frontend
npm install --no-audit --no-fund
~~~

Start Vite:

~~~bash
npm run dev
~~~

The development server proxies `/api` to FastAPI at `http://127.0.0.1:8000`.

Build the production frontend:

~~~bash
npm run build
~~~

## Configuration

Backend settings are centralized in `app/config.py` and use the `DEVCOCKPIT_` environment prefix. `.env.example` documents the supported local values and no secret is required for DC-010.

The current settings are:

~~~
DEVCOCKPIT_APP_NAME
DEVCOCKPIT_ENVIRONMENT
DEVCOCKPIT_DATABASE_URL
~~~

## PromptDispatch model

DC-010 persists one prompt prepared for one logical AgentSession. The session convention is:

~~~
<project>:<role>:<work-item>
~~~

The DC-010-only state machine is intentionally small:

~~~
PREPARED → CANCELLED
~~~

Transport states, WebSocket acknowledgements, reconnect/replay delivery and Firefox behavior belong to DC-011 and later slices.

Creation uses an explicit idempotency key. Replaying the same logical creation returns the existing dispatch; reusing a key for different logical prompt content is rejected.

## Tests and external integrations

The test suite is offline and deterministic. It validates application creation, health, domain invariants/transitions, PromptDispatch persistence/idempotence/rollback, and migration from an empty SQLite database without GitHub, ChatGPT, OpenAI, WebSocket, scheduler, or browser-extension dependencies.

## Target loop

~~~
Roadmap / WorkItem
        ↓
DevCockpit derives next action
        ↓
PromptDispatch
        ↓
WebSocket
        ↓
Firefox extension
        ↓
user clicks Send
        ↓
ChatGPT role session
        ↓
Developer pushes PR
        ↓
GitHub / CI
        ↓
DevCockpit observes result
        ↓
next PromptDispatch
~~~

## Initial roles

### Product Owner
Clarifies product intent, acceptance criteria and minimal redécoupage.

### Architect
Performs read-only architecture analysis and stabilizes durable decisions.

### Developer
Implements an approved slice, validates it and resolves normal CI failures.

### DevCockpit
Owns deterministic orchestration, routing, dependencies, evidence and prompt preparation.

## Firefox extension contract

The initial wire payload remains deliberately small:

~~~json
{
  "session": "RessourcePlanner:DEV:502A",
  "text": "Le prompt complet à envoyer à ChatGPT"
}
~~~

DevCockpit may keep richer metadata internally, but the transport contract should not grow accidentally.

## Roadmap

The canonical roadmap lives in GitHub issue #1 and contains a machine-readable `COCKPIT_PIPELINE_V1` block. There is intentionally no canonical `ROADMAP.md`.

## Architecture decisions

Durable decisions live under `docs/architecture/`, including authority boundaries, orchestration identity, the manual ChatGPT companion boundary, and explicit Alembic schema migrations.

## Development workflow

Read `AGENTS.md` before implementing any roadmap slice.

Normal delivery is:

~~~
current main
→ issue/roadmap
→ implement
→ targeted tests
→ broader validation
→ PR
→ CI
→ fix normal failures
→ merge
→ reconcile roadmap
~~~

Do not infer a completed roadmap step from a ChatGPT message alone.
