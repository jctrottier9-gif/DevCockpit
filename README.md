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

DC-001 establishes the executable application shell, DC-010 adds persistent PromptDispatch, and DC-011 adds reliable WebSocket transport:

~~~text
frontend/                  React + TypeScript + Vite
        ↓ /api
app/main.py                FastAPI HTTP/WebSocket boundary
        ↓
app/application/           Prompt creation + delivery/ACK use cases
app/domain/                PromptDispatch + PromptDelivery invariants
        ↓
app/infrastructure/        SQLite / SQLAlchemy / WebSocket protocol
~~~

PromptDispatch remains transport-independent. PromptDelivery is separate persisted transport truth and never means that a prompt was sent to ChatGPT.

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

The default SQLite database is ./devcockpit.db and is ignored by Git.

Apply explicit schema migrations and verify local persistence:

~~~bash
python -m app.infrastructure.database
~~~

Equivalent migration-only command:

~~~bash
alembic upgrade head
~~~

Alembic migrations are authoritative for business-schema evolution. Base.metadata.create_all() is not used as a migration mechanism.

Start FastAPI:

~~~bash
uvicorn app.main:app --reload
~~~

Health endpoint:

~~~text
GET http://127.0.0.1:8000/api/health
~~~

Run backend validation:

~~~bash
python -m compileall -q app tests
pytest -q
~~~

## WebSocket companion transport

DC-011 exposes the local companion endpoint:

~~~text
ws://127.0.0.1:8000/api/companion/ws
~~~

The current policy is one active companion. A second simultaneous connection is rejected deterministically. The endpoint is intentionally unauthenticated for the current trusted local-network development model and must not be exposed to an untrusted network without adding authentication.

The functional prompt payload remains exactly:

~~~json
{
  "session": "DevCockpit:DEV:DC-011",
  "text": "Le prompt complet à envoyer à ChatGPT"
}
~~~

For reliable ACK/replay it is carried inside protocol version 1:

~~~json
{
  "version": 1,
  "type": "prompt",
  "delivery_id": "stable-uuid",
  "payload": {
    "session": "DevCockpit:DEV:DC-011",
    "text": "Le prompt complet à envoyer à ChatGPT"
  }
}
~~~

The extension acknowledges receipt with:

~~~json
{
  "version": 1,
  "type": "ack",
  "delivery_id": "stable-uuid"
}
~~~

An ACK means only received by the extension. It does not mean sent to ChatGPT, response received, work started, work completed or WorkItem DONE.

If a connection drops before ACK, the same logical delivery and the same delivery_id are replayed after reconnect. SQLite stores one PromptDelivery per PromptDispatch; reconnects increment transport attempt metadata but never create a new PromptDispatch.

A PromptDispatch CANCELLED is not sent as new work and is not replayed. No remote-revocation protocol is invented for a message that may already have reached the extension.

Control messages are typed. Version 1 currently supports ack and ping inbound, with pong and explicit error responses. chatgpt_response is intentionally not implemented until DC-030.

The transport bounds inbound messages to 64 KiB and does not log full prompt bodies or credentials.

To exercise the complete server loop without Firefox, run:

~~~bash
pytest -q tests/test_websocket_transport.py
~~~

Those tests use FastAPI/Starlette's WebSocket test client and require no browser, ChatGPT, GitHub, OpenAI or external network.

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

The development server proxies /api to FastAPI at http://127.0.0.1:8000.

Build the production frontend:

~~~bash
npm run build
~~~

## Configuration

Backend settings are centralized in app/config.py and use the DEVCOCKPIT_ environment prefix. .env.example documents the supported local values.

The current settings are:

~~~text
DEVCOCKPIT_APP_NAME
DEVCOCKPIT_ENVIRONMENT
DEVCOCKPIT_DATABASE_URL
~~~

## PromptDispatch and PromptDelivery

PromptDispatch persists one prompt prepared for one logical AgentSession. The session convention is:

~~~text
<project>:<role>:<work-item>
~~~

The domain state machine remains intentionally small:

~~~text
PREPARED → CANCELLED
~~~

Transport status is not added to that state machine. PromptDelivery owns the separate transport states:

~~~text
PENDING → ACKNOWLEDGED
~~~

Creation of a PromptDispatch uses an explicit idempotency key. Replaying the same logical creation returns the existing dispatch; reusing a key for different logical prompt content is rejected.

PromptDelivery has a stable UUID and a database uniqueness constraint on dispatch_id, so a reconnect/retry reuses one logical delivery. Prompt text, project, role and WorkItem are not duplicated into the delivery table.

## Tests and external integrations

The test suite is offline and deterministic. It validates application creation, health, domain invariants/transitions, PromptDispatch persistence/idempotence, PromptDelivery migration/constraints/replay/ACK semantics, WebSocket protocol behavior, rollback, and reconnect from an empty or upgraded SQLite database without Firefox, ChatGPT, OpenAI, GitHub or external network dependencies.

## Target loop

~~~text
Roadmap / WorkItem
        ↓
DevCockpit derives next action
        ↓
PromptDispatch
        ↓
PromptDelivery + WebSocket
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

## Roadmap

The canonical roadmap lives in GitHub issue #1 and contains a machine-readable COCKPIT_PIPELINE_V1 block. There is intentionally no canonical ROADMAP.md.

## Architecture decisions

Durable decisions live under docs/architecture/, including authority boundaries, orchestration identity, the manual ChatGPT companion/WebSocket protocol, and explicit Alembic schema migrations.

## Development workflow

Read AGENTS.md before implementing any roadmap slice.

Normal delivery is:

~~~text
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
