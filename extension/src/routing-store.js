(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const STORAGE_KEY = "conversationRoutingV1";
  const ROUTING_KIND = Object.freeze({
    BOUND: "BOUND",
    PROVISIONAL: "PROVISIONAL",
    INVALIDATED: "INVALIDATED",
  });

  class RoutingStorageError extends Error {
    constructor(message) {
      super(message);
      this.name = "RoutingStorageError";
    }
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function validateEntry(entry) {
    if (
      !entry ||
      typeof entry !== "object" ||
      typeof entry.session !== "string" ||
      entry.session.trim() === "" ||
      !Object.values(ROUTING_KIND).includes(entry.kind) ||
      !(entry.tab_id === null || Number.isInteger(entry.tab_id)) ||
      typeof entry.updated_at !== "string" ||
      !(
        entry.last_error === null ||
        entry.last_error === undefined ||
        typeof entry.last_error === "string"
      )
    ) {
      throw new RoutingStorageError("stored_routing_invalid");
    }
    if (entry.kind === ROUTING_KIND.BOUND) {
      if (
        !Number.isInteger(entry.binding_version) ||
        entry.binding_version < 1 ||
        typeof entry.conversation_id !== "string" ||
        entry.conversation_id === "" ||
        typeof entry.canonical_url !== "string" ||
        entry.canonical_url === ""
      ) {
        throw new RoutingStorageError("stored_bound_routing_invalid");
      }
    }
    if (
      entry.kind === ROUTING_KIND.INVALIDATED &&
      (typeof entry.last_error !== "string" || entry.last_error.trim() === "")
    ) {
      throw new RoutingStorageError("stored_invalidated_routing_invalid");
    }
    return entry;
  }

  class ConversationRoutingStore {
    constructor(storageArea, { now = () => new Date() } = {}) {
      if (!storageArea?.get || !storageArea?.set) {
        throw new RoutingStorageError("storage_area_required");
      }
      this.storageArea = storageArea;
      this.now = now;
      this.mutationChain = Promise.resolve();
    }

    async _loadUnsafe() {
      const stored = await this.storageArea.get(STORAGE_KEY);
      const entries = stored?.[STORAGE_KEY];
      if (entries === undefined) {
        return [];
      }
      if (!Array.isArray(entries)) {
        throw new RoutingStorageError("stored_routing_invalid");
      }
      return entries.map((entry) => clone(validateEntry(entry)));
    }

    async _saveUnsafe(entries) {
      await this.storageArea.set({ [STORAGE_KEY]: clone(entries) });
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

    async get(session) {
      const entries = await this.list();
      return entries.find((entry) => entry.session === session) || null;
    }

    async setBound({ session, routing, tabId = null }) {
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const existing = entries.find((entry) => entry.session === session);
        if (
          existing?.kind === ROUTING_KIND.BOUND &&
          existing.binding_version > routing.binding_version
        ) {
          throw new RoutingStorageError("stale_binding_snapshot");
        }
        if (
          existing?.kind === ROUTING_KIND.BOUND &&
          existing.binding_version === routing.binding_version &&
          (
            existing.conversation_id !== routing.conversation_id ||
            existing.canonical_url !== routing.canonical_url
          )
        ) {
          throw new RoutingStorageError("binding_snapshot_conflict");
        }
        const next = {
          session,
          kind: ROUTING_KIND.BOUND,
          tab_id: Number.isInteger(tabId) ? tabId : null,
          binding_version: routing.binding_version,
          conversation_id: routing.conversation_id,
          canonical_url: routing.canonical_url,
          updated_at: this.now().toISOString(),
          last_error: null,
        };
        if (existing) {
          Object.assign(existing, next);
        } else {
          entries.push(next);
        }
        await this._saveUnsafe(entries);
        return clone(next);
      });
    }

    async setProvisional(session, tabId) {
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const existing = entries.find((entry) => entry.session === session);
        if (existing?.kind === ROUTING_KIND.BOUND) {
          throw new RoutingStorageError("backend_binding_missing_for_cached_bound_session");
        }
        if (existing?.kind === ROUTING_KIND.INVALIDATED) {
          throw new RoutingStorageError("binding_invalidated");
        }
        const next = {
          session,
          kind: ROUTING_KIND.PROVISIONAL,
          tab_id: Number.isInteger(tabId) ? tabId : null,
          binding_version: null,
          conversation_id: null,
          canonical_url: null,
          updated_at: this.now().toISOString(),
          last_error: null,
        };
        if (existing) {
          Object.assign(existing, next);
        } else {
          entries.push(next);
        }
        await this._saveUnsafe(entries);
        return clone(next);
      });
    }

    async markInvalidated(session, reason) {
      if (typeof reason !== "string" || reason.trim() === "") {
        throw new RoutingStorageError("invalidation_reason_required");
      }
      return this._mutate(async () => {
        const entries = await this._loadUnsafe();
        const existing = entries.find((entry) => entry.session === session);
        const next = {
          session,
          kind: ROUTING_KIND.INVALIDATED,
          tab_id: existing?.tab_id ?? null,
          binding_version: existing?.binding_version ?? null,
          conversation_id: existing?.conversation_id ?? null,
          canonical_url: existing?.canonical_url ?? null,
          updated_at: this.now().toISOString(),
          last_error: reason,
        };
        if (existing) {
          Object.assign(existing, next);
        } else {
          entries.push(next);
        }
        await this._saveUnsafe(entries);
        return clone(next);
      });
    }
  }

  namespace.routingStore = {
    STORAGE_KEY,
    ROUTING_KIND,
    RoutingStorageError,
    ConversationRoutingStore,
  };
})();
