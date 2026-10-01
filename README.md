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

DC-001 establishes the executable application shell, DC-010 adds persistent PromptDispatch, DC-011 adds reliable WebSocket transport, DC-012 adds the first usable Firefox companion, and DC-020 adds configured GitHub projects plus strict canonical-roadmap projection:

~~~text
frontend/                  React + TypeScript + Vite
        ↓ /api
app/main.py                FastAPI HTTP/WebSocket boundary
        ↓
app/application/           Prompt use cases + Project/roadmap projection
app/domain/                Prompt models + Project + WorkItem/parser rules
        ↓
app/infrastructure/        SQLite / GitHub read adapter / WebSocket protocol

extension/                 Firefox queue / explicit ChatGPT send
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

## GitHub projects and canonical roadmap

DC-020 introduces an explicit, versioned local Project configuration in \`projects.json\`. Project is configuration rather than a database table in this slice: GitHub remains authoritative for roadmap state, and no persistent Project UI/workflow is required yet.

The committed local default is:

~~~json
{
  "projects": [
    {
      "project_id": "DevCockpit",
      "repository_full_name": "jctrottier9-gif/DevCockpit",
      "roadmap_issue_number": 1
    }
  ]
}
~~~

\`project_id\` is the stable logical identity. It is not derived from an issue title, branch, PR, CI run or commit SHA. More projects can be added by adding entries with the same three explicit fields.

The backend alone reads GitHub. For public repositories, a token is optional. For private repositories or higher API limits, set \`DEVCOCKPIT_GITHUB_TOKEN\` in the local environment. Never put that token in React, the Firefox extension, \`projects.json\`, logs or committed files.

The roadmap reader fetches only the configured issue and parses exactly one block delimited by:

~~~text
<!-- COCKPIT_PIPELINE_V1 -->
...
<!-- /COCKPIT_PIPELINE_V1 -->
~~~

Its canonical header is exactly:

~~~text
KEY | TYPE | STATUS | PARENT | LANE | TITLE
~~~

The parser is fail-closed. Missing/duplicate markers, invalid headers, malformed rows, duplicate keys, unknown TYPE/STATUS values, invalid parents and ambiguous MAIN READY state produce deterministic diagnostics and no active READY item. Human prose around the block is never used as a fallback to infer work.

The minimal backend surface is:

~~~text
GET /api/projects
GET /api/projects/{project_id}/roadmap
~~~

The roadmap response distinguishes three situations explicitly:

- GitHub source unavailable/unauthorized/not found: upstream error, no pipeline projection;
- GitHub issue read successfully but pipeline invalid: \`pipeline.valid = false\` with diagnostics;
- valid pipeline with no READY WorkItem: valid projection with \`active_ready_item = null\`.

DC-020 is strictly read-only toward GitHub. It does not update roadmap issues, infer PR/CI execution state, reconcile DONE/READY automatically or create PromptDispatch records.

With the local server running, a real read-only smoke against the configured DevCockpit roadmap is:

~~~bash
curl http://127.0.0.1:8000/api/projects/DevCockpit/roadmap
~~~

For the current roadmap before DC-020 is merged, the expected active canonical item is \`DC-020\`. This is smoke-test evidence only and is not hardcoded into product logic.

## Execution projection and CI follow-up

DC-021 adds a derived `ExecutionProjection` without creating a second execution authority. `WorkItem.status` remains the canonical `READY / BLOCKED / DONE` value from `COCKPIT_PIPELINE_V1`; `DEVELOPING`, `PR_OPEN`, `CI_RUNNING`, `CI_RED`, `READY_TO_MERGE`, `MERGED` and `ROADMAP_UPDATE_REQUIRED` are recalculated from read-only GitHub evidence.

The backend uses strong WorkItem identity only:

- WorkItem key at the beginning of the PR title with an explicit boundary;
- a branch path segment prefixed by the WorkItem key with an explicit branch boundary;
- an exact structured PR-body line such as `Work-Item: DC-021`.

An arbitrary body mention is never sufficient. Multiple open strongly-associated PRs fail closed instead of selecting one silently.

CI is evaluated only for the current PR head SHA. Current failure conclusions take precedence over running validations; otherwise queued/in-progress validations yield `CI_RUNNING`. Green requires at least one observed current workflow, no current red/running workflow, and only completed `success / neutral / skipped` conclusions. Zero observed workflows is not green.

The state priority after source/pipeline validation is:

~~~text
merged + green while roadmap remains READY -> ROADMAP_UPDATE_REQUIRED
open PR + red CI                         -> CI_RED
open PR + running CI                     -> CI_RUNNING
open PR + green CI + mergeable           -> READY_TO_MERGE
open PR without verdict                  -> PR_OPEN
strong branch ahead of default branch    -> DEVELOPING
no strong GitHub evidence                -> READY
~~~

The execution surface is:

~~~text
GET  /api/projects/{project_id}/execution
POST /api/projects/{project_id}/execution/evaluate
~~~

The GET is read-only. The explicit evaluation use case, also called by the bounded backend poller, may create automatic PromptDispatch records only for `READY -> DEV initial` and `CI_RED -> DEV follow-up`. Both reuse `<project>:DEV:<work-item>`. Repeated polling is idempotent; a CI follow-up key is tied to PR + current head SHA + workflow run ID + run attempt.

`DEVCOCKPIT_EXECUTION_POLL_SECONDS` controls the poll interval and defaults to 30 seconds. Set it to `0` to disable the automatic poller. One project failing to read GitHub is isolated from the others, and poller shutdown follows the FastAPI lifecycle.

A merged PR with sufficiently green delivery CI while the roadmap still says `READY` is projected as `ROADMAP_UPDATE_REQUIRED`. DC-021 does not write the roadmap, merge the PR, rerun CI or start the next WorkItem implicitly.

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

An ACK means that the extension has durably accepted the delivery into its persistent local queue (or already holds an identical persisted delivery). It is emitted only after that local persistence succeeds. It does not mean sent to ChatGPT, response received, work started, work completed or WorkItem DONE.

If a connection drops before ACK, the same logical delivery and the same delivery_id are replayed after reconnect. SQLite stores one PromptDelivery per PromptDispatch; reconnects increment transport attempt metadata but never create a new PromptDispatch.

A PromptDispatch CANCELLED is not sent as new work and is not replayed. No remote-revocation protocol is invented for a message that may already have reached the extension.

Control messages are typed. Version 1 currently supports ack and ping inbound, with pong and explicit error responses. chatgpt_response is intentionally not implemented until DC-030.

The transport bounds inbound messages to 64 KiB and does not log full prompt bodies or credentials.

To exercise the complete server loop without Firefox, run:

~~~bash
pytest -q tests/test_websocket_transport.py
~~~

Those tests use FastAPI/Starlette's WebSocket test client and require no browser, ChatGPT, GitHub, OpenAI or external network.

## Firefox companion extension

DC-012 adds `extension/`, a deliberately small Firefox companion. Its responsibilities are limited to the local WebSocket transport, a persistent local queue, the extension UI, and an isolated ChatGPT page adapter. It does not own roadmap, WorkItem, GitHub, CI or product state.

The companion uses a persistent Firefox Manifest V2 background context so the WebSocket lifetime is independent of the popup. Queue entries are persisted in `browser.storage.local`, so closing/reopening the popup or reloading the extension UI does not discard accepted prompts. The configured endpoint is fixed for the MVP:

~~~text
ws://127.0.0.1:8000/api/companion/ws
~~~

### Build and test

From the repository root:

~~~bash
cd extension
npm install --no-audit --no-fund
npm run check
npm test
npm run build
~~~

`npm run check` validates the manifest, referenced extension files and JavaScript syntax. `npm test` runs the protocol, queue, reconnect, ACK, explicit-send and ChatGPT adapter tests without contacting ChatGPT. `npm run build` creates the temporary-loadable extension under `extension/dist/`.

### Load temporarily in Firefox

1. Build the extension with the commands above.
2. Open `about:debugging#/runtime/this-firefox` in Firefox.
3. Choose **Load Temporary Add-on…**.
4. Select `extension/dist/manifest.json`.
5. Keep DevCockpit running locally and open the companion popup.

