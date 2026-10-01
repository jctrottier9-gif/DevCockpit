# ADR-0003 — Manual ChatGPT companion and WebSocket transport

- Status: Accepted
- Date: 2026-10-01
- Amended: 2026-10-01 by DC-011 protocol framing

## Context

An existing Firefox extension attempted to monitor ChatGPT conversations. Continuous monitoring proved unreliable and creates an unnecessarily tight dependency on ChatGPT UI behavior.

The desired workflow is simpler: DevCockpit prepares prompts and the user explicitly selects which prompt to send.

DC-011 adds reliable replay. A transport ACK therefore needs a stable technical identity while the functional prompt contract must remain deliberately small.

## Decision

The canonical ChatGPT integration is manual and bidirectional:

~~~text
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
~~~

The extension connects as a WebSocket client to DevCockpit.

### Versioned transport envelope

The functional outbound prompt payload remains exactly:

~~~json
{
  "session": "nom_du_projet_ou_identifiant",
  "text": "Le prompt complet à envoyer à ChatGPT"
}
~~~

DC-011 carries that payload inside protocol version 1 so ACK/replay can name one precise logical delivery without adding business metadata to the functional payload:

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

The transport envelope is deliberately technical. It must not grow project, role, WorkItem, GitHub or roadmap fields merely for convenience.

A transport ACK is:

~~~json
{
  "version": 1,
  "type": "ack",
  "delivery_id": "stable-uuid"
}
~~~

An ACK means only that the extension received and accepted the corresponding transport message. It does not mean that the prompt was sent to ChatGPT, read by ChatGPT, completed, or that any WorkItem changed state.

### Persistent delivery identity

PromptDispatch remains transport-independent with its DC-010 state machine:

~~~text
PREPARED → CANCELLED
~~~

Transport truth is a separate persistent PromptDelivery, with exactly one logical delivery per dispatch. The same delivery_id is reused after reconnect until acknowledged. Attempt counters/timestamps describe transport attempts; the prompt text remains authoritative on PromptDispatch and is not duplicated in transport persistence.

A cancelled dispatch is not sent as new work and is not replayed after reconnect. If an ACK arrives late for a delivery that was already transmitted before cancellation, DevCockpit may still record that transport fact; it does not revoke or reinterpret the cancelled business state.

### Connection policy

The MVP permits one active Firefox companion. A second simultaneous connection is accepted only long enough to be closed deterministically with an application WebSocket close code; it does not replace or fan out from the active companion.

Connection presence is in-memory/transient. A FastAPI restart reconstructs replayable delivery state from SQLite.

### Bidirectional messages

DC-011 implements typed protocol control messages such as ack, ping, pong and explicit protocol errors.

The future chatgpt_response message remains reserved for DC-030. DC-011 does not persist, interpret or route ChatGPT responses.

## Reliability rules

- Reconnects must not create duplicate logical work.
- Database uniqueness enforces one PromptDelivery per PromptDispatch.
- Socket write success is not acknowledgement.
- Transport acknowledgement is distinct from “prompt sent to ChatGPT”.
- A duplicate ACK is idempotent.
- An ACK for an unknown delivery produces an explicit protocol diagnostic and cannot mutate another dispatch.
- Failure to deliver a prompt does not mutate WorkItem or PromptDispatch state.
- Several AgentSessions may share the same companion connection without changing delivery identity.
- The extension remains replaceable and carries no required product-state authority.

## Security

The DC-011 endpoint is intentionally unauthenticated only for the current trusted local-network development model.

- Do not expose it beyond a trusted local network without adding authentication/authorization.
- No GitHub or backend secret is included in protocol messages.
- Prompt bodies are not logged by the transport.
- Inbound protocol messages are size-bounded and invalid JSON/types are rejected explicitly.
- ChatGPT DOM integration remains isolated to the future extension and does not leak into DevCockpit domain rules.

## Consequences

The MVP can provide reliable prompt delivery without OpenAI API billing or automated multi-agent execution.

DC-012 can implement the Firefox queue against a stable versioned protocol without redesigning delivery identity. DC-030 can later add explicit response return as a separate typed message and domain behavior.
