(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const SENT_PROMPTS_STORAGE_KEY = "sentPromptContextsV1";
  const PENDING_RESPONSES_STORAGE_KEY = "pendingResponsesV1";

  class ResponseStorageError extends Error {
    constructor(message) {
      super(message);
      this.name = "ResponseStorageError";
    }
  }

  class ResponseStorageConflictError extends Error {
    constructor(kind, id) {
      super(kind + "_conflict:" + id);
      this.name = "ResponseStorageConflictError";
      this.kind = kind;
      this.id = id;
    }
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function validateSentContext(entry) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.delivery_id !== "string" ||
      typeof entry.session !== "string" ||
      typeof entry.sent_at !== "string" ||
      !(entry.tab_id === null || Number.isInteger(entry.tab_id)) ||
      !(entry.conversation_url === null || typeof entry.conversation_url === "string")
    ) {
      throw new ResponseStorageError("stored_sent_context_invalid");
    }
    return entry;
  }

  function validatePendingResponse(entry) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.response_id !== "string" ||
      typeof entry.delivery_id !== "string" ||
      typeof entry.session !== "string" ||
      typeof entry.text !== "string" ||
      typeof entry.created_at !== "string" ||
      !(
        entry.last_error === null ||
        entry.last_error === undefined ||
        typeof entry.last_error === "string"
      )
    ) {
      throw new ResponseStorageError("stored_pending_response_invalid");
    }
    return entry;
  }

  class BaseStore {
    constructor(storageArea, storageKey, validator, { now = () => new Date() } = {}) {
      if (!storageArea?.get || !storageArea?.set) {
        throw new ResponseStorageError("storage_area_required");
      }
      this.storageArea = storageArea;
      this.storageKey = storageKey;
      this.validator = validator;
      this.now = now;
      this.mutationChain = Promise.resolve();
    }

    async _loadUnsafe() {
      const stored = await this.storageArea.get(this.storageKey);
      const entries = stored?.[this.storageKey];
      if (entries === undefined) {
        return [];
      }
      if (!Array.isArray(entries)) {
        throw new ResponseStorageError("stored_response_state_invalid");
      }
      return entries.map((entry) => clone(this.validator(entry)));
    }

    async _saveUnsafe(entries) {
      await this.storageArea.set({ [this.storageKey]: clone(entries) });
    }

    _mutate(task) {
      const run = this.mutationChain.then(task, task);
      this.mutationChain = run.then(
        () => undefined,
        () => undefined,
      );
      return run;
    }

    async list() {
      await this.mutationChain;
      return this._loadUnsafe();
    }
  }

  class SentPromptStore extends BaseStore {
    constructor(storageArea, options = {}) {
      super(storageArea, SENT_PROMPTS_STORAGE_KEY, validateSentContext, options);
    }

    async get(deliveryId) {
      const entries = await this.list();
      return entries.find((entry) => entry.delivery_id === deliveryId) || null;
    }

    async recordSent({ deliveryId, session, tabId = null, conversationUrl = null }) {
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const existing = entries.find((entry) => entry.delivery_id === deliveryId);
        if (existing) {
          if (existing.session !== session) {
            throw new ResponseStorageConflictError("sent_prompt", deliveryId);
          }
          return { kind: "duplicate", entry: clone(existing) };
        }

        const entry = {
          delivery_id: deliveryId,
          session,
          sent_at: this.now().toISOString(),
          tab_id: Number.isInteger(tabId) ? tabId : null,
          conversation_url:
            typeof conversationUrl === "string" && conversationUrl ? conversationUrl : null,
        };
        entries.push(entry);
        await this._saveUnsafe(entries);
        return { kind: "recorded", entry: clone(entry) };
      });
    }
  }

  class PendingResponseStore extends BaseStore {
    constructor(storageArea, options = {}) {
      super(storageArea, PENDING_RESPONSES_STORAGE_KEY, validatePendingResponse, options);
    }

    async get(responseId) {
      const entries = await this.list();
      return entries.find((entry) => entry.response_id === responseId) || null;
    }

    async add({ responseId, deliveryId, session, text }) {
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const existing = entries.find((entry) => entry.response_id === responseId);
        if (existing) {
          if (
            existing.delivery_id !== deliveryId ||
            existing.session !== session ||
            existing.text !== text
          ) {
            throw new ResponseStorageConflictError("pending_response", responseId);
          }
          return { kind: "duplicate", entry: clone(existing) };
        }

        const entry = {
          response_id: responseId,
          delivery_id: deliveryId,
          session,
          text,
          created_at: this.now().toISOString(),
          last_error: null,
        };
        entries.push(entry);
        await this._saveUnsafe(entries);
        return { kind: "added", entry: clone(entry) };
      });
    }

    async markError(responseId, errorMessage) {
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const entry = entries.find((item) => item.response_id === responseId);
        if (!entry) {
          throw new ResponseStorageError("response_not_found:" + responseId);
        }
        entry.last_error = errorMessage;
        await this._saveUnsafe(entries);
        return clone(entry);
      });
    }

    async remove(responseId) {
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const nextEntries = entries.filter((item) => item.response_id !== responseId);
        if (nextEntries.length === entries.length) {
          return false;
        }
        await this._saveUnsafe(nextEntries);
        return true;
      });
    }
  }

  namespace.responses = {
    SENT_PROMPTS_STORAGE_KEY,
    PENDING_RESPONSES_STORAGE_KEY,
    ResponseStorageError,
    ResponseStorageConflictError,
    SentPromptStore,
    PendingResponseStore,
  };
})();
