(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const { ROUTING_KIND } = namespace.routingStore;
  const NEW_CHAT_URL = "https://chatgpt.com/";

  class RoutingError extends Error {
    constructor(code) {
      super(code);
      this.name = "RoutingError";
      this.code = code;
    }
  }

  function isSupportedChatGptUrl(rawUrl) {
    if (typeof rawUrl !== "string") {
      return false;
    }
    try {
      const url = new URL(rawUrl);
      return (
        url.protocol === "https:" &&
        (url.hostname === "chatgpt.com" || url.hostname === "chat.openai.com")
      );
    } catch {
      return false;
    }
  }

  function isNewChatUrl(rawUrl) {
    if (!isSupportedChatGptUrl(rawUrl)) {
      return false;
    }
    const url = new URL(rawUrl);
    return url.pathname === "/" || url.pathname === "";
  }

  function conversationIdentity(rawUrl) {
    if (!isSupportedChatGptUrl(rawUrl)) {
      return null;
    }
    const url = new URL(rawUrl);
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts.length !== 2 || parts[0] !== "c" || !parts[1]) {
      return null;
    }
    return {
      conversationId: parts[1],
      canonicalUrl: "https://chatgpt.com/c/" + parts[1],
    };
  }

  function normalizeRouting(routing) {
    if (
      !routing ||
      typeof routing !== "object" ||
      !Number.isInteger(routing.binding_version) ||
      routing.binding_version < 1 ||
      typeof routing.conversation_id !== "string" ||
      routing.conversation_id === "" ||
      typeof routing.canonical_url !== "string"
    ) {
      throw new RoutingError("invalid_routing_snapshot");
    }
    if (routing.state === "INVALIDATED") {
      throw new RoutingError("binding_invalidated");
    }
    const identity = conversationIdentity(routing.canonical_url);
    if (!identity || identity.conversationId !== routing.conversation_id) {
      throw new RoutingError("invalid_routing_snapshot");
    }
    return {
      binding_version: routing.binding_version,
      conversation_id: routing.conversation_id,
      canonical_url: identity.canonicalUrl,
    };
  }

  function tabIdentityMatches(tab, routing) {
    if (!Number.isInteger(tab?.id)) {
      return false;
    }
    const identity = conversationIdentity(tab.url);
    return Boolean(
      identity &&
      (
        identity.conversationId === routing.conversation_id ||
        identity.canonicalUrl === routing.canonical_url
      )
    );
  }

  class ConversationRouter {
    constructor({ routingStore, queryTabs, createTab }) {
      this.routingStore = routingStore;
      this.queryTabs = queryTabs;
      this.createTab = createTab;
      this.sessionChains = new Map();
    }

    _serialize(session, task) {
      const previous = this.sessionChains.get(session) || Promise.resolve();
      const current = previous.then(task, task);
      const tracked = current.finally(() => {
        if (this.sessionChains.get(session) === tracked) {
          this.sessionChains.delete(session);
        }
      });
      this.sessionChains.set(session, tracked);
      return current;
    }

    async _tabs() {
      const tabs = await this.queryTabs();
      if (!Array.isArray(tabs)) {
        throw new RoutingError("tabs_unavailable");
      }
      return tabs
        .filter((tab) => Number.isInteger(tab?.id))
        .sort((left, right) => left.id - right.id);
    }

    async route({ session, routing }) {
      if (typeof session !== "string" || session.trim() === "") {
        throw new RoutingError("invalid_session");
      }
      return this._serialize(session, () => this._routeUnlocked({ session, routing }));
    }

    async _routeUnlocked({ session, routing }) {
      if (routing === null) {
        return this._routeProvisional(session);
      }
      return this._routeBound(session, normalizeRouting(routing));
    }

    async _routeBound(session, routing) {
      const cached = await this.routingStore.get(session);
      if (cached?.kind === ROUTING_KIND.INVALIDATED) {
        throw new RoutingError("binding_invalidated");
      }
      if (
        cached?.kind === ROUTING_KIND.BOUND &&
        cached.binding_version > routing.binding_version
      ) {
        throw new RoutingError("stale_binding_snapshot");
      }

      const tabs = await this._tabs();
      const matches = tabs.filter((tab) => tabIdentityMatches(tab, routing));
      let target = null;
      if (
        cached?.kind === ROUTING_KIND.BOUND &&
        Number.isInteger(cached.tab_id)
      ) {
        target = matches.find((tab) => tab.id === cached.tab_id) || null;
      }
      target ||= matches[0] || null;

      if (!target) {
        target = await this.createTab({ url: routing.canonical_url, active: false });
        if (!Number.isInteger(target?.id)) {
          throw new RoutingError("tab_create_failed");
        }
        if (target.url && !tabIdentityMatches(target, routing)) {
          throw new RoutingError("created_tab_target_mismatch");
        }
      }

      await this.routingStore.setBound({
        session,
        routing,
        tabId: target.id,
      });
      return {
        kind: ROUTING_KIND.BOUND,
        tabId: target.id,
        url: target.url || routing.canonical_url,
        routing,
      };
    }

    async _routeProvisional(session) {
      const cached = await this.routingStore.get(session);
      if (cached?.kind === ROUTING_KIND.INVALIDATED) {
        throw new RoutingError("binding_invalidated");
      }
      if (cached?.kind === ROUTING_KIND.BOUND) {
        throw new RoutingError("backend_binding_missing_for_cached_bound_session");
      }

      const tabs = await this._tabs();
      if (cached?.kind === ROUTING_KIND.PROVISIONAL && Number.isInteger(cached.tab_id)) {
        const existing = tabs.find(
          (tab) => tab.id === cached.tab_id && isNewChatUrl(tab.url),
        );
        if (existing) {
          return {
            kind: ROUTING_KIND.PROVISIONAL,
            tabId: existing.id,
            url: existing.url,
            routing: null,
          };
        }
      }

      const created = await this.createTab({ url: NEW_CHAT_URL, active: false });
      if (!Number.isInteger(created?.id)) {
        throw new RoutingError("tab_create_failed");
      }
      if (created.url && !isNewChatUrl(created.url)) {
        throw new RoutingError("created_tab_target_mismatch");
      }
      await this.routingStore.setProvisional(session, created.id);
      return {
        kind: ROUTING_KIND.PROVISIONAL,
        tabId: created.id,
        url: created.url || NEW_CHAT_URL,
        routing: null,
      };
    }

    async revalidateTarget({ session, routing, tabId }) {
      const tabs = await this._tabs();
      const tab = tabs.find((candidate) => candidate.id === tabId);
      if (!tab) {
        throw new RoutingError("target_tab_missing");
      }

      if (routing === null) {
        const cached = await this.routingStore.get(session);
        if (
          cached?.kind !== ROUTING_KIND.PROVISIONAL ||
          cached.tab_id !== tabId ||
          !isNewChatUrl(tab.url)
        ) {
          throw new RoutingError("provisional_target_changed");
        }
        return tab;
      }

      const normalized = normalizeRouting(routing);
      if (!tabIdentityMatches(tab, normalized)) {
        await this.routingStore.markInvalidated(session, "target_navigation_changed");
        throw new RoutingError("bound_target_changed");
      }
      return tab;
    }

    async invalidate(session, reason) {
      await this.routingStore.markInvalidated(session, reason);
    }
  }

  namespace.routing = {
    NEW_CHAT_URL,
    RoutingError,
    isSupportedChatGptUrl,
    isNewChatUrl,
    conversationIdentity,
    ConversationRouter,
  };
})();
