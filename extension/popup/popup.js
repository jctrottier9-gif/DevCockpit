(() => {
  "use strict";

  const statusElement = document.getElementById("connection-status");
  const errorElement = document.getElementById("connection-error");
  const socketElement = document.getElementById("socket-url");
  const queueElement = document.getElementById("queue");
  const emptyElement = document.getElementById("empty-state");
  const retryButton = document.getElementById("retry-connection");
  const template = document.getElementById("prompt-card-template");

  const connectionLabels = {
    CONNECTED: "● Connecté",
    RECONNECTING: "◐ Reconnexion…",
    DISCONNECTED: "○ Déconnecté",
    CONFLICT: "⚠ Un autre compagnon est déjà connecté",
  };

  function localStatusLabel(status) {
    return status === "SEND_REQUESTED" ? "Envoi demandé" : "En attente";
  }

  function render(state) {
    const connection = state.connection || { status: "DISCONNECTED" };
    statusElement.textContent =
      connectionLabels[connection.status] || connectionLabels.DISCONNECTED;
    errorElement.textContent = connection.lastError || "";
    errorElement.hidden = !connection.lastError;
    retryButton.hidden = !["DISCONNECTED", "CONFLICT"].includes(
      connection.status,
    );
    socketElement.textContent = state.socketUrl || "";

    const queue = Array.isArray(state.queue) ? state.queue : [];
    queueElement.replaceChildren();
    emptyElement.hidden = queue.length !== 0;

    for (const entry of queue) {
      const fragment = template.content.cloneNode(true);
      const card = fragment.querySelector(".card");
      card.dataset.deliveryId = entry.delivery_id;
      fragment.querySelector(".session").textContent = entry.session;
      fragment.querySelector(".local-status").textContent =
        localStatusLabel(entry.local_status);
      fragment.querySelector(".preview").textContent = entry.text;
      fragment.querySelector(".full-text").textContent = entry.text;

      const entryError = fragment.querySelector(".entry-error");
      if (entry.last_error) {
        entryError.textContent = entry.last_error;
        entryError.hidden = false;
      }

      const sendButton = fragment.querySelector(".send");
      if (entry.local_status === "SEND_REQUESTED") {
        sendButton.textContent = "Réessayer";
        sendButton.title =
          "Une tentative d'envoi précédente peut avoir atteint ChatGPT. Vérifiez la conversation avant de réessayer.";
      }
      sendButton.addEventListener("click", async () => {
        sendButton.disabled = true;
        const result = await browser.runtime.sendMessage({
          type: "devcockpit_send_prompt",
          deliveryId: entry.delivery_id,
        });
        if (!result?.ok && result?.error) {
          errorElement.textContent = result.error;
          errorElement.hidden = false;
        }
        await refresh();
      });

      queueElement.appendChild(fragment);
    }
  }

  async function refresh() {
    const state = await browser.runtime.sendMessage({
      type: "devcockpit_get_state",
    });
    render(state);
  }

  retryButton.addEventListener("click", async () => {
    retryButton.disabled = true;
    try {
      await browser.runtime.sendMessage({
        type: "devcockpit_retry_connection",
      });
      await refresh();
    } finally {
      retryButton.disabled = false;
    }
  });

  browser.runtime.onMessage.addListener((message) => {
    if (
      message?.type === "devcockpit_queue_changed" ||
      message?.type === "devcockpit_connection_changed"
    ) {
      void refresh();
    }
  });

  void refresh();
})();
