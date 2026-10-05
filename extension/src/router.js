(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});
  const { ROUTING_KIND } = namespace.routingStore;
  const NEW_CHAT_URL = "https://chatgpt.com/";
  const DEFAULT_CREATED_TAB_POLL_DELAYS_MS = Object.freeze([100, 250, 500, 1000, 2000, 3000, 3000]);

  class RoutingError extends Error {
    constructor(code, details = null) {
      super(details ? code + ":" + details : code);
      this.name = "RoutingError";
      this.code = code;
      this.details = details;
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
    if (
      parts.length < 2 ||
      parts[parts.length - 2] !== "c" ||
      !parts[parts.length - 1]
    ) {
      return null;
    }
    const conversationId = parts[parts.length - 1];
    return {
      conversationId,
      canonicalUrl: "https://chatgpt.com/c/" + conversationId,
    };
  }

  function isProvisionalChatGptUrl(rawUrl) {
    return isSupportedChatGptUrl(rawUrl) && conversationIdentity(rawUrl) === null;
  }

  function isLegacySyntheticConversationId(value) {
    if (typeof value !== "string" || !value) return false;
    let decoded = value;
    try { decoded = decodeURIComponent(value); } catch {}
    return decoded.toLowerCase().startsWith("local-chatgpt:");
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
    if (isLegacySyntheticConversationId(routing.conversation_id)) {
      throw new RoutingError("legacy_synthetic_binding_unrecoverable");
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
    constructor({
      routingStore,
      queryTabs,
      createTab,
      sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
      createdTabPollDelaysMs = DEFAULT_CREATED_TAB_POLL_DELAYS_MS,
    }) {
      this.routingStore = routingStore;
      this.queryTabs = queryTabs;
      this.createTab = createTab;
      this.sleep = sleep;
      this.createdTabPollDelaysMs = createdTabPollDelaysMs;
      this.sessionChains = new Map();
    }

    _serialize(session, task) {
      const previous = this.sessionChains.get(session) || Promise.resolve();
      const current = previous.then(task, task);
      const cleanup = () => {
        if (this.sessionChains.get(session) === tracked) {
          this.sessionChains.delete(session);
        }
      };
      const tracked = current.then(cleanup, cleanup);
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

    async _awaitCreatedTabTarget(created, matchesTarget) {
      if (!Number.isInteger(created?.id)) {
        throw new RoutingError("tab_create_failed");
      }
      if (created.url && matchesTarget(created)) {
        return created;
      }
      if (created.url && conversationIdentity(created.url)) {
        throw new RoutingError("created_tab_target_mismatch");
      }

      for (const delay of this.createdTabPollDelaysMs) {
        await this.sleep(delay);
        const tabs = await this._tabs();
        const candidate = tabs.find((tab) => tab.id === created.id);
        if (!candidate) {
          continue;
        }
        if (matchesTarget(candidate)) {
          return candidate;
        }
        if (candidate.url && conversationIdentity(candidate.url)) {
          throw new RoutingError("created_tab_target_mismatch");
        }
      }

      throw new RoutingError("created_tab_navigation_timeout");
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
      let cachedTransient = null;
      if (
        cached?.kind === ROUTING_KIND.BOUND &&
        Number.isInteger(cached.tab_id)
      ) {
        const cachedTab = tabs.find((tab) => tab.id === cached.tab_id) || null;
        if (cachedTab) {
          const cachedIdentity = conversationIdentity(cachedTab.url);
          if (cachedIdentity && !tabIdentityMatches(cachedTab, routing)) {
            throw new RoutingError(
              "bound_target_changed",
              [
                "expected=" + routing.conversation_id,
                "observed=" + cachedIdentity.conversationId,
                "source=cached_tab",
              ].join(","),
            );
          }
          if (tabIdentityMatches(cachedTab, routing)) {
            target = cachedTab;
          } else if (
            cachedTab.url === "about:blank" ||
            isSupportedChatGptUrl(cachedTab.url)
          ) {
            cachedTransient = cachedTab;
          }
        }
      }
      target ||= matches[0] || cachedTransient || null;

      if (!target) {
        const created = await this.createTab({
          url: routing.canonical_url,
          active: false,
        });
        target = await this._awaitCreatedTabTarget(
          created,
          (tab) => tabIdentityMatches(tab, routing),
        );
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
          (tab) =>
            tab.id === cached.tab_id && isProvisionalChatGptUrl(tab.url),
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
      const target = await this._awaitCreatedTabTarget(
        created,
        (tab) => isProvisionalChatGptUrl(tab.url),
      );
      await this.routingStore.setProvisional(session, target.id);
      return {
        kind: ROUTING_KIND.PROVISIONAL,
        tabId: target.id,
        url: target.url || NEW_CHAT_URL,
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
          !isProvisionalChatGptUrl(tab.url)
        ) {
          throw new RoutingError("provisional_target_changed");
        }
        return tab;
      }

      const normalized = normalizeRouting(routing);
      if (tabIdentityMatches(tab, normalized)) {
        return tab;
      }

      const incompatible = (candidate) => {
        const identity = conversationIdentity(candidate?.url);
        if (identity) return !tabIdentityMatches(candidate, normalized);
        return Boolean(
          candidate?.url &&
          candidate.url !== "about:blank" &&
          !isSupportedChatGptUrl(candidate.url)
        );
      };

      if (incompatible(tab)) {
        const observed = conversationIdentity(tab.url);
        throw new RoutingError(
          "bound_target_changed",
          [
            "expected=" + normalized.conversation_id,
            "observed=" + (observed?.conversationId || "non_conversation"),
            "source=revalidate",
          ].join(","),
        );
      }

      for (const delay of this.createdTabPollDelaysMs) {
        await this.sleep(delay);
        const refreshedTabs = await this._tabs();
        const candidate = refreshedTabs.find((item) => item.id === tabId);
        if (!candidate) {
          throw new RoutingError("target_tab_missing");
        }
        if (tabIdentityMatches(candidate, normalized)) {
          return candidate;
        }
        if (incompatible(candidate)) {
          const observed = conversationIdentity(candidate.url);
          throw new RoutingError(
            "bound_target_changed",
            [
              "expected=" + normalized.conversation_id,
              "observed=" + (observed?.conversationId || "non_conversation"),
              "source=revalidate",
            ].join(","),
          );
        }
      }

      throw new RoutingError(
        "bound_target_changed",
        [
          "expected=" + normalized.conversation_id,
          "observed=non_conversation",
          "source=timeout",
        ].join(","),
      );
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
    isProvisionalChatGptUrl,
    isLegacySyntheticConversationId,
    ConversationRouter,
  };
})();
