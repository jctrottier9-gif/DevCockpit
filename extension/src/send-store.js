(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const SENDS_STORAGE_KEY = "chatGptPromptSendsV1";
  const OUTBOX_STORAGE_KEY = "chatGptSendStatusOutboxV1";
  const SEND_STATE = Object.freeze({
    QUEUED: "QUEUED",
    ROUTING: "ROUTING",
    WAITING_READY: "WAITING_READY",
    SEND_ARMED: "SEND_ARMED",
    SENT_CONFIRMED: "SENT_CONFIRMED",
    RETRYABLE_FAILURE: "RETRYABLE_FAILURE",
    BLOCKED: "BLOCKED",
    AMBIGUOUS: "AMBIGUOUS",
  });

  class ChatGptSendStorageError extends Error {
    constructor(message) {
      super(message);
      this.name = "ChatGptSendStorageError";
    }
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function validateState(entry) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.delivery_id !== "string" ||
      typeof entry.session !== "string" ||
      !Object.values(SEND_STATE).includes(entry.state) ||
      !Number.isInteger(entry.attempt) ||
      entry.attempt < 0 ||
      typeof entry.updated_at !== "string"
    ) {
      throw new ChatGptSendStorageError("stored_chatgpt_send_invalid");
    }
    return entry;
  }

  function validateEvent(entry) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.event_id !== "string" ||
      typeof entry.delivery_id !== "string" ||
      typeof entry.session !== "string" ||
      !Object.values(SEND_STATE).includes(entry.state) ||
      !Number.isInteger(entry.attempt) ||
      entry.attempt < 0 ||
      typeof entry.occurred_at !== "string"
    ) {
      throw new ChatGptSendStorageError("stored_chatgpt_send_event_invalid");
    }
    return entry;
  }

  class ChatGptSendStore {
    constructor(storageArea, { now = () => new Date(), uuid = () => crypto.randomUUID() } = {}) {
      if (!storageArea?.get || !storageArea?.set) {
        throw new ChatGptSendStorageError("storage_area_required");
      }
      this.storageArea = storageArea;
      this.now = now;
      this.uuid = uuid;
      this.mutationChain = Promise.resolve();
    }

    async _loadUnsafe() {
      const storedStates = await this.storageArea.get(SENDS_STORAGE_KEY);
      const storedOutbox = await this.storageArea.get(OUTBOX_STORAGE_KEY);
      const states = storedStates?.[SENDS_STORAGE_KEY] ?? [];
      const outbox = storedOutbox?.[OUTBOX_STORAGE_KEY] ?? [];
      if (!Array.isArray(states) || !Array.isArray(outbox)) {
        throw new ChatGptSendStorageError("stored_chatgpt_send_state_invalid");
      }
      return {
        states: states.map((entry) => clone(validateState(entry))),
        outbox: outbox.map((entry) => clone(validateEvent(entry))),
      };
    }

    async _saveUnsafe(states, outbox) {
      await this.storageArea.set({
        [SENDS_STORAGE_KEY]: clone(states),
        [OUTBOX_STORAGE_KEY]: clone(outbox),
      });
    }

    _mutate(task) {
      const run = this.mutationChain.then(task, task);
      this.mutationChain = run.then(() => undefined, () => undefined);
      return run;
    }

    async list() {
      await this.mutationChain;
      return (await this._loadUnsafe()).states;
    }

    async get(deliveryId) {
      const states = await this.list();
      return states.find((entry) => entry.delivery_id === deliveryId) || null;
    }

    async pendingEvents() {
      await this.mutationChain;
      return (await this._loadUnsafe()).outbox;
    }

    async ensureQueued({ deliveryId, session }) {
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const existing = states.find((entry) => entry.delivery_id === deliveryId);
        if (existing) {
          if (existing.session !== session) {
            throw new ChatGptSendStorageError("chatgpt_send_identity_conflict");
          }
          return clone(existing);
        }
        const entry = {
          delivery_id: deliveryId,
          session,
          state: SEND_STATE.QUEUED,
          attempt: 0,
          conversation: null,
          error_code: null,
          next_retry_at: null,
          updated_at: this.now().toISOString(),
        };
        states.push(entry);
        await this._saveUnsafe(states, outbox);
        return clone(entry);
      });
    }

    async transition({
      deliveryId,
      session,
      state,
      attempt,
      conversation = null,
      errorCode = null,
      nextRetryAt = null,
    }) {
      if (!Object.values(SEND_STATE).includes(state)) {
        throw new ChatGptSendStorageError("invalid_chatgpt_send_state");
      }
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const entry = states.find((item) => item.delivery_id === deliveryId);
        if (!entry || entry.session !== session) {
          throw new ChatGptSendStorageError("chatgpt_send_not_found");
        }
        if (
          entry.state === SEND_STATE.SENT_CONFIRMED ||
          entry.state === SEND_STATE.AMBIGUOUS
        ) {
          throw new ChatGptSendStorageError("chatgpt_send_terminal");
        }
        if (
          entry.state === SEND_STATE.SEND_ARMED &&
          ![
            SEND_STATE.SEND_ARMED,
            SEND_STATE.SENT_CONFIRMED,
            SEND_STATE.AMBIGUOUS,
          ].includes(state)
        ) {
          throw new ChatGptSendStorageError("chatgpt_send_fail_stop");
        }
        if (!Number.isInteger(attempt) || attempt < entry.attempt) {
          throw new ChatGptSendStorageError("invalid_chatgpt_send_attempt");
        }

        const occurredAt = this.now().toISOString();
        entry.state = state;
        entry.attempt = attempt;
        entry.conversation = conversation ? clone(conversation) : null;
        entry.error_code = errorCode;
        entry.next_retry_at = nextRetryAt;
        entry.updated_at = occurredAt;

        const event = {
          event_id: this.uuid(),
          delivery_id: deliveryId,
          session,
          state,
          attempt,
          conversation: conversation ? clone(conversation) : null,
          error_code: errorCode,
          next_retry_at: nextRetryAt,
          occurred_at: occurredAt,
        };
        outbox.push(event);
        await this._saveUnsafe(states, outbox);
        return { entry: clone(entry), event: clone(event) };
      });
    }

    async ackEvent(eventId) {
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const next = outbox.filter((entry) => entry.event_id !== eventId);
        if (next.length === outbox.length) {
          return false;
        }
        await this._saveUnsafe(states, next);
        return true;
      });
    }

    async recoverInterruptedArmedSends() {
      const armed = (await this.list()).filter(
        (entry) => entry.state === SEND_STATE.SEND_ARMED,
      );
      const events = [];
      for (const entry of armed) {
        const result = await this.transition({
          deliveryId: entry.delivery_id,
          session: entry.session,
          state: SEND_STATE.AMBIGUOUS,
          attempt: entry.attempt,
          errorCode: "recovered_after_send_armed",
        });
        events.push(result.event);
      }
      return events;
    }

    async retryBlocked(deliveryId) {
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const entry = states.find((item) => item.delivery_id === deliveryId);
        if (!entry || entry.state !== SEND_STATE.BLOCKED) {
          throw new ChatGptSendStorageError("chatgpt_send_not_blocked");
        }
        entry.state = SEND_STATE.QUEUED;
        entry.error_code = null;
        entry.next_retry_at = null;
        entry.updated_at = this.now().toISOString();
        await this._saveUnsafe(states, outbox);
        return clone(entry);
      });
    }
  }

  namespace.sendStore = {
    SENDS_STORAGE_KEY,
    OUTBOX_STORAGE_KEY,
    SEND_STATE,
    ChatGptSendStorageError,
    ChatGptSendStore,
  };
})();
