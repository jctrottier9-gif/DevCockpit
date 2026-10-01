(() => {
  "use strict";

  const namespace = globalThis.DevCockpitCompanion;
  const { QueueStore } = namespace.queue;
  const { CompanionTransport, CONNECTION_STATUS } = namespace.transport;
  const { PromptSendCoordinator } = namespace.send;

  const SOCKET_URL = "ws://127.0.0.1:8000/api/companion/ws";

  const queueStore = new QueueStore(browser.storage.local);
  let connection = {
    status: CONNECTION_STATUS.DISCONNECTED,
    lastError: null,
  };

  async function broadcast(type) {
    try {
      await browser.runtime.sendMessage({ type });
    } catch {
      // No popup is open; persisted queue/state remains authoritative locally.
    }
  }

  const transport = new CompanionTransport({
    url: SOCKET_URL,
    webSocketFactory: (url) => new WebSocket(url),
    onPrompt: async (prompt) => {
      await queueStore.acceptPrompt(prompt);
      await broadcast("devcockpit_queue_changed");
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
    getActiveTabs: () => browser.tabs.query({ active: true, currentWindow: true }),
    sendToTab: (tabId, message) => browser.tabs.sendMessage(tabId, message),
  });

  browser.runtime.onMessage.addListener(async (message) => {
    if (message?.type === "devcockpit_get_state") {
      return {
        socketUrl: SOCKET_URL,
        connection: connection,
        queue: await queueStore.list(),
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
      return result;
    }

    return undefined;
  });

  transport.connect();
})();