The popup reports `Connecté`, `Reconnexion…`, `Déconnecté`, or an explicit single-companion conflict. A second companion rejected with close code `4409` is not retried aggressively; use **Reconnecter** after the other companion is gone.

### Queue and ACK semantics

For every protocol-v1 `prompt`, the companion validates the envelope, deduplicates by `delivery_id`, and persists the entry before sending its ACK:

~~~text
receive prompt
→ validate
→ deduplicate by delivery_id
→ persist local queue
→ ACK
~~~

If local persistence fails, no ACK is sent. An identical replay reuses the existing local entry and can be ACKed again. Reusing one `delivery_id` with different `session` or `text` is treated as an explicit local delivery conflict; the stored text is not overwritten.

An ACK therefore means only that the Firefox companion has durably accepted responsibility for that delivery in its local queue. It still does **not** mean that the prompt was sent to ChatGPT, that ChatGPT produced a response, or that any WorkItem state changed.

The queue stores only local transport/UI fields such as `delivery_id`, `session`, `text`, `received_at`, `local_status` and an optional local error. Its `QUEUED` / `SEND_REQUESTED` states are not roadmap or WorkItem statuses.

### Explicit send to ChatGPT

Nothing is injected or sent when a prompt arrives. The user must open the intended ChatGPT conversation and click **Envoyer** on the chosen queue entry. The companion then targets only the active `chatgpt.com` or `chat.openai.com` tab.

