(() => {
  "use strict";

  const namespace = globalThis.DevCockpitCompanion;
  const { QueueStore } = namespace.queue;
  const { SentPromptStore, PendingResponseStore } = namespace.responses;
  const { ChatGptSendStore, SEND_STATE } = namespace.sendStore;
  const { ConversationRoutingStore } = namespace.routingStore;
  const { ConversationRouter, isSupportedChatGptUrl } = namespace.routing;
  const { CompanionTransport, CONNECTION_STATUS } = namespace.transport;
  const {
    PromptSendCoordinator,
    AmbiguousSendRecovery,
    canonicalConversation,
    isLegacySyntheticConversationId,
    isLegacySyntheticRouting,
    routingFromActiveConversation,
    isArchitectureSession,
    shouldCloseManagedTabAfterAck,
    sentContextMatchesTab,
  } = namespace.send;
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
    removeTab: (tabId) => browser.tabs.remove(tabId),
  });
  let connection = {
    status: CONNECTION_STATUS.DISCONNECTED,
    lastError: null,
  };
  let sendCoordinator = null;
  let sendRecovery = null;

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
      let routing = prompt.routing;
      if (isLegacySyntheticRouting(routing)) {
        const recovered = await recoveredRoutingForLegacy(prompt.session, routing);
        if (recovered) routing = recovered;
      }
      const acceptedPrompt = { ...prompt, routing };
      await queueStore.acceptPrompt(acceptedPrompt);
      const sendState = await sendStore.ensureQueued(acceptedPrompt);
      if (sendState.state === SEND_STATE.SENT_CONFIRMED) {
        await queueStore.remove(prompt.deliveryId);
      }
      await broadcast("devcockpit_queue_changed");
    },
    onPromptAccepted: async (prompt) => {
      if (isArchitectureSession(prompt.session)) {
        await broadcast("devcockpit_queue_changed");
        await broadcast("devcockpit_send_state_changed");
        return;
      }
      void sendCoordinator.enqueue(prompt.deliveryId).then(async (result) => {
        if (result?.state === SEND_STATE.AMBIGUOUS) {
          await sendRecovery.recover(prompt.deliveryId);
        }
        await broadcast("devcockpit_queue_changed");
        await broadcast("devcockpit_sent_prompts_changed");
        await broadcast("devcockpit_send_state_changed");
      }).catch(async () => {
        await broadcast("devcockpit_send_state_changed");
      });
    },
    getPendingResponses: () => pendingResponseStore.list(),
    getPendingSendStatuses: () => sendStore.pendingEvents(),
    onSendStatusAck: async (eventId) => {
      const pending = await sendStore.pendingEvents();
      const event = pending.find((item) => item.event_id === eventId) || null;
      const sentContext =
        event?.state === SEND_STATE.SENT_CONFIRMED
          ? await sentPromptStore.get(event.delivery_id)
          : null;
      const queuedPrompts = sentContext ? await queueStore.list() : [];

      await sendStore.ackEvent(eventId);

      if (
        shouldCloseManagedTabAfterAck({
          event,
          sentContext,
          queuedPrompts,
        })
      ) {
        await router.closeManagedTab({
          session: sentContext.session,
          tabId: sentContext.tab_id,
          conversationUrl: sentContext.conversation_url,
        });
      }
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

  sendRecovery = new AmbiguousSendRecovery({
    queueStore,
    sentPromptStore,
    sendStore,
    routingStore,
    router,
    inspectTab: (tabId, text) =>
      browser.tabs.sendMessage(tabId, {
        type: "devcockpit_inspect_prompt_delivery",
        text,
      }),
    reloadTab: (tabId) => browser.tabs.reload(tabId),
    emitStatus: (event) => transport.sendPendingSendStatus(event),
    resumeSession: (session) => sendCoordinator.resumeSession(session),
  });

  async function recoveredRoutingForLegacy(session, routing) {
    if (!isLegacySyntheticRouting(routing)) return routing;
    const sent = await sentPromptStore.list();
    const candidates = sent
      .filter((entry) => entry.session === session && entry.conversation_url)
      .sort((left, right) => String(right.sent_at).localeCompare(String(left.sent_at)));
    for (const context of candidates) {
      const conversation = canonicalConversation(context.conversation_url);
      if (
        conversation &&
        !isLegacySyntheticConversationId(conversation.conversation_id)
      ) {
        return {
          binding_version: routing.binding_version,
          conversation_id: conversation.conversation_id,
          canonical_url: conversation.canonical_url,
        };
      }
    }
    return null;
  }

  async function recoverPersistedSends() {
    const ambiguousEvents = await sendStore.recoverInterruptedArmedSends();
    for (const event of ambiguousEvents) {
      transport.sendPendingSendStatus(event);
    }

    const queue = await queueStore.list();
    for (const originalEntry of queue) {
      let entry = originalEntry;
      if (isLegacySyntheticRouting(entry.routing)) {
        const recovered = await recoveredRoutingForLegacy(entry.session, entry.routing);
        if (recovered) {
          entry = await queueStore.repairLegacyRouting(entry.delivery_id, recovered);
        }
      }
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
      if (state?.state === SEND_STATE.AMBIGUOUS) {
        if (!isArchitectureSession(entry.session)) {
          void sendRecovery.recover(entry.delivery_id).then(async () => {
            await broadcast("devcockpit_queue_changed");
            await broadcast("devcockpit_sent_prompts_changed");
            await broadcast("devcockpit_send_state_changed");
          }).catch(async () => {
            await broadcast("devcockpit_send_state_changed");
          });
        }
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
        if (isArchitectureSession(entry.session)) {
          if (state.state !== SEND_STATE.QUEUED) {
            const reset = await sendStore.transition({
              deliveryId: entry.delivery_id,
              session: entry.session,
              state: SEND_STATE.QUEUED,
              attempt: state.attempt,
              errorCode: null,
            });
            transport.sendPendingSendStatus(reset.event);
          }
          continue;
        }
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

  async function adoptActiveConversationForLegacy(deliveryId) {
    const queue = await queueStore.list();
    const entry = queue.find((candidate) => candidate.delivery_id === deliveryId);
    if (!entry) {
      throw new Error("delivery_not_found:" + deliveryId);
    }
    if (!isLegacySyntheticRouting(entry.routing)) {
      return entry;
    }

    const tab = await activeChatGptTab();
    const repaired = routingFromActiveConversation(entry.routing, tab.url);
    if (!repaired) {
      throw new Error("legacy_binding_requires_active_conversation");
    }

    const updated = await queueStore.repairLegacyRouting(deliveryId, repaired);
    await routingStore.setBound({
      session: entry.session,
      routing: repaired,
      tabId: tab.id,
    });
    return updated;
  }

  async function listResponseCandidates(deliveryId) {
    const context = await sentPromptStore.get(deliveryId);
    if (!context) {
      return { ok: false, error: "Contexte de prompt envoyé introuvable" };
    }

    try {
      const tab = await activeChatGptTab();
      if (!sentContextMatchesTab(context, tab)) {
        return {
          ok: false,
          error:
            "Ouvrez la conversation ChatGPT exacte utilisée pour ce prompt avant de choisir une réponse",
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
    try {
      const tab = await activeChatGptTab();
      const result = await sendRecovery.verify(deliveryId, { tabId: tab.id });
      await broadcast("devcockpit_queue_changed");
      await broadcast("devcockpit_sent_prompts_changed");
      await broadcast("devcockpit_send_state_changed");
      return result;
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
      message?.type === "devcockpit_send_arch_manually" &&
      typeof message.deliveryId === "string"
    ) {
      try {
        const queue = await queueStore.list();
        const entry = queue.find(
          (candidate) => candidate.delivery_id === message.deliveryId,
        );
        if (!entry) {
          return { ok: false, error: "delivery_not_found:" + message.deliveryId };
        }
        if (!isArchitectureSession(entry.session)) {
          return { ok: false, error: "manual_arch_only" };
        }

        const tab = await activeChatGptTab();
        if (entry.routing) {
          const activeConversation = canonicalConversation(tab.url);
          if (
            !activeConversation ||
            activeConversation.canonical_url !== entry.routing.canonical_url
          ) {
            return { ok: false, error: "manual_arch_wrong_conversation" };
          }
        }

        const current = await sendStore.get(entry.delivery_id);
        if (current?.state === SEND_STATE.BLOCKED) {
          await sendStore.retryBlocked(entry.delivery_id);
        }
        if (current?.state === SEND_STATE.AMBIGUOUS) {
          return { ok: false, error: "manual_arch_send_ambiguous_verify_first" };
        }

        const result = await sendCoordinator.enqueue(entry.delivery_id, {
          manualTabId: tab.id,
        });
        await broadcast("devcockpit_queue_changed");
        await broadcast("devcockpit_sent_prompts_changed");
        await broadcast("devcockpit_send_state_changed");
        return result;
      } catch (error) {
        return {
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        };
      }
    }

    if (
      message?.type === "devcockpit_retry_chatgpt_send" &&
      typeof message.deliveryId === "string"
    ) {
      try {
        await adoptActiveConversationForLegacy(message.deliveryId);
        await sendStore.retryBlocked(message.deliveryId);
        const result = await sendCoordinator.enqueue(message.deliveryId);
        await broadcast("devcockpit_send_state_changed");
        return result;
      } catch (error) {
        return {
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        };
      }
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
