(() => {
  "use strict";

  const namespace = globalThis.DevCockpitCompanion;
  const { QueueStore } = namespace.queue;
  const { SentPromptStore, PendingResponseStore } = namespace.responses;
  const { ChatGptSendStore, SEND_STATE } = namespace.sendStore;
  const { ConversationRoutingStore } = namespace.routingStore;
  const { ConversationRouter, isSupportedChatGptUrl } = namespace.routing;
  const { CompanionTransport, CONNECTION_STATUS } = namespace.transport;
  const { PromptSendCoordinator, canonicalConversation } = namespace.send;
  const { buildChatGptResponseMessage, ProtocolError } = namespace.protocol;

  const SOCKET_URL = "ws://127.0.0.1:8000/api/companion/ws";

  const queueStore = new QueueStore(browser.storage.local);
  const sentPromptStore = new SentPromptStore(browser.storage.local);
  const pendingResponseStore = new PendingResponseStore(browser.storage.local);
  const sendStore = new ChatGptSendStore(browser.storage.local);
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
  let sendCoordinator = null;

  async function broadcast(type) {
    try {
      await browser.runtime.sendMessage({ type });
    } catch {
      // No popup is open; persistent local state remains authoritative.
    }
  }

  const transport = new CompanionTransport({
    url: SOCKET_URL,
    webSocketFactory: (url) => new WebSocket(url),
    onPrompt: async (prompt) => {
      await queueStore.acceptPrompt(prompt);
      const sendState = await sendStore.ensureQueued(prompt);
      if (sendState.state === SEND_STATE.SENT_CONFIRMED) {
        await queueStore.remove(prompt.deliveryId);
      }
      await broadcast("devcockpit_queue_changed");
    },
    onPromptAccepted: async (prompt) => {
      void sendCoordinator.enqueue(prompt.deliveryId).then(async () => {
        await broadcast("devcockpit_queue_changed");
        await broadcast("devcockpit_sent_prompts_changed");
        await broadcast("devcockpit_send_state_changed");
      });
    },
    getPendingResponses: () => pendingResponseStore.list(),
    getPendingSendStatuses: () => sendStore.pendingEvents(),
    onSendStatusAck: async (eventId) => {
      await sendStore.ackEvent(eventId);
      await broadcast("devcockpit_send_state_changed");
    },
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

  sendCoordinator = new PromptSendCoordinator({
    queueStore,
    sentPromptStore,
    sendStore,
    router,
    sendToTab: (tabId, message) => browser.tabs.sendMessage(tabId, message),
    emitStatus: (event) => transport.sendPendingSendStatus(event),
  });

  async function recoverPersistedSends() {
    const ambiguousEvents = await sendStore.recoverInterruptedArmedSends();
    for (const event of ambiguousEvents) {
      transport.sendPendingSendStatus(event);
    }

    const queue = await queueStore.list();
    for (const entry of queue) {
      await sendStore.ensureQueued({
        deliveryId: entry.delivery_id,
        session: entry.session,
      });
      const state = await sendStore.get(entry.delivery_id);
      if (state?.state === SEND_STATE.SENT_CONFIRMED) {
        await sentPromptStore.recordSent({
          deliveryId: entry.delivery_id,
          session: entry.session,
          tabId: null,
          conversationUrl: state.conversation?.canonical_url || null,
        });
        await queueStore.remove(entry.delivery_id);
        continue;
      }
      if (
        state &&
        [
          SEND_STATE.QUEUED,
          SEND_STATE.ROUTING,
          SEND_STATE.WAITING_READY,
          SEND_STATE.RETRYABLE_FAILURE,
        ].includes(state.state)
      ) {
        void sendCoordinator.enqueue(entry.delivery_id);
      }
    }
  }

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
          error:
            "Ouvrez l’onglet ChatGPT utilisé pour ce prompt avant de choisir une réponse",
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

  async function resolveAmbiguousSend(deliveryId) {
    const queue = await queueStore.list();
    const entry = queue.find((candidate) => candidate.delivery_id === deliveryId);
    if (!entry) {
      return { ok: false, error: "delivery_not_found:" + deliveryId };
    }
    const sendState = await sendStore.get(deliveryId);
    if (!sendState || sendState.state !== SEND_STATE.AMBIGUOUS) {
      return { ok: false, error: "chatgpt_send_not_ambiguous" };
    }

    try {
      const tab = await activeChatGptTab();
      if (
        entry.routing &&
        canonicalConversation(tab.url)?.canonical_url !== entry.routing.canonical_url
      ) {
        return {
          ok: false,
          error: "ambiguous_resolution_wrong_conversation",
        };
      }

      const inspection = await browser.tabs.sendMessage(tab.id, {
        type: "devcockpit_inspect_prompt_delivery",
        text: entry.text,
      });
      if (!inspection?.ok) {
        return {
          ok: false,
          error: inspection?.error || "delivery_evidence_ambiguous",
        };
      }

      if (inspection.state === "NOT_SENT") {
        const resolved = await sendStore.resolveAmbiguous({
          deliveryId,
          state: SEND_STATE.BLOCKED,
          errorCode: "verified_not_sent",
        });
        transport.sendPendingSendStatus(resolved.event);
        await broadcast("devcockpit_send_state_changed");
        return { ok: true, state: SEND_STATE.BLOCKED, evidence: "NOT_SENT" };
      }

      if (inspection.state === "SENT") {
        const conversation =
          canonicalConversation(inspection.conversationUrl) ||
          canonicalConversation(tab.url);
        if (!conversation) {
          return {
            ok: false,
            error: "verified_send_missing_conversation_identity",
          };
        }
        const resolved = await sendStore.resolveAmbiguous({
          deliveryId,
          state: SEND_STATE.SENT_CONFIRMED,
          conversation,
        });
        transport.sendPendingSendStatus(resolved.event);
        await sentPromptStore.recordSent({
          deliveryId: entry.delivery_id,
          session: entry.session,
          tabId: tab.id,
          conversationUrl: conversation.canonical_url,
        });
        await queueStore.remove(entry.delivery_id);
        void sendCoordinator.resumeSession(entry.session);
        await broadcast("devcockpit_queue_changed");
        await broadcast("devcockpit_sent_prompts_changed");
        await broadcast("devcockpit_send_state_changed");
        return {
          ok: true,
          state: SEND_STATE.SENT_CONFIRMED,
          evidence: "SENT",
          conversation,
        };
      }

      return { ok: false, error: "delivery_evidence_ambiguous" };
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
        sendStates: await sendStore.list(),
        pendingSendStatuses: await sendStore.pendingEvents(),
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
      message?.type === "devcockpit_retry_chatgpt_send" &&
      typeof message.deliveryId === "string"
    ) {
      await sendStore.retryBlocked(message.deliveryId);
      const result = await sendCoordinator.enqueue(message.deliveryId);
      await broadcast("devcockpit_send_state_changed");
      return result;
    }

    if (
      message?.type === "devcockpit_resolve_ambiguous_send" &&
      typeof message.deliveryId === "string"
    ) {
      return resolveAmbiguousSend(message.deliveryId);
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

  void recoverPersistedSends();
  transport.connect();
})();
