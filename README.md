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

## Target loop

```text
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
```

## Initial roles

### Product Owner
Clarifies product intent, acceptance criteria and minimal redécoupage.

### Architect
Performs read-only architecture analysis and stabilizes durable decisions.

### Developer
Implements an approved slice, validates it and resolves normal CI failures.

### DevCockpit
Owns deterministic orchestration, routing, dependencies, evidence and prompt preparation.

## Initial technical direction

```text
React + TypeScript + Vite
          ↓
       FastAPI
          ↓
 Application / Domain
          ↓
 Infrastructure
   ├── SQLite / SQLAlchemy
   ├── GitHub adapter
   └── WebSocket transport

Firefox WebExtension
   ↕ WebSocket
FastAPI
```

The architecture is intentionally modest for the MVP. External services are kept behind adapters so the core orchestration can be tested without live credentials.

## Firefox extension contract

The initial wire payload remains deliberately small:

```json
{
  "session": "RessourcePlanner:DEV:502A",
  "text": "Le prompt complet à envoyer à ChatGPT"
}
```

DevCockpit may keep richer metadata internally, but the transport contract should not grow accidentally.

## Roadmap

The canonical roadmap lives in the master GitHub issue and contains a machine-readable `COCKPIT_PIPELINE_V1` block.

There is intentionally no canonical `ROADMAP.md`.

## Architecture decisions

Durable decisions live under:

```text
docs/architecture/
```

Initial ADRs establish:

1. authority and trust boundaries;
2. orchestration state model;
3. manual ChatGPT companion and WebSocket boundary.

## Development workflow

Read `AGENTS.md` before implementing any roadmap slice.

Normal delivery is:

```text
current main
→ issue/roadmap
→ implement
→ targeted tests
→ PR
→ CI
→ fix normal failures
→ merge
→ reconcile roadmap
```

Do not infer a completed roadmap step from a ChatGPT message alone.
