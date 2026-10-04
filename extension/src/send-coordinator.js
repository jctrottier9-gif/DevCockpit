(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const { SEND_STATE } = namespace.sendStore;

  const DEFAULT_RETRY_DELAYS_MS = Object.freeze([250, 750, 1500, 2500, 4000]);
  const RETRYABLE_READY_ERRORS = new Set([
    "composer_not_found",
    "send_button_not_found",
    "send_button_disabled",
    "content_script_unavailable",
  ]);

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  function preSendErrorCode(error) {
    const message = errorText(error);
    if (/receiving end does not exist/i.test(message)) {
      return "content_script_unavailable";
    }
    return message;
  }

  function canonicalConversation(rawUrl) {
    if (typeof rawUrl !== "string" || !rawUrl) return null;
    try {
      const parsed = new URL(rawUrl);
      if (!["chatgpt.com", "chat.openai.com"].includes(parsed.hostname.toLowerCase())) {
        return null;
      }
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (
        parts.length < 2 ||
        parts[parts.length - 2] !== "c" ||
        !parts[parts.length - 1]
      ) {
        return null;
      }
      const conversationId = parts[parts.length - 1];
      return {
        conversation_id: conversationId,
        canonical_url: "https://chatgpt.com/c/" + conversationId,
      };
    } catch {
      return null;
    }
  }

  class PromptSendCoordinator {
    constructor({
      queueStore,
      sentPromptStore,
      sendStore,
      router,
      sendToTab,
      emitStatus = () => false,
      sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
      retryDelaysMs = DEFAULT_RETRY_DELAYS_MS,
    }) {
      this.queueStore = queueStore;
      this.sentPromptStore = sentPromptStore;
      this.sendStore = sendStore;
      this.router = router;
      this.sendToTab = sendToTab;
      this.emitStatus = emitStatus;
      this.sleep = sleep;
      this.retryDelaysMs = retryDelaysMs;
      this.deliveryWorkers = new Map();
      this.sessionChains = new Map();
      this.enqueueSetupChain = Promise.resolve();
    }

    async _emit(result) {
      try {
        this.emitStatus(result.event);
      } catch {
        // Durable outbox is replayed by the transport.
      }
      return result;
    }

    async _queueEntry(deliveryId) {
      const queue = await this.queueStore.list();
      return queue.find((entry) => entry.delivery_id === deliveryId) || null;
    }

    async _hasEarlierUnfinished(entry) {
      const queue = await this.queueStore.list();
      for (const candidate of queue) {
        if (candidate.delivery_id === entry.delivery_id) return false;
        if (candidate.session === entry.session) {
          const state = await this.sendStore.get(candidate.delivery_id);
          if (!state || state.state !== SEND_STATE.SENT_CONFIRMED) return true;
        }
      }
      return false;
    }

    enqueue(deliveryId) {
      const existingWorker = this.deliveryWorkers.get(deliveryId);
      if (existingWorker) return existingWorker;

      const setup = this.enqueueSetupChain.then(async () => {
        const entry = await this._queueEntry(deliveryId);
        if (!entry) {
          return {
            run: Promise.resolve({
              ok: false,
              error: "delivery_not_found:" + deliveryId,
            }),
          };
        }

        const prior = this.sessionChains.get(entry.session) || Promise.resolve();
        const run = prior.then(() => this._run(entry), () => this._run(entry));
        this.sessionChains.set(
          entry.session,
          run.then(() => undefined, () => undefined),
        );
        return { run };
      });
      this.enqueueSetupChain = setup.then(() => undefined, () => undefined);

      const worker = setup
        .then(({ run }) => run)
        .finally(() => {
          this.deliveryWorkers.delete(deliveryId);
        });
      this.deliveryWorkers.set(deliveryId, worker);
      return worker;
    }

    send(deliveryId) {
      return this.enqueue(deliveryId);
    }

    async resumeSession(session) {
      if (typeof session !== "string" || session.trim() === "") {
        return { ok: false, error: "invalid_session" };
      }
      const queue = await this.queueStore.list();
      const entries = queue.filter((entry) => entry.session === session);
      for (const entry of entries) {
        const state = await this.sendStore.get(entry.delivery_id);
        if (!state) {
          return {
            ok: false,
            error: "chatgpt_send_not_found:" + entry.delivery_id,
          };
        }
        if (state.state === SEND_STATE.SENT_CONFIRMED) {
          continue;
        }
        if (
          [
            SEND_STATE.QUEUED,
            SEND_STATE.ROUTING,
            SEND_STATE.WAITING_READY,
            SEND_STATE.RETRYABLE_FAILURE,
          ].includes(state.state)
        ) {
          return this.enqueue(entry.delivery_id);
        }
        return {
          ok: false,
          state: state.state,
          error: "session_head_not_sendable",
        };
      }
      return { ok: true, state: "EMPTY" };
    }

    async _run(entry) {
      const existing = await this.sendStore.get(entry.delivery_id);
      if (!existing) return { ok: false, error: "chatgpt_send_not_found" };
      if (
        [SEND_STATE.SEND_ARMED, SEND_STATE.SENT_CONFIRMED, SEND_STATE.AMBIGUOUS].includes(
          existing.state,
        )
      ) {
        return {
          ok: existing.state === SEND_STATE.SENT_CONFIRMED,
          state: existing.state,
        };
      }
      if (existing.state === SEND_STATE.BLOCKED) {
        return { ok: false, state: SEND_STATE.BLOCKED, error: existing.error_code };
      }
      if (await this._hasEarlierUnfinished(entry)) {
        return {
          ok: false,
          state: "WAITING_FIFO",
          error: "session_predecessor_not_complete",
        };
      }

      let attempt = existing.attempt;
      let target = null;
      let prepared = null;
      let tab = null;

      for (let index = 0; index <= this.retryDelaysMs.length; index += 1) {
        attempt += 1;
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.ROUTING,
            attempt,
          }),
        );

        try {
          target = await this.router.route({
            session: entry.session,
            routing: entry.routing,
          });
          tab = await this.router.revalidateTarget({
            session: entry.session,
            routing: entry.routing,
            tabId: target.tabId,
          });
          prepared = await this.sendToTab(tab.id, {
            type: "devcockpit_prepare_prompt",
            text: entry.text,
          });
        } catch (error) {
          prepared = { ok: false, error: preSendErrorCode(error) };
        }

        if (prepared?.ok === true) break;

        const code = prepared?.error || "chatgpt_prepare_failed";
        const retryable =
          RETRYABLE_READY_ERRORS.has(code) ||
          code.startsWith("composer_not_found:");
        if (!retryable) {
          await this._emit(
            await this.sendStore.transition({
              deliveryId: entry.delivery_id,
              session: entry.session,
              state: SEND_STATE.BLOCKED,
              attempt,
              errorCode: code,
            }),
          );
          return { ok: false, state: SEND_STATE.BLOCKED, error: code };
        }

        const hasRetry = index < this.retryDelaysMs.length;
        const nextRetryAt = hasRetry
          ? new Date(Date.now() + this.retryDelaysMs[index]).toISOString()
          : null;
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: hasRetry ? SEND_STATE.WAITING_READY : SEND_STATE.BLOCKED,
            attempt,
            errorCode: code,
            nextRetryAt,
          }),
        );
        if (!hasRetry) {
          return { ok: false, state: SEND_STATE.BLOCKED, error: code };
        }
        await this.sleep(this.retryDelaysMs[index]);
      }

      try {
        tab = await this.router.revalidateTarget({
          session: entry.session,
          routing: entry.routing,
          tabId: target.tabId,
        });
      } catch (error) {
        const code = errorText(error);
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.BLOCKED,
            attempt,
            errorCode: code,
          }),
        );
        return { ok: false, state: SEND_STATE.BLOCKED, error: code };
      }

      await this._emit(
        await this.sendStore.transition({
          deliveryId: entry.delivery_id,
          session: entry.session,
          state: SEND_STATE.SEND_ARMED,
          attempt,
        }),
      );

      let result;
      try {
        result = await this.sendToTab(tab.id, {
          type: "devcockpit_commit_prepared_prompt",
          text: entry.text,
          baseline: prepared.baseline,
        });
      } catch (error) {
        result = { ok: false, error: errorText(error), ambiguous: true };
      }

      if (!result?.ok) {
        const code = result?.error || "send_confirmation_unknown";
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.AMBIGUOUS,
            attempt,
            errorCode: code,
          }),
        );
        return { ok: false, state: SEND_STATE.AMBIGUOUS, error: code };
      }

      const conversation =
        canonicalConversation(result.conversationUrl) ||
        (entry.routing
          ? {
              conversation_id: entry.routing.conversation_id,
              canonical_url: entry.routing.canonical_url,
            }
          : null);
      if (!conversation) {
        await this._emit(
          await this.sendStore.transition({
            deliveryId: entry.delivery_id,
            session: entry.session,
            state: SEND_STATE.AMBIGUOUS,
            attempt,
            errorCode: "confirmed_send_missing_conversation_identity",
          }),
        );
        return {
          ok: false,
          state: SEND_STATE.AMBIGUOUS,
          error: "confirmed_send_missing_conversation_identity",
        };
      }

      const confirmed = await this.sendStore.transition({
        deliveryId: entry.delivery_id,
        session: entry.session,
        state: SEND_STATE.SENT_CONFIRMED,
        attempt,
        conversation,
      });
      await this._emit(confirmed);

      try {
        await this.sentPromptStore.recordSent({
          deliveryId: entry.delivery_id,
          session: entry.session,
          tabId: tab.id,
          conversationUrl: conversation.canonical_url,
        });
        await this.queueStore.remove(entry.delivery_id);
      } catch {
        // SENT_CONFIRMED is already durable; never replay the irreversible send.
      }
      void this.resumeSession(entry.session);
      return { ok: true, state: SEND_STATE.SENT_CONFIRMED, conversation };
    }
  }

  namespace.send = {
    DEFAULT_RETRY_DELAYS_MS,
    preSendErrorCode,
    canonicalConversation,
    PromptSendCoordinator,
  };
})();
