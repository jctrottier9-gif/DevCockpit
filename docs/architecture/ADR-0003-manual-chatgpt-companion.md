# ADR-0003 — Manual ChatGPT companion and WebSocket transport

- Status: Accepted
- Date: 2026-10-01

## Context

An existing Firefox extension attempted to monitor ChatGPT conversations. Continuous monitoring proved unreliable and creates an unnecessarily tight dependency on ChatGPT UI behavior.

The desired workflow is simpler: DevCockpit prepares prompts and the user explicitly selects which prompt to send.

## Decision

The canonical ChatGPT integration is manual and asymmetric:

```text
DevCockpit
   ↓ prepared prompt
WebSocket
   ↓
Firefox extension
   ↓ explicit user action
ChatGPT
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

A later feature may provide an explicit action such as “Return response to DevCockpit”. This is a deliberate user-triggered import, not continuous scraping.

## Reliability rules

- Reconnects must not create duplicate logical work.
- DevCockpit should track dispatch identity internally.
- Transport acknowledgement is distinct from “prompt sent to ChatGPT”.
- Failure to deliver a prompt must not mutate WorkItem state.
- The extension should remain replaceable and carry no required product-state authority.

## Security

- No GitHub or backend secret is exposed to the ChatGPT page.
- WebSocket authentication must be added before exposing the endpoint beyond a trusted local network.
- ChatGPT DOM integration should be isolated in the extension and not leak into DevCockpit domain rules.

## Consequences

The MVP can be useful without OpenAI API billing or automated multi-agent execution.

It also preserves a human gate before each expensive or consequential ChatGPT action.
