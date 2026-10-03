# ADR-0011 — DEV GitHub handoff and explicit human authorization of architecture gates

- Status: Accepted
- Date: 2026-10-02

## Context

DevCockpit already derives execution state from GitHub, observes PR/CI evidence, prepares DEV follow-up prompts for current-head CI failures, and keeps GitHub plus the canonical roadmap authoritative.

The previous agent lifecycle nevertheless asked a DEV agent to remain active through CI polling, CI repair, merge and roadmap progression. That duplicates orchestration work already owned by DevCockpit and wastes an active agent turn while external systems are simply running.

The deterministic scheduler also exposes READY `ARCHITECTURE_GATE` items with the expected ARCH role. Eligibility alone must not allow the background poller to launch architecture work: architecture analysis is a deliberate human gate.

## Decision

### DEV handoff

A normal DEV turn ends when:

1. the authorized implementation scope is complete;
2. relevant local validation has been performed;
3. the PR is created or updated;
4. auto-merge is enabled when repository rules permit it;
5. the DEV reports the PR number and current head SHA.

The DEV then stops. DevCockpit observes GitHub.

- `CI_RUNNING`: no agent prompt is required.
- `CI_RED`: DevCockpit may prepare a corrective prompt automatically for the same logical DEV session and immutable failure evidence.
- after the correction is pushed, the DEV stops again rather than polling.
- `CI_GREEN` with auto-merge armed: wait for GitHub.
- `MERGED`: DevCockpit surfaces reconciliation; it does not invent completion or silently cross roadmap gates.

This changes the active-agent stop point, not the Definition of Done. GitHub and the canonical roadmap still determine whether the WorkItem is actually complete.

### Architecture gates

A READY `ARCHITECTURE_GATE` is eligible but not authorized.

DevCockpit may:

- parse it;
- show it in the scheduler;
- surface an Attention Center action requiring authorization.

DevCockpit must not create the gate's ARCH `PromptDispatch` from polling, refresh, dependency satisfaction, CI state, merge state or roadmap state alone.

The only supported creation path is an explicit human authorization command from the cockpit. The authorization command:

- requires an explicit confirmation payload;
- revalidates the current canonical roadmap and dependencies;
- creates the ARCH prompt idempotently;
- records authorization durably through the resulting PromptDispatch identity.

Under the currently delivered companion, sending that prepared prompt into ChatGPT remains a separate explicit user action at the Firefox companion boundary. ADR-0014 accepts automatic routing/send of an already-authorized PromptDispatch after DC-063B; that later transport automation does not change this authorization boundary.

## Consequences

- Agent time is spent on actionable work rather than waiting for GitHub.
- CI failures continue to route automatically to the same DEV session.
- Architecture gates cannot accidentally launch because a scheduler row becomes READY.
- The Attention Center becomes the visible human gate for architecture work.
- A scheduler `START_ARCH` projection is an eligibility signal only; it is not permission for background execution.
- Roadmap Safe Writeback remains separately protected by its existing preview/confirmation rules.

## Rejected alternatives

### Let DEV poll CI until merge

Rejected because it duplicates deterministic orchestration and keeps an agent active while no agent decision is required.

### Auto-launch every READY architecture gate

Rejected because READY expresses canonical ordering/eligibility, not human authorization to spend an Architect turn or start a consequential analysis.

### Treat Firefox send confirmation as architecture authorization

Rejected because the authorization boundary belongs to DevCockpit before the ARCH PromptDispatch exists; the companion remains a thin transport/UI adapter.


## ASTRA-063 amendment — authorization and sending are separate gates

ADR-0014 makes the distinction explicit:

~~~text
architecture gate authorization
= always human before PromptDispatch

sending an already-authorized PromptDispatch
= automatic only after DC-063B
~~~

A READY architecture gate therefore remains only eligible. Polling, dependency satisfaction, scheduler state, CI/merge evidence, browser state and ChatGPT state cannot create its ARCH PromptDispatch.

Once the human authorization command has created that PromptDispatch, DC-063B may automate the transport gesture without asking for a second Send confirmation. The browser still cannot infer, create or broaden ARCH authorization.