All ChatGPT DOM knowledge is isolated in `extension/src/chatgpt-page-adapter.js`. The adapter uses narrowly scoped composer/send selectors and fails closed when the composer or send button is missing, disabled or ambiguous. It never falls back to the first textarea or first button. When injection/send fails, the prompt remains available for retry. When the page reports a successful send, the entry is removed from the active queue; if local cleanup then fails, `SEND_REQUESTED` remains visible so the user can verify the conversation before retrying rather than blindly duplicating a send.

The ChatGPT UI is an external dependency and its DOM can change. A DOM change may require updating the isolated adapter selectors. DC-012 intentionally performs no response scraping, no generation monitoring and no `chatgpt_response` return; that remains DC-030.

### Manual smoke procedure

The end-to-end smoke is intentionally manual and independent from CI:

~~~text
FastAPI local started
→ Firefox extension loaded temporarily
→ test PromptDispatch prepared through the application use case
→ prompt appears once in the extension queue
→ PromptDelivery becomes ACKNOWLEDGED only after local queue persistence
→ intended ChatGPT conversation opened in the active tab
→ user selects the prompt and clicks Envoyer
→ prompt is sent in that active conversation
~~~

If the real ChatGPT DOM cannot be exercised in the current development environment, record that limitation rather than treating the DOM-fixture tests as proof of a browser smoke.

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
DEVCOCKPIT_PROJECTS_CONFIG_PATH
DEVCOCKPIT_GITHUB_TOKEN (optional)
DEVCOCKPIT_GITHUB_TIMEOUT_SECONDS
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

The test suite is offline and deterministic. It validates application creation, health, Project configuration, strict canonical-roadmap parsing, the mockable read-only GitHub adapter, Project roadmap API projection, domain invariants/transitions, PromptDispatch persistence/idempotence, PromptDelivery migration/constraints/replay/ACK semantics, WebSocket protocol behavior, rollback, and reconnect from an empty or upgraded SQLite database without Firefox, ChatGPT, OpenAI, live GitHub or external network dependencies.

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
