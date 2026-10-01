(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const STORAGE_KEY = "promptQueueV1";
  const LOCAL_STATUS = Object.freeze({
    QUEUED: "QUEUED",
    SEND_REQUESTED: "SEND_REQUESTED",
  });

  class QueueStorageError extends Error {
    constructor(message) {
      super(message);
      this.name = "QueueStorageError";
    }
  }

  class DeliveryConflictError extends Error {
    constructor(deliveryId) {
      super(`delivery_conflict:${deliveryId}`);
      this.name = "DeliveryConflictError";
      this.deliveryId = deliveryId;
    }
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function validateEntry(entry) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.delivery_id !== "string" ||
      typeof entry.session !== "string" ||
      typeof entry.text !== "string" ||
      typeof entry.received_at !== "string" ||
      !Object.values(LOCAL_STATUS).includes(entry.local_status) ||
      !(
        entry.last_error === null ||
        entry.last_error === undefined ||
        typeof entry.last_error === "string"
      )
    ) {
      throw new QueueStorageError("stored_queue_invalid");
    }
    return entry;
  }

  class QueueStore {
    constructor(storageArea, { now = () => new Date() } = {}) {
      if (!storageArea?.get || !storageArea?.set) {
        throw new QueueStorageError("storage_area_required");
      }
      this.storageArea = storageArea;
      this.now = now;
      this.mutationChain = Promise.resolve();
    }

    async _loadUnsafe() {
      const stored = await this.storageArea.get(STORAGE_KEY);
      const queue = stored?.[STORAGE_KEY];
      if (queue === undefined) {
        return [];
      }
      if (!Array.isArray(queue)) {
        throw new QueueStorageError("stored_queue_invalid");
      }
      return queue.map((entry) => clone(validateEntry(entry)));
    }

    async _saveUnsafe(queue) {
      await this.storageArea.set({ [STORAGE_KEY]: clone(queue) });
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

    async acceptPrompt({ deliveryId, session, text }) {
      return this._mutate(async () => {
        const queue = await this._loadUnsafe();
        const existing = queue.find((entry) => entry.delivery_id === deliveryId);
        if (existing) {
          if (existing.session !== session || existing.text !== text) {
            throw new DeliveryConflictError(deliveryId);
          }
          return { kind: "duplicate", entry: clone(existing) };
        }

        const entry = {
          delivery_id: deliveryId,
          session,
          text,
          received_at: this.now().toISOString(),
          local_status: LOCAL_STATUS.QUEUED,
          last_error: null,
        };
        queue.push(entry);
        await this._saveUnsafe(queue);
        return { kind: "accepted", entry: clone(entry) };
      });
    }

    async markSendRequested(deliveryId) {
      return this._mutate(async () => {
        const queue = await this._loadUnsafe();
        const entry = queue.find((item) => item.delivery_id === deliveryId);
        if (!entry) {
          throw new QueueStorageError(`delivery_not_found:${deliveryId}`);
        }
        entry.local_status = LOCAL_STATUS.SEND_REQUESTED;
        entry.last_error = null;
        await this._saveUnsafe(queue);
        return clone(entry);
      });
    }

    async markQueued(deliveryId, errorMessage = null) {
      return this._mutate(async () => {
        const queue = await this._loadUnsafe();
        const entry = queue.find((item) => item.delivery_id === deliveryId);
        if (!entry) {
          throw new QueueStorageError(`delivery_not_found:${deliveryId}`);
        }
        entry.local_status = LOCAL_STATUS.QUEUED;
        entry.last_error = errorMessage;
        await this._saveUnsafe(queue);
        return clone(entry);
      });
    }

    async remove(deliveryId) {
      return this._mutate(async () => {
        const queue = await this._loadUnsafe();
        const nextQueue = queue.filter((item) => item.delivery_id !== deliveryId);
        if (nextQueue.length === queue.length) {
          throw new QueueStorageError(`delivery_not_found:${deliveryId}`);
        }
        await this._saveUnsafe(nextQueue);
      });
    }
  }

  namespace.queue = {
    STORAGE_KEY,
    LOCAL_STATUS,
    QueueStorageError,
    DeliveryConflictError,
    QueueStore,
  };
})();
