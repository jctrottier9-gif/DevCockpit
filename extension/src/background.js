(() => {
  "use strict";

  const namespace = globalThis.DevCockpitCompanion;
  const { QueueStore } = namespace.queue;
  const { SentPromptStore, PendingResponseStore } = namespace.responses;
  const { ConversationRoutingStore } = namespace.routingStore;
  const { ConversationRouter, isSupportedChatGptUrl } = namespace.routing;
  const { CompanionTransport, CONNECTION_STATUS } = namespace.transport;
  const { PromptSendCoordinator } = namespace.send;
  const { buildChatGptResponseMessage, ProtocolError } = namespace.protocol;

  const SOCKET_URL = "ws://127.0.0.1:8000/api/companion/ws";

  const queueStore = new QueueStore(browser.storage.local);
  const sentPromptStore = new SentPromptStore(browser.storage.local);
  const pendingResponseStore = new PendingResponseStore(browser.storage.local);
  const routingStore = new ConversationRoutingStore(browser.storage.local);
  const router = new ConversationRouter({
    routingStore,
    queryTabs: () =>
      browser.tabs.query({
        url: ["https://chatgpt.com/*", "https://chat.openai.com/*"],
      }),
    createTab: ({ url, active }) => browser.tabs.create({ url, active }),
  });
  let connection = {
    status: CONNECTION_STATUS.DISCONNECTED,
    lastError: null,
  };

  async function broadcast(type) {
    try {
      await browser.runtime.sendMessage({ type });
    } catch {
      // No popup is open; persistent local state remains authoritative.
    }
  }

  async function routeQueuedPrompt(prompt) {
    try {
      await router.route({
        session: prompt.session,
        routing: prompt.routing,
      });
      await queueStore.setError(prompt.deliveryId, null);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      try {
        await queueStore.setError(prompt.deliveryId, message);
      } catch {
        // The queue remains authoritative for accepted delivery state.
      }
    } finally {
      await broadcast("devcockpit_queue_changed");
    }
  }

  async function routePersistedQueue() {
    const queue = await queueStore.list();
    for (const entry of queue) {
      void routeQueuedPrompt({
        deliveryId: entry.delivery_id,
        session: entry.session,
        routing: entry.routing,
      });
    }
  }

  const transport = new CompanionTransport({
    url: SOCKET_URL,
    webSocketFactory: (url) => new WebSocket(url),
    onPrompt: async (prompt) => {
      await queueStore.acceptPrompt(prompt);
      await broadcast("devcockpit_queue_changed");
      void routeQueuedPrompt(prompt);
    },
    getPendingResponses: () => pendingResponseStore.list(),
    onResponseAck: async (responseId) => {
      await pendingResponseStore.remove(responseId);
      await broadcast("devcockpit_response_changed");
    },
    onResponseError: async (responseId, code) => {
      await pendingResponseStore.markError(responseId, code);
      await broadcast("devcockpit_response_changed");
    },
    onState: (state) => {
      connection = {
        status: state.status,
        lastError: state.lastError || null,
      };
      void broadcast("devcockpit_connection_changed");
    },
  });

  const sendCoordinator = new PromptSendCoordinator({
    queueStore,
    sentPromptStore,
    router,
    sendToTab: (tabId, message) => browser.tabs.sendMessage(tabId, message),
  });

  async function activeChatGptTab() {
    const tabs = await browser.tabs.query({ active: true, currentWindow: true });
    if (!Array.isArray(tabs) || tabs.length !== 1) {
      throw new Error("Aucun onglet actif unique");
    }
    const tab = tabs[0];
    if (!Number.isInteger(tab.id) || !isSupportedChatGptUrl(tab.url)) {
      throw new Error("Ouvrez la conversation ChatGPT cible dans l'onglet actif");
    }
    return tab;
  }

  async function listResponseCandidates(deliveryId) {
    const context = await sentPromptStore.get(deliveryId);
    if (!context) {
      return { ok: false, error: "Contexte de prompt envoyé introuvable" };
    }

    try {
      const tab = await activeChatGptTab();
      if (context.tab_id !== null && tab.id !== context.tab_id) {
        return {
          ok: false,
          error: "Ouvrez l’onglet ChatGPT utilisé pour ce prompt avant de choisir une réponse",
        };
      }
      const result = await browser.tabs.sendMessage(tab.id, {
        type: "devcockpit_list_chatgpt_responses",
      });
      if (!result?.ok) {
        return { ok: false, error: result?.error || "Lecture ChatGPT impossible" };
      }
      return { ok: true, candidates: result.candidates || [] };
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  async function returnSelectedResponse(deliveryId, text) {
    const context = await sentPromptStore.get(deliveryId);
    if (!context) {
      return { ok: false, error: "Contexte de prompt envoyé introuvable" };
    }
    if (typeof text !== "string" || text.trim() === "") {
      return { ok: false, error: "Réponse ChatGPT vide" };
    }

    const responseId = crypto.randomUUID();
    try {
      buildChatGptResponseMessage({
        responseId,
        deliveryId: context.delivery_id,
        session: context.session,
        text,
      });
    } catch (error) {
      if (error instanceof ProtocolError) {
        return { ok: false, error: error.code };
      }
      return { ok: false, error: "response_validation_failed" };
    }

    try {
      const stored = await pendingResponseStore.add({
        responseId,
        deliveryId: context.delivery_id,
        session: context.session,
        text,
      });
      transport.sendPendingResponse(stored.entry);
      await broadcast("devcockpit_response_changed");
      return { ok: true, responseId };
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  browser.runtime.onMessage.addListener(async (message) => {
    if (message?.type === "devcockpit_get_state") {
      return {
        socketUrl: SOCKET_URL,
        connection,
        queue: await queueStore.list(),
        sentPrompts: await sentPromptStore.list(),
        pendingResponses: await pendingResponseStore.list(),
        routing: await routingStore.list(),
      };
    }

    if (message?.type === "devcockpit_retry_connection") {
      transport.retry();
      return { ok: true };
    }

    if (
      message?.type === "devcockpit_send_prompt" &&
      typeof message.deliveryId === "string"
    ) {
      const result = await sendCoordinator.send(message.deliveryId);
      await broadcast("devcockpit_queue_changed");
      await broadcast("devcockpit_sent_prompts_changed");
      return result;
    }

    if (
      message?.type === "devcockpit_list_response_candidates" &&
      typeof message.deliveryId === "string"
    ) {
      return listResponseCandidates(message.deliveryId);
    }

    if (
      message?.type === "devcockpit_return_response" &&
      typeof message.deliveryId === "string"
    ) {
      return returnSelectedResponse(message.deliveryId, message.text);
    }

    return undefined;
  });

  void routePersistedQueue();
  transport.connect();
})();
