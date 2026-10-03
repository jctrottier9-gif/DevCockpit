# ADR-0013 — Stale DEV watchdog and bounded ChatGPT retry

- Status: Accepted
- Date: 2026-10-03

## Context

DevCockpit intentionally stops active DEV turns after a PR/auto-merge handoff and resumes them only when GitHub evidence requires more work. A different failure mode exists before a PR is created: a DEV conversation can be interrupted while a work branch remains ahead of `main` but receives no new commits for hours.

The Firefox companion can also observe an explicit ChatGPT transport failure such as `Message delivery timed out` or `Connection interrupted`. Retrying that failed turn is different from sending a new prompt and should not duplicate logical work.

## Decision

### DevCockpit stale-DEV watchdog

DevCockpit derives branch activity from strongly associated GitHub branch evidence. For a WorkItem in `DEVELOPING`, it may prepare one same-session watchdog follow-up when all of the following are true:

- the canonical WorkItem is executable DEV work;
- a single strongly associated branch is ahead of `main`;
- GitHub exposes the last commit activity timestamp for that branch;
- the initial DEV PromptDispatch already exists;
- until DC-063B, its PromptDelivery has been ACKNOWLEDGED by Firefox;
- until DC-063B, the newest of branch activity, initial prompt creation and Firefox acknowledgement is older than `DEVCOCKPIT_DEV_STALE_AFTER_SECONDS`.

The default inactivity threshold is 3600 seconds.

The watchdog follow-up reuses the same `<project>:DEV:<work-item>` AgentSession, tells the DEV to inspect and resume the existing branch, must not restart the slice from zero, must not create a competing PR when an existing PR appears, stops again at the normal PR/auto-merge handoff, and never starts the next WorkItem.

Its idempotency key is bound to the project, WorkItem, branch name, branch head SHA and GitHub last-activity timestamp. Re-polling the same stagnant evidence cannot create duplicate logical watchdog prompts. New branch activity changes the evidence identity and resets the watchdog window.

If GitHub cannot provide branch activity time, the watchdog fails closed and does not create a recovery prompt.

### Firefox bounded retry

The Firefox companion may automatically click ChatGPT's explicit retry control only while tracking the response to a prompt that the user already chose to send.

A retry is allowed only when the current tracked turn contains an explicit failure signal such as timeout, interrupted connection or try-again/retry UI. The extension must retry the failed turn rather than resubmit the prompt as a new message, cap retries at three attempts, apply backoff between attempts, expose the retry state in the popup, stop with `ERROR` after the retry budget is exhausted, and never turn the retry loop into general background conversation scraping.

## Consequences

Interrupted DEV work can recover without waiting many hours for manual branch inspection. Stale branch age alone cannot immediately trigger a watchdog right after a fresh prompt delivery. Each stagnant GitHub head produces at most one logical watchdog follow-up. Explicit ChatGPT transport failures can self-recover without duplicate prompts. GitHub remains authoritative for software-delivery progress.


## ASTRA-063 amendment — stale timer moves to confirmed ChatGPT send

ADR-0014 accepts a stronger browser-to-ChatGPT send state. Once DC-063B is delivered, PromptDelivery.ACKNOWLEDGED is no longer the lower bound for stale-DEV timing because it proves only that Firefox persisted the prompt.

The post-DC-063B rule is:

~~~text
initial ChatGptPromptSend == SENT_CONFIRMED
and
reference time includes ChatGptPromptSend.confirmed_at
~~~

ROUTING, WAITING_READY, RETRYABLE_FAILURE or a merely ACKNOWLEDGED PromptDelivery must not start the DEV inactivity window. This prevents the watchdog from declaring a DEV stagnant while the prompt is still blocked before ChatGPT actually receives it.

Until DC-063B implements ChatGptPromptSend, the existing ACK-based watchdog remains the delivered behavior. The migration to confirmed_at belongs to DC-063B and must preserve the existing same-session/idempotency guarantees.
