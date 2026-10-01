# ADR-0006 — Explicit ChatGPT response import

- Status: Accepted
- Date: 2026-10-01

## Context

DevCockpit can reliably deliver a prepared prompt to the Firefox companion, and the user can explicitly send it to ChatGPT. DC-030 closes the transport loop in the opposite direction without making ChatGPT prose authoritative for delivery or introducing continuous browser monitoring.

A response import must survive reconnects and remain unambiguously tied to the prompt that caused it.

## Decision

### Explicit selection only

The Firefox companion inspects assistant responses only after the user chooses a previously sent DevCockpit prompt and requests a response return. It never polls, observes or scrapes ChatGPT continuously.

The user then chooses one assistant response candidate explicitly and confirms its return. Ambiguous or unsupported DOM shapes fail closed.

### Strong correlation

Every sent prompt context retains its delivery_id and logical session locally after the active prompt queue entry is removed.

The inbound protocol-v1 message is:

~~~json
{
  "version": 1,
  "type": "chatgpt_response",
  "response_id": "stable-client-uuid",
  "delivery_id": "prompt-delivery-uuid",
  "payload": {
    "session": "DevCockpit:DEV:DC-030",
    "text": "Complete selected response"
  }
}
~~~

DevCockpit resolves delivery_id -> PromptDelivery -> PromptDispatch and requires the received session to equal the dispatch AgentSession exactly. Project, WorkItem, role and GitHub state are not copied into the wire message.

### Idempotent replay

The companion creates response_id once, persists the complete pending response before transmission, and reuses that same ID after reconnect until the backend acknowledges it.

The server acknowledgement is:

~~~json
{
  "version": 1,
  "type": "chatgpt_response_ack",
  "response_id": "stable-client-uuid"
}
~~~

An identical replay is idempotent. Reusing a response ID with different immutable content is an explicit conflict and never overwrites the first import.

### Imported response is not a decision

The backend persists only the imported historical fact:

- response_id;
- source delivery_id;
- response text;
- imported_at.

Session, Project, WorkItem and role are derived by joining back through PromptDelivery and PromptDispatch.

An imported response does not mutate PromptDispatch, PromptDelivery, WorkItem, GitHub or ExecutionProjection state. A response for a valid delivery whose dispatch was later cancelled may still be retained as historical evidence without reactivating that dispatch.

ASTRA-040 is responsible for deciding whether later structured concepts such as Handoff, Decision, Question or Recommendation should be derived from imported responses.

### Protocol size

DC-030 remains additive and backward-compatible within protocol version 1. Inbound companion messages are explicitly bounded at 512 KiB. Oversize responses fail with message_too_large; they are never silently truncated.

## Consequences

- Response identity and prompt correlation are deterministic.
- A dropped ACK can be retried without duplicate imports.
- The Firefox companion keeps transport/UI state but remains outside product-state authority.
- The cockpit can display imported responses read-only without changing delivery status.
- ChatGPT DOM changes can cause an explicit selection error but cannot silently associate arbitrary page text.
