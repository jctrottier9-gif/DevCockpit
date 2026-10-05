# ADR-0014 — Automatic ChatGPT routing, durable ConversationBinding, and fail-stop send idempotence

- Status: Accepted
- Date: 2026-10-03
- Gate: ASTRA-063 / #51
- Parent initiative: DC-063 / #50
- Implementation: DC-063A / #52 then DC-063B / #53
- Supersedes in part: ADR-0003

## Context

DevCockpit already prepares authorized PromptDispatch records, delivers them reliably to the Firefox companion, and keeps PromptDelivery acknowledgement separate from the act of sending a prompt to ChatGPT.

The current companion remains manual: a user chooses a queued prompt and clicks Send in the intended ChatGPT tab. ASTRA-063 accepts a target architecture in which the companion routes and sends an already-authorized PromptDispatch automatically, without moving product authority into the browser and without weakening human architecture gates.

Exactly-once delivery cannot be proven across Firefox storage, ChatGPT DOM state, the ChatGPT server and DevCockpit SQLite. The design therefore prioritizes prevention of duplicate automatic sends and fails closed whenever an irreversible send may have occurred but cannot be proven.

This ADR records the target architecture. It does not state that automatic routing or automatic send is already implemented. Until DC-063B is delivered, the operational send path remains manual.

## Decision

### 1. Authority remains in DevCockpit

DevCockpit alone decides when a PromptDispatch may be created.

For an ARCHITECTURE_GATE:

~~~text
human authorization in DevCockpit
        ↓
PromptDispatch ARCH
        ↓
Firefox queue
        ↓
human selects ChatGPT Work-mode tab
        ↓
explicit companion launch
~~~

ARCH is a deliberate exception to automatic routing/send. The browser must never create a tab, route, recover or resume-send an ARCH/ASTRA dispatch automatically. The explicit companion launch still uses SEND_ARMED, targeted confirmation, idempotency and ConversationBinding promotion. Automatic browser transport never substitutes for either human action.

The Firefox companion is never authoritative for roadmap, scheduler, WorkItem state, CI, ResourceLocks, PO/ARCH decisions or delivery state.

### 2. AgentSession is the routing identity

The semantic conversation identity remains:

~~~text
<project>:<role>:<work-item>
~~~

Examples include:

~~~text
DevCockpit:ARCH:ASTRA-063
RessourcePlanner:DEV:575C
TaskPlanner:DEV:TP-011
~~~

One AgentSession has at most one active ConversationBinding.

### 3. ConversationBinding is durable backend state

The durable source of truth for conversation binding lives in DevCockpit SQLite.

Conceptual contract:

~~~text
ConversationBinding
-------------------
binding_id              optional technical UUID
agent_session           UNIQUE semantic identity
conversation_id         opaque ChatGPT identifier
canonical_url
state                   BOUND | INVALIDATED
version
bound_at
last_validated_at
updated_at
invalidated_at          nullable
invalidation_reason     nullable
~~~

agent_session remains the business identity. conversation_id is opaque. tab_id is never durable identity; it is only an ephemeral Firefox routing cache.

Firefox may keep a local cache for routing and recovery, but the backend ConversationBinding remains durable authority.

DC-063A should introduce the next available Alembic migration for this backend table. At the time of this decision, migrations 0001 through 0007 exist, so the expected filename is 0008_conversation_binding.py if still available when implementation starts.

### 4. Conversation matching is exact

A bound conversation is recognized only from exact identity evidence:

~~~text
conversation_id
and/or
normalized canonical_url
~~~

The router must never use ChatGPT title, tab title, tab position, active-tab status or most-recently-used tab as conversation identity.

Immediately before DOM insertion/click, the target conversation must be revalidated to protect against concurrent user navigation.

### 5. First-use binding is provisional until the first confirmed send

When no binding exists:

~~~text
AgentSession
  ↓
dedicated provisional ChatGPT tab
  ↓
first send
  ↓
ChatGPT assigns /c/<conversation-id>
  ↓
send confirmation
  ↓
finalize ConversationBinding
~~~

DC-063A may create and recover the dedicated provisional tab, but it must not claim a durable /c/... binding before the first successful send creates one.

Final promotion from provisional tab to durable ConversationBinding belongs to DC-063B.

### 6. Serialization is per AgentSession

Only one send operation may be active for one AgentSession.

~~~text
Session A: A1 → A2 → A3
Session B: B1 → B2
Session C: C1
~~~

Different sessions may progress in parallel.

If one send becomes AMBIGUOUS or BLOCKED, later prompts for that same session may not pass it.

### 7. PromptDelivery semantics do not change

PromptDelivery.ACKNOWLEDGED continues to mean only:

~~~text
prompt received and durably persisted by Firefox
~~~

It never means that ChatGPT received the prompt.

PromptDispatch, PromptDelivery and ChatGptPromptSend remain separate state machines.

### 8. ChatGptPromptSend has a dedicated state machine

The browser-to-ChatGPT side effect uses a distinct durable/projection state:

~~~text
QUEUED
  ↓
ROUTING
  ↓
WAITING_READY
  ↓
SEND_ARMED
  ├─→ SENT_CONFIRMED
  └─→ AMBIGUOUS

ROUTING / WAITING_READY
  ├─ certain/transient failure → RETRYABLE_FAILURE
  └─ durable/manual failure    → BLOCKED
~~~

Semantics:

- QUEUED: no irreversible side effect has occurred.
- ROUTING: resolving the bound/provisional conversation and tab.
- WAITING_READY: correct conversation is targeted but ChatGPT is not yet safely sendable.
- SEND_ARMED: a durable pre-send barrier has been written.
- SENT_CONFIRMED: targeted evidence proves the expected user message appeared.
- RETRYABLE_FAILURE: it is known that no irreversible send occurred.
- BLOCKED: manual intervention is required.
- AMBIGUOUS: a send may have occurred; automatic resend is forbidden.

This integration state is not ExecutionState.

### 9. SEND_ARMED is the irreversible-action barrier

Before any operation may click the actual ChatGPT Send control:

~~~text
persist SEND_ARMED
        ↓
storage success
        ↓
DOM side effect allowed
~~~

If persistence fails, there is no click.

After restart, a delivery already in SEND_ARMED is never automatically resent until reconciliation determines that a retry is safe.

### 10. The realistic guarantee is at-most-one automatic actuation

Exactly-once cannot be guaranteed across:

~~~text
browser.storage.local
ChatGPT DOM
ChatGPT server
DevCockpit SQLite
~~~

The accepted guarantee is:

~~~text
at-most-one automatic send actuation per delivery_id
~~~

with fail-stop behavior when evidence is uncertain.

Avoiding a duplicate send takes priority over unattended progress.

### 11. Crash recovery fails closed after SEND_ARMED

Required recovery semantics:

~~~text
crash before SEND_ARMED
→ automatic retry may be allowed

crash after SEND_ARMED before click
→ no automatic resend

crash after click before confirmation
→ AMBIGUOUS

confirmation observed but local persistence uncertain
→ reconciliation / AMBIGUOUS

SENT_CONFIRMED local but backend not informed
→ replay the status event, never replay the send
~~~

A duplicate WebSocket prompt with identical delivery_id + session + text is idempotent and must not create a second send worker.

### 12. Send confirmation requires targeted evidence

Calling sendButton.click() is not proof.

Nominal confirmation combines:

~~~text
pre-send baseline
+
composer emptied/changed after click
+
new user message appeared after baseline
+
normalized text/hash matches the expected prompt
~~~

For the first prompt of a new session, observing the canonical /c/<conversation-id> URL is additional binding evidence.

The URL by itself is not sufficient proof that the prompt was sent.

### 13. Automatic retry is allowed only before irreversible uncertainty

Automatic retry is permitted only when the implementation can prove that no irreversible send occurred.

There is no automatic retry after SEND_ARMED with unknown outcome or after AMBIGUOUS.

Retryable failures use a bounded retry budget and bounded backoff.

### 14. Busy ChatGPT is WAITING_READY, never an alternate button heuristic

The companion must distinguish the real Send control from Stop/generation controls.

If ChatGPT is already generating, the send remains WAITING_READY until the composer and supported Send control are explicitly ready.

Ambiguous DOM fails closed.

### 15. Protocol v2 carries routing context and durable send-status events

The functional payload remains:

~~~json
{
  "session": "...",
  "text": "..."
}
~~~

The transport must be deliberately versioned before automatic routing. Protocol v2 may carry a routing snapshot:

~~~json
{
  "version": 2,
  "type": "prompt",
  "delivery_id": "...",
  "payload": {
    "session": "...",
    "text": "..."
  },
  "routing": {
    "binding_version": 4,
    "conversation_id": "...",
    "canonical_url": "https://chatgpt.com/c/..."
  }
}
~~~

routing is null for a new AgentSession.

The companion also emits a durable chatgpt_send_status event correlated by:

~~~text
event_id
delivery_id
session
state
attempt
conversation
error_code
~~~

event_id is idempotent and replayed until backend acknowledgement.

A version-1 companion must never be silently treated as compatible with automatic-send v2 semantics.

### 16. The backend projects ChatGptPromptSend separately

Conceptual backend projection:

~~~text
ChatGptPromptSend
-----------------
delivery_id UNIQUE
state
attempt_count
last_error_code
next_retry_at
confirmed_at
updated_at
last_event_id / sequence
~~~

For the first prompt in a new conversation, the backend handles SENT_CONFIRMED and the ConversationBinding upsert atomically from the accepted status event.

### 17. SentPromptStore remains delivery history

The existing SentPromptStore remains historical send context keyed by delivery_id.

It does not become the source of truth for session → conversation. ConversationBinding remains a separate durable concept.

### 18. Response return remains explicit

This decision does not introduce continuous ChatGPT response monitoring, automatic scraping of all responses, automatic response acceptance, or product decisions inferred from ChatGPT content.

