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

DC-001 establishes the executable application shell, DC-010 adds persistent PromptDispatch, DC-011 adds reliable WebSocket transport, DC-012 adds the first usable Firefox companion, DC-020 adds configured GitHub projects plus strict canonical-roadmap projection, DC-021 adds GitHub/CI execution projection, DC-030 adds explicit response return, and DC-040 adds the explicit Architect loop:

~~~text
frontend/                  React + TypeScript + Vite
        ↓ /api
app/main.py                FastAPI HTTP/WebSocket boundary
        ↓
app/application/           Prompt use cases + Project/roadmap projection
app/domain/                Prompt models + Project + WorkItem/parser rules
        ↓
app/infrastructure/        SQLite / GitHub read adapter / WebSocket protocol

extension/                 Firefox queue / explicit send + selected response return
~~~

PromptDispatch remains transport-independent. PromptDelivery is separate persisted transport truth and never means that a prompt was sent to ChatGPT.

## Prerequisites

CI uses Python 3.12.7 and Node 22.20.0. Local development should use compatible Python 3.12+ and Node 22 versions.

## Docker Compose

DevCockpit can run locally as two containers:

~~~text
Browser
  ↓ http://127.0.0.1:8080
Nginx + compiled React frontend
  ↓ /api/*
FastAPI backend :8000
  ↓
SQLite persisted in the devcockpit_data volume
~~~

The backend port remains bound to loopback because the Firefox companion currently connects directly to:

~~~text
ws://127.0.0.1:8000/api/companion/ws
~~~

This preserves the current trusted-local-machine boundary; do not publish port 8000 on an untrusted network without adding authentication.

### Configuration

Docker Compose does not require a local `.env` file to start. For GitHub write operations, private repositories, or higher API limits, create one from the template and set a backend-only token:

~~~powershell
Copy-Item .env.example .env
~~~

On Linux/macOS:

~~~bash
cp .env.example .env
~~~

Then set:

~~~dotenv
DEVCOCKPIT_GITHUB_TOKEN=github_pat_...
~~~

Never expose this token to React or the Firefox extension. Compose overrides the SQLite URL to `sqlite+pysqlite:////data/devcockpit.db` and stores that file in the named `devcockpit_data` volume. The committed `projects.json` is mounted read-only into the backend so project configuration can be edited on the host without rebuilding the image.

### Start

Build and start both containers:

~~~bash
docker compose up -d --build
~~~

Open:

~~~text
http://127.0.0.1:8080
~~~

The backend health endpoint remains available at:

~~~text
http://127.0.0.1:8000/api/health
~~~

Alembic migrations are applied automatically before Uvicorn starts. The frontend waits for the backend healthcheck before starting.

Useful commands:

~~~bash
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
~~~

`docker compose down` preserves the SQLite volume. To deliberately delete all Docker-persisted DevCockpit state, use `docker compose down -v`; that operation is destructive.

After changing Python/backend dependencies or frontend dependencies, rebuild with:

~~~bash
docker compose up -d --build
~~~

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

## Parallel DEV ResourceLocks

DC-052 adds a deterministic conflict gate after the scheduler and DEV-capacity checks. ResourceLocks do not replace `DEPENDS_ON` or `DEVCOCKPIT_MAX_PARALLEL_DEV_EXECUTIONS`.

Conflict surfaces are declared explicitly per WorkItem in the Project configuration. For example:

~~~json
{
  "projects": [
    {
      "project_id": "DevCockpit",
      "repository_full_name": "jctrottier9-gif/DevCockpit",
      "roadmap_issue_number": 1,
      "resource_locks": {
        "DC-052": [
          {"surface": "migration:alembic", "mode": "EXCLUSIVE"},
          {"surface": "api:contracts", "mode": "SHARED"}
        ]
      }
    }
  ]
}
~~~

Surface names are stable policy keys; common conventions include `migration:...`, `adr:...`, `roadmap:...`, `api:...`, `domain:...` and `file:...`. DevCockpit does not infer authoritative locks from AI analysis.

The compatibility policy is intentionally small: SHARED is compatible only with SHARED; any EXCLUSIVE participant conflicts. Acquisition of every required lock and creation of the initial DEV PromptDispatch happen in the same SQLite `BEGIN IMMEDIATE` UnitOfWork. A failed lock acquisition creates no initial prompt and does not consume a DEV slot.

Persisted locks use ACTIVE / RELEASED / STALE state, optimistic versions and renewable leases. `DEVCOCKPIT_RESOURCE_LOCK_LEASE_SECONDS` defaults to 900 seconds. A restart preserves active locks. GitHub-active executions can transfer lease ownership to the new process; uncertain GitHub evidence keeps an expired lock conservative; determinate stale work can be recovered without duplicating its initial PromptDispatch.

The parallel execution API projects required/held surfaces, conflict owner/session, requested and held modes, lock state and recovery state:

~~~text
GET  /api/projects/{project_id}/executions
POST /api/projects/{project_id}/executions/evaluate
~~~

React is projection-only. Compatibility, acquisition, release and recovery remain backend rules. The legacy single-execution mutation endpoint delegates to the same ResourceLock-aware gate.

See `docs/architecture/ADR-0010-resource-locks-and-conflict-surfaces.md`.

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

Control messages are typed. Version 1 supports ack, ping and chatgpt_response inbound, with pong, chatgpt_response_ack and explicit error responses. A returned response is correlated by delivery_id to its source PromptDelivery/PromptDispatch and the received session must exactly match the source AgentSession.

The transport bounds inbound messages to 512 KiB. Oversize responses fail explicitly and are never silently truncated. Full prompt/response bodies and credentials are not logged.

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

### Manual targeted redelivery

An already acknowledged prompt can be explicitly resent to the currently connected companion without creating a new `PromptDispatch` or a new logical delivery. The original ACK remains historical truth, the same `delivery_id` is reused, and the transport attempt counter increments.

The Attention Center exposes **Renvoyer au companion** only for prompts whose delivery was already acknowledged. The corresponding backend command is:

~~~text
POST /api/prompt-dispatches/{dispatch_id}/redeliver
~~~

The command fails explicitly when no companion is connected, when the dispatch is no longer PREPARED, or when the delivery has never been acknowledged.

### Explicit send to ChatGPT

Nothing is injected or sent when a prompt arrives. The user must open the intended ChatGPT conversation and click **Envoyer** on the chosen queue entry. The companion then targets only the active `chatgpt.com` or `chat.openai.com` tab.

All ChatGPT DOM knowledge is isolated in `extension/src/chatgpt-page-adapter.js`. The adapter uses narrowly scoped composer/send selectors and fails closed when the composer or send button is missing, disabled or ambiguous. It never falls back to the first textarea or first button. When injection/send fails, the prompt remains available for retry. When the page reports a successful send, the entry is removed from the active queue; if local cleanup then fails, `SEND_REQUESTED` remains visible so the user can verify the conversation before retrying rather than blindly duplicating a send.

The ChatGPT UI is an external dependency and its DOM can change. A DOM change may require updating the isolated adapter selectors. DC-030 adds no continuous scraping or generation monitoring: after a successful explicit send, the companion retains a SentPromptContext containing the delivery_id/session. The user later clicks **Retourner une réponse**, the adapter performs one one-shot scan of assistant-role elements, the user explicitly chooses one candidate and confirms it, and a PendingResponse is persisted before WebSocket transmission. The same response_id is replayed after reconnect until chatgpt_response_ack is received.

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

### Explicit response return

DC-030 persists imported responses as historical/audit facts only. They do not change PromptDispatch, PromptDelivery, WorkItem, GitHub or ExecutionProjection state and are not interpreted as decisions or handoffs.

The read-only cockpit surface is:

~~~text
GET /api/projects/{project_id}/responses
~~~

It resolves session, Project, WorkItem and role through the source delivery/dispatch and displays the complete returned text. DC-040 adds separate Architect consultation and explicit decision-acceptance actions; importing a response still triggers none of them.

## Attention Center (DC-060)

The cockpit starts with a read-only Attention Center derived from existing backend-owned projections. It answers what needs human attention now without persisting a notification inbox or introducing a parallel state machine.

The API is:

~~~text
GET /api/projects/{project_id}/attention
~~~

It exposes one explicit aggregate state:

- `ACTION`: a human action is currently possible or required;
- `WATCH`: a condition is worth surfacing but no immediate human action is useful;
- `CLEAR`: there are no ACTION or WATCH items.

The projection aggregates existing `PromptDispatch` / delivery state, `ExecutionProjection`, explicit Handoff/Decision actions, roadmap proposal/application actions, ResourceLock conflicts and transient companion connectivity. Companion disconnection is surfaced only when it blocks delivery of a prompt that is actually ready; a disconnected companion alone creates no attention item.

Items are deduplicated by the real human need: project + role + WorkItem + action. For example, `CI_RED` plus its already-prepared DEV correction prompt is one logical card, while an independent ARCH/PO review on the same WorkItem remains separate. Ordering is deterministic: ACTION precedes WATCH, then stable role/WorkItem/action keys.

The Attention Center owns no dismiss state. An item disappears on refresh when its source condition is no longer true. React renders the backend projection and opens existing WorkItem/PR/orchestration targets; it does not recalculate CI, Handoff, roadmap, ResourceLock or transport eligibility rules.

## Flow Analytics (DC-061)

Flow Analytics is a separate, read-only GitHub projection for delivery history. It is loaded on demand and is deliberately kept out of the execution poller:

~~~text
GET /api/projects/{project_id}/analytics
~~~

GitHub remains the authority for every delivery timestamp. The projection reuses the strict WorkItem-to-PR identity rule from ADR-0005 and never derives delivery evidence from ChatGPT, PromptDispatch, browser state or local poll timestamps.

Per selected WorkItem delivery, the backend exposes:

- `first_commit_at`: earliest usable `commit.committer.date` from commits returned by the strongly associated PR;
- `pr_created_at`: GitHub PR `created_at`;
- `first_green_ci_at`: earliest observable PR commit whose latest workflow attempts are all completed with `success / neutral / skipped`, using the last required workflow completion timestamp for that commit;
- `merged_at`: GitHub PR `merged_at`;
- commit → PR, PR → first fully green CI, green CI → merge and total observable durations;
- total observed workflow attempts, red attempts and whether a red attempt is observably followed by a fully green CI;
- the observable workflow run/attempt history and GitHub links.

Zero observed workflows is never treated as green. Missing timestamps or incomplete sub-history remain `null` / unavailable, and only independent metrics continue to be calculated. When CI attempt history is incomplete, `recovered_after_red` remains unknown rather than inventing `false`.

Docs-only exclusion is backend-owned and conservative. A delivery is excluded only when the complete GitHub changed-file list is non-empty and every changed file is under `docs/` or is a recognized root documentation file such as `README.md` or `AGENTS.md`. Incomplete changed-file evidence retains the delivery and emits a diagnostic rather than excluding it.

Aggregates are deterministic medians calculated only from deliveries that actually contain the required metric bounds. Every median exposes its observation count. The summary also exposes delivery counts, CI attempt/red totals, docs-only exclusions, recovery observation counts and the actual first/last observed merge timestamps. No arbitrary velocity window, percentile, composite score or developer ranking is produced.

No analytics table or migration is introduced by DC-061. GitHub history is reconstructed on demand and React renders the backend projection without recalculating identity, timestamps, durations, medians, docs-only policy or CI recovery.

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
DEVCOCKPIT_EXECUTION_POLL_SECONDS
DEVCOCKPIT_MAX_PARALLEL_DEV_EXECUTIONS
DEVCOCKPIT_RESOURCE_LOCK_LEASE_SECONDS
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

ImportedChatGptResponse stores only response_id, source delivery_id, complete text and imported_at. The response_id is the idempotency identity for the return action; multiple distinct response_ids may legitimately refer to the same delivery.

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

Durable decisions live under docs/architecture/, including authority boundaries, orchestration identity, the manual ChatGPT companion/WebSocket protocol, explicit Alembic schema migrations, explicit Handoff/Decision semantics, and ADR-0008 for revisioned roadmap proposals plus explicit GitHub writeback.

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


## Architect consultations (DC-040)

From a WorkItem, choose **Consulter l’Architecte**, explicitly select a DEV prompt
(and optionally its imported response), and confirm a question and context.
The server persists a `Handoff` and its frozen ARCH `PromptDispatch` atomically.
Send that prompt manually with the existing Firefox companion, then explicitly
return the Architect response using the existing response-return action.
Refresh consultations to see returned responses. Select the precise source response,
enter the accepted conclusion and constraints, and explicitly choose its effect:

- `CONTINUE_IN_SCOPE`: persist an immutable `Decision` and prepare the DEV resume in
  the same transaction if current canonical work and GitHub evidence permit it.
- `HOLD_FOR_AUTHORIZATION`: persist the Decision, keep the Handoff blocking and
  prepare no resume. Required ADR, scope, PO or roadmap authorization is not inferred.

A changed/unavailable roadmap or unusable GitHub evidence retains the accepted
Decision with an explicit held reason. There is no automatic PO escalation or
extra “prepare resume” action. Cancelling an OPEN or DECIDED consultation requires
an attributed reason; responses arriving afterward remain historical and cannot
be accepted. A resumed consultation is final in DC-040.

`ImportedChatGptResponse` is raw historical text. `Decision` is the explicitly
accepted conclusion from one precisely correlated response; several responses
never imply that the newest or first was accepted. No semantic parsing takes place.
Neither concept changes GitHub, ADRs, roadmap state or `ExecutionProjection`.

Sessions remain `<project>:ARCH:<work-item>` and `<project>:DEV:<work-item>`.
Successive consultations reuse the ARCH session, and resume uses the original DEV
session. Handoff/dispatch/delivery/response UUIDs provide isolation between consultations.

The Handoff lifecycle is `OPEN → DECIDED → RESUME_PREPARED`, with cancellation
from OPEN or DECIDED. At most one OPEN/DECIDED Handoff may exist per Project/WorkItem.
The partial unique SQLite index, writer reservation (`BEGIN IMMEDIATE`) and optimistic
version protect concurrent poller/user commands. Command UUIDs make identical retries
idempotent and incompatible retries explicit conflicts. Request and resume dispatches
participate in the caller-owned UoW; repositories never commit independently.

During OPEN or DECIDED, automatic DEV INITIAL/CI_RED prompts are inhibited while
GitHub/CI projection remains visible. Existing DEV prompts are cancelled locally to
prevent new backend delivery, including a check at the socket-write boundary.
Historical dispatches and delivery ACKs are retained. **A prompt already received or
ACKed by Firefox cannot be remotely revoked**; verify its relevance before sending.
The extension protocol and business responsibilities are unchanged.

A resume records the existing DC-021 immutable CI key (PR, head SHA, workflow run,
attempt) when it covers a failed cycle. Re-polling the same cycle produces no second
follow-up; a different head or attempt remains eligible.

API:

~~~text
POST /api/projects/{project_id}/work-items/{key}/handoffs
GET  /api/projects/{project_id}/work-items/{key}/orchestration
POST /api/handoffs/{handoff_id}/decisions
POST /api/handoffs/{handoff_id}/cancel
~~~

Mutations require creation/acceptance/cancellation command UUIDs; acceptance and
cancellation also require the displayed Handoff version. Actions exposed by the
read-only orchestration view are determined on the server and revalidated on mutation.
The actor entered in the local UI is attribution, not authenticated identity; the
existing trusted-local-network deployment boundary still applies.

Apply additive migration `0004_handoffs_decisions` with `alembic upgrade head`.
It adds only `handoffs` and `decisions`, with RESTRICT historical references and no
Project, WorkItem, Execution or AgentSession tables. Existing DC-030 data is preserved.


## Product Owner roadmap-change direction (ASTRA-041)

ASTRA-041 accepts the Product Owner implementation as two future delivery slices:

~~~text
DC-041A — PO handoffs, revisioned RoadmapChangeProposal, deterministic preview, V1/V2 reader
DC-041B — exact-revision confirmation, targeted GitHub issue-body writeback, reconciliation
~~~

The current application and canonical roadmap still use COCKPIT_PIPELINE_V1. DC-041A must first deliver explicit V1/V2 support before issue #1 can be converted to the accepted V2 format with SUPERSEDED and REPLACES.

A PO Decision is not a roadmap mutation. A RoadmapChangeProposal remains local and non-canonical until an exact revision is explicitly confirmed, applied through the future targeted writer, and reconciled. Direct GitHub PATCH in DC-041B also remains gated by an explicit product decision accepting the residual race between the final GitHub reread and PATCH; proposal/preview can exist even if direct writeback remains disabled.

See docs/architecture/ADR-0008-explicit-roadmap-change-proposals-and-github-writeback.md.


## DC-041A — boucle Product Owner et proposals locales

DevCockpit prend en charge trois consultations explicites, sans interprétation automatique du texte ChatGPT :

```text
DEV → ARCH → DEV
DEV → PO → DEV
ARCH → PO → nouvelle consultation ARCH
```

Les combinaisons de Handoff sont fermées : `ARCH + TECHNICAL_GUIDANCE`, `PO + PRODUCT_CLARIFICATION` et `PO + ROADMAP_REVIEW`. Une réponse importée reste distincte d'une `Decision`; une `Decision` reste distincte d'une `RoadmapChangeProposal`. Le transfert `ARCH → PO` clôt le Handoff précédent avec `TRANSFERRED` et ouvre le PO dans la même transaction. Après une clarification PO issue d'ARCH, le backend prépare une nouvelle consultation ARCH dans la session logique `<project>:ARCH:<work-item>`; React ne choisit jamais librement le rôle de reprise.

Les Decisions acceptées utilisent les types `ARCHITECTURE_GUIDANCE`, `PRODUCT_CLARIFICATION` ou `SCOPE_DECISION`, avec les effets `CONTINUE_IN_SCOPE` ou `HOLD_FOR_AUTHORIZATION`. Un redécoupage n'est jamais inféré d'une réponse brute : il faut une `SCOPE_DECISION` PO explicitement acceptée et tenue pour autorisation, puis une création explicite de `RoadmapChangeProposal`.

Une proposal est locale et possède un target GitHub figé, des révisions immuables et des opérations structurées. Chaque révision fige le body GitHub de base, ses hashes, les opérations, le body généré et les versions de génération/validation. Le preview est recalculé uniquement depuis cette révision figée et expose le diff complet, les changements de pipeline, READY avant/après, replacements, issue mappings, diagnostics bloquants et un digest stable.

Le parser canonique accepte explicitement :

```text
COCKPIT_PIPELINE_V1
KEY | TYPE | STATUS | PARENT | LANE | TITLE
```

et :

```text
COCKPIT_PIPELINE_V2
KEY | TYPE | STATUS | PARENT | LANE | TITLE | REPLACES
```

V2 ajoute `SUPERSEDED` et `REPLACES`. Les anciens WorkItems restent présents; un WorkItem `SUPERSEDED` doit être remplacé par au moins une nouvelle clé, et le replacement ne constitue pas une dependency. Les blocs V1/V2 ambigus, mixtes, incomplets ou de version inconnue échouent fermés.

DC-041A reste entièrement read-only envers GitHub : le backend peut lire le roadmap nécessaire à une nouvelle révision, mais il n'expose aucun endpoint `confirm`, `apply` ou `reconcile`, et aucun `RoadmapWriter` n'est présent. L'application distante du body et la réconciliation appartiennent exclusivement à DC-041B.
