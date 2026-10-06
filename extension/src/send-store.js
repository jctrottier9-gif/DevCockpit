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
  const RECOVERY_STATUS = Object.freeze({
    PENDING: "PENDING",
    INSPECTING: "INSPECTING",
    RELOADING: "RELOADING",
    WAITING_CONTENT: "WAITING_CONTENT",
    CONFIRMED: "CONFIRMED",
    VERIFIED_NOT_SENT: "VERIFIED_NOT_SENT",
    INTERVENTION_REQUIRED: "INTERVENTION_REQUIRED",
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

  function newRecovery(timestamp) {
    return {
      status: RECOVERY_STATUS.PENDING,
      reload_attempted: false,
      reload_attempted_at: null,
      last_error: null,
      binding_conflict: null,
      updated_at: timestamp,
    };
  }

  function validateBindingConflict(value) {
    if (value === null || value === undefined) return value;
    if (
      !value ||
      typeof value !== "object" ||
      typeof value.agent_session !== "string" ||
      value.agent_session.trim() === "" ||
      !(value.expected_conversation_id === null || typeof value.expected_conversation_id === "string") ||
      !(value.expected_canonical_url === null || typeof value.expected_canonical_url === "string") ||
      !(
        value.expected_binding_version === null ||
        (Number.isInteger(value.expected_binding_version) && value.expected_binding_version >= 1)
      ) ||
      !(value.observed_conversation_id === null || typeof value.observed_conversation_id === "string") ||
      !(value.observed_canonical_url === null || typeof value.observed_canonical_url === "string") ||
      !(
        value.observed_binding_version === null ||
        (Number.isInteger(value.observed_binding_version) && value.observed_binding_version >= 1)
      )
    ) {
      throw new ChatGptSendStorageError("stored_binding_conflict_invalid");
    }
    return value;
  }

  function validateRecovery(value) {
    if (value === null || value === undefined) return value;
    if (
      !value ||
      typeof value !== "object" ||
      !Object.values(RECOVERY_STATUS).includes(value.status) ||
      typeof value.reload_attempted !== "boolean" ||
      !(value.reload_attempted_at === null || typeof value.reload_attempted_at === "string") ||
      !(value.last_error === null || typeof value.last_error === "string") ||
      typeof value.updated_at !== "string"
    ) {
      throw new ChatGptSendStorageError("stored_chatgpt_recovery_invalid");
    }
    validateBindingConflict(value.binding_conflict);
    return value;
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
    validateRecovery(entry.recovery);
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
          recovery: null,
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
        if (state === SEND_STATE.AMBIGUOUS && !entry.recovery) {
          entry.recovery = newRecovery(occurredAt);
        }
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

    async setRecoveryStatus(
      deliveryId,
      {
        status,
        errorCode = null,
        bindingConflict = null,
      },
    ) {
      if (!Object.values(RECOVERY_STATUS).includes(status)) {
        throw new ChatGptSendStorageError("invalid_chatgpt_recovery_status");
      }
      validateBindingConflict(bindingConflict);
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const entry = states.find((item) => item.delivery_id === deliveryId);
        if (!entry || entry.state !== SEND_STATE.AMBIGUOUS) {
          throw new ChatGptSendStorageError("chatgpt_send_not_ambiguous");
        }
        const occurredAt = this.now().toISOString();
        entry.recovery ||= newRecovery(occurredAt);
        entry.recovery.status = status;
        entry.recovery.last_error = errorCode;
        entry.recovery.binding_conflict = bindingConflict
          ? clone(bindingConflict)
          : null;
        entry.recovery.updated_at = occurredAt;
        if (errorCode) entry.error_code = errorCode;
        entry.updated_at = occurredAt;
        await this._saveUnsafe(states, outbox);
        return clone(entry);
      });
    }

    async markRecoveryReloadAttempted(deliveryId) {
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const entry = states.find((item) => item.delivery_id === deliveryId);
        if (!entry || entry.state !== SEND_STATE.AMBIGUOUS) {
          throw new ChatGptSendStorageError("chatgpt_send_not_ambiguous");
        }
        const occurredAt = this.now().toISOString();
        entry.recovery ||= newRecovery(occurredAt);
        if (entry.recovery.reload_attempted) {
          return { started: false, entry: clone(entry) };
        }
        entry.recovery.reload_attempted = true;
        entry.recovery.reload_attempted_at = occurredAt;
        entry.recovery.status = RECOVERY_STATUS.RELOADING;
        entry.recovery.last_error = null;
        entry.recovery.binding_conflict = null;
        entry.recovery.updated_at = occurredAt;
        entry.updated_at = occurredAt;
        await this._saveUnsafe(states, outbox);
        return { started: true, entry: clone(entry) };
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

    async resolveAmbiguous({
      deliveryId,
      state,
      conversation = null,
      errorCode = null,
    }) {
      if (![SEND_STATE.BLOCKED, SEND_STATE.SENT_CONFIRMED].includes(state)) {
        throw new ChatGptSendStorageError("invalid_ambiguous_resolution_state");
      }
      return this._mutate(async () => {
        const { states, outbox } = await this._loadUnsafe();
        const entry = states.find((item) => item.delivery_id === deliveryId);
        if (!entry || entry.state !== SEND_STATE.AMBIGUOUS) {
          throw new ChatGptSendStorageError("chatgpt_send_not_ambiguous");
        }
        const occurredAt = this.now().toISOString();
        entry.state = state;
        entry.conversation = conversation ? clone(conversation) : null;
        entry.error_code = errorCode;
        entry.next_retry_at = null;
        entry.updated_at = occurredAt;

        const event = {
          event_id: this.uuid(),
          delivery_id: entry.delivery_id,
          session: entry.session,
          state,
          attempt: entry.attempt,
          conversation: conversation ? clone(conversation) : null,
          error_code: errorCode,
          next_retry_at: null,
          occurred_at: occurredAt,
        };
        outbox.push(event);
        await this._saveUnsafe(states, outbox);
        return { entry: clone(entry), event: clone(event) };
      });
    }
  }

  namespace.sendStore = {
    SENDS_STORAGE_KEY,
    OUTBOX_STORAGE_KEY,
    SEND_STATE,
    RECOVERY_STATUS,
    ChatGptSendStorageError,
    ChatGptSendStore,
  };
})();