listResponseCandidates, PendingResponseStore and the explicit WebSocket return path remain compatible.

After a conversation is reopened, validation may use conversation_id/canonical URL and must not depend forever on one tab_id.

### 19. Stale DEV watchdog starts from confirmed ChatGPT send

ADR-0013 currently uses PromptDelivery.ACKNOWLEDGED as part of its inactivity lower bound because that is the strongest implemented transport fact today.

After DC-063B, the watchdog must instead use the initial ChatGptPromptSend.SENT_CONFIRMED confirmed_at timestamp. It must not declare a DEV stagnant while its prompt is merely ACKed by Firefox but still blocked in ROUTING or WAITING_READY.

Until DC-063B is delivered, the implemented ACK-based watchdog remains the current behavior.

### 20. Attention Center follows send state after DC-063B

The current nominal action:

~~~text
prompt ACKed
→ ACTION: manually send
~~~

disappears after automatic send is implemented.

Target projection:

~~~text
ROUTING / WAITING_READY
→ WATCH or no card

automatic retry scheduled
→ WATCH

SENT_CONFIRMED
→ CLEAR

LOGIN_REQUIRED
DOM_INCOMPATIBLE
BINDING_INVALIDATED
ROUTING_AMBIGUOUS
→ ACTION

AMBIGUOUS
→ priority ACTION
~~~

Attention logic remains backend-owned.

### 21. ADR-0003 is superseded only for the Send gesture

ADR-0003 remains accepted and is superseded in part by this ADR.

The superseded statement is the requirement that a user explicitly chooses/clicks Send for every already-authorized prompt.

The following remain valid:

- PromptDispatch and PromptDelivery are distinct.
- Firefox ACK does not mean sent to ChatGPT.
- delivery_id is stable.
- local durable queue persistence precedes ACK.
- the extension is not product-state authority.
- response return remains explicit.
- there is no continuous response monitoring.
- validation fails closed.

### 22. ADR-0011 human gate remains before PromptDispatch

Architecture-gate authorization remains human and explicit before PromptDispatch creation.

Sending an already-authorized PromptDispatch becomes automatic only after DC-063B.

No browser behavior may create or infer architecture authorization.

## Delivery split

### DC-063A — ConversationBinding and deterministic routing

DC-063A owns:

- ConversationBinding backend persistence and repository/UoW;
- the next explicit Alembic migration;
- protocol-v2 routing snapshot;
- extension binding cache;
- exact tab/conversation router;
- reopening canonical URLs;
- dedicated provisional new-chat tabs;
- invalidation/restart behavior;
- multi-session isolation;
- routing diagnostics.

DC-063A does not auto-send.

For a new AgentSession it may establish:

~~~text
session → dedicated provisional tab
~~~

but not yet:

~~~text
session → durable /c/<conversation-id>
~~~

### DC-063B — automatic send and safe recovery

DC-063B owns:

- automatic trigger;
- per-session FIFO;
- readiness probing and busy-ChatGPT handling;
- SEND_ARMED barrier;
- DOM send;
- send confirmation;
- capture of /c/<id>;
- promotion provisional → ConversationBinding;
- ChatGptPromptSend backend projection;
- durable status-event outbox/replay;
- safe bounded retry;
- AMBIGUOUS recovery;
- Attention Center updates;
- stale DEV watchdog transition to SENT_CONFIRMED;
- SentPromptStore adaptations;
- popup/recovery UI;
- implementation documentation/tests.

## Consequences

- Automatic send cannot bypass product or architecture authorization.
- Browser tab identity is disposable; conversation identity is durable.
- Parallel DEV sessions can route independently while preserving FIFO within each session.
- Unknown post-click outcomes stop automatic progress instead of risking duplicate prompts.
- Protocol v1 remains valid for the currently delivered manual path until the v2 implementation is introduced deliberately.
- The explicit ChatGPT response-return model remains unchanged.


## Amendment — 2026-10-05 — ARCH/ASTRA remains manual to preserve ChatGPT Work mode

The original automatic-send decision is narrowed for the `ARCH` role because ChatGPT's Chat/Work mode is UI state outside DevCockpit's protocol and authority boundary.

Accepted rule:

~~~text
DEV / PO
→ automatic routing + automatic send remain allowed

ARCH / ASTRA
→ PromptDispatch and PromptDelivery remain normal
→ automatic routing/send/recovery/resume forbidden
→ extension displays an explicit manual launch action
→ user selects an active ChatGPT tab already configured in Work mode
→ the manual click executes the existing SEND_ARMED + confirmation pipeline
~~~

No protocol-v2 field is added. The role is already deterministic in `AgentSession = <project>:<role>:<work-item>`.

The extension must not attempt to infer, toggle or persist ChatGPT's Chat/Work UI mode. That remains an explicit human choice for architecture gates. This avoids making brittle DOM state part of DevCockpit's transport contract and prevents ARCH requirements from changing the automatic transport semantics of DEV/PO sessions.
