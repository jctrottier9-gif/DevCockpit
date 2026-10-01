# ADR-0003 — Manual ChatGPT companion and WebSocket transport

- Status: Accepted
- Date: 2026-10-01

## Context

An existing Firefox extension attempted to monitor ChatGPT conversations. Continuous monitoring proved unreliable and creates an unnecessarily tight dependency on ChatGPT UI behavior.

The desired workflow is simpler: DevCockpit prepares prompts and the user explicitly selects which prompt to send.

## Decision

The canonical ChatGPT integration is manual and bidirectional:

```text
DevCockpit
   ↓ prepared prompt
WebSocket
   ↓
Firefox extension
   ↓ explicit user action
ChatGPT
   ↓ selected response
Firefox extension
   ↓ explicit return
WebSocket
   ↓
DevCockpit
```

The extension connects as a WebSocket client to DevCockpit.

The initial outbound payload is exactly:

```json
{
  "session": "nom_du_projet_ou_identifiant",
  "text": "Le prompt complet à envoyer à ChatGPT"
}
```

The extension may display a queue and delivery state, but must not infer roadmap progress.

The same WebSocket connection is bidirectional. The extension can return a selected ChatGPT response to DevCockpit.

The inbound response contract is distinct from the outbound prompt contract and should be typed/versioned explicitly. An initial logical shape is:

```json
{
  "type": "chatgpt_response",
  "session": "RessourcePlanner:DEV:502A",
  "text": "Réponse complète de ChatGPT"
}
```

DevCockpit may enrich the association internally with the current PromptDispatch / Execution when unambiguous.

Returning a response is a deliberate extension action tied to the relevant ChatGPT response; it is not continuous background scraping of all conversations.

## Reliability rules

- Reconnects must not create duplicate logical work.
- DevCockpit should track dispatch identity internally.
- Transport acknowledgement is distinct from “prompt sent to ChatGPT”.
- A returned ChatGPT response must be idempotent or deduplicable.
- A response received from the extension must be associated with the declared AgentSession before any downstream routing.
- Failure to deliver a prompt must not mutate WorkItem state.
- The extension should remain replaceable and carry no required product-state authority.

## Security

- No GitHub or backend secret is exposed to the ChatGPT page.
- WebSocket authentication must be added before exposing the endpoint beyond a trusted local network.
- ChatGPT DOM integration should be isolated in the extension and not leak into DevCockpit domain rules.

## Consequences

The MVP can be useful without OpenAI API billing or automated multi-agent execution.

It also preserves a human gate before each expensive or consequential ChatGPT action.
