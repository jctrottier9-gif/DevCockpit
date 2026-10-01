(() => {
  "use strict";

  const statusElement = document.getElementById("connection-status");
  const errorElement = document.getElementById("connection-error");
  const socketElement = document.getElementById("socket-url");
  const queueElement = document.getElementById("queue");
  const emptyElement = document.getElementById("empty-state");
  const sentElement = document.getElementById("sent-prompts");
  const sentEmptyElement = document.getElementById("sent-empty-state");
  const pendingElement = document.getElementById("pending-responses");
  const pendingEmptyElement = document.getElementById("pending-empty-state");
  const retryButton = document.getElementById("retry-connection");
  const promptTemplate = document.getElementById("prompt-card-template");
  const sentTemplate = document.getElementById("sent-card-template");
  const pendingTemplate = document.getElementById("pending-card-template");

  const connectionLabels = {
    CONNECTED: "● Connecté",
    RECONNECTING: "◐ Reconnexion…",
    DISCONNECTED: "○ Déconnecté",
    CONFLICT: "⚠ Un autre compagnon est déjà connecté",
  };

  function localStatusLabel(status) {
    return status === "SEND_REQUESTED" ? "Envoi demandé" : "En attente";
  }

  function showEntryError(element, message) {
    element.textContent = message || "";
    element.hidden = !message;
  }

  function renderPromptQueue(queue) {
    queueElement.replaceChildren();
    emptyElement.hidden = queue.length !== 0;

    for (const entry of queue) {
      const fragment = promptTemplate.content.cloneNode(true);
      fragment.querySelector(".session").textContent = entry.session;
      fragment.querySelector(".local-status").textContent =
        localStatusLabel(entry.local_status);
      fragment.querySelector(".preview").textContent = entry.text;
      fragment.querySelector(".full-text").textContent = entry.text;
      showEntryError(fragment.querySelector(".entry-error"), entry.last_error);

      const sendButton = fragment.querySelector(".send");
      if (entry.local_status === "SEND_REQUESTED") {
        sendButton.textContent = "Réessayer";
        sendButton.title =
          "Une tentative précédente peut avoir atteint ChatGPT. Vérifiez la conversation avant de réessayer.";
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

  function renderCandidates(entry, panel, container, confirmButton, entryError, candidates) {
    container.replaceChildren();
    let selectedText = null;

    candidates.forEach((candidate, index) => {
      const label = document.createElement("label");
      label.className = "candidate";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "response-" + entry.delivery_id;
      radio.value = String(index);
      const preview = document.createElement("span");
      preview.textContent = candidate.preview || candidate.text;
      radio.addEventListener("change", () => {
        selectedText = candidate.text;
        confirmButton.disabled = false;
      });
      label.append(radio, preview);
      container.appendChild(label);
    });

    confirmButton.onclick = async () => {
      if (!selectedText) {
        return;
      }
      confirmButton.disabled = true;
      const result = await browser.runtime.sendMessage({
        type: "devcockpit_return_response",
        deliveryId: entry.delivery_id,
        text: selectedText,
      });
      if (!result?.ok) {
        showEntryError(entryError, result?.error || "Retour impossible");
        confirmButton.disabled = false;
        return;
      }
      panel.hidden = true;
      await refresh();
    };
  }

  function renderSentPrompts(entries) {
    sentElement.replaceChildren();
    sentEmptyElement.hidden = entries.length !== 0;

    for (const entry of entries) {
      const fragment = sentTemplate.content.cloneNode(true);
      fragment.querySelector(".session").textContent = entry.session;
      fragment.querySelector(".sent-at").textContent =
        new Date(entry.sent_at).toLocaleString();

      const entryError = fragment.querySelector(".entry-error");
      const returnButton = fragment.querySelector(".return-response");
      const panel = fragment.querySelector(".candidate-panel");
      const candidatesElement = fragment.querySelector(".candidates");
      const confirmButton = fragment.querySelector(".confirm-return");

      returnButton.addEventListener("click", async () => {
        returnButton.disabled = true;
        showEntryError(entryError, null);
        try {
          const result = await browser.runtime.sendMessage({
            type: "devcockpit_list_response_candidates",
            deliveryId: entry.delivery_id,
          });
          if (!result?.ok) {
            showEntryError(entryError, result?.error || "Lecture ChatGPT impossible");
            return;
          }
          const candidates = Array.isArray(result.candidates) ? result.candidates : [];
          if (candidates.length === 0) {
            showEntryError(entryError, "Aucune réponse assistant disponible");
            return;
          }
          panel.hidden = false;
          confirmButton.disabled = true;
          renderCandidates(
            entry,
            panel,
            candidatesElement,
            confirmButton,
            entryError,
            candidates,
          );
        } finally {
          returnButton.disabled = false;
        }
      });

      sentElement.appendChild(fragment);
    }
  }

  function renderPendingResponses(entries) {
    pendingElement.replaceChildren();
    pendingEmptyElement.hidden = entries.length !== 0;
    for (const entry of entries) {
      const fragment = pendingTemplate.content.cloneNode(true);
      fragment.querySelector(".session").textContent = entry.session;
      fragment.querySelector(".pending-id").textContent =
        "response_id: " + entry.response_id;
      fragment.querySelector(".preview").textContent = entry.text;
      showEntryError(fragment.querySelector(".entry-error"), entry.last_error);
      pendingElement.appendChild(fragment);
    }
  }

  function render(state) {
    const connection = state.connection || { status: "DISCONNECTED" };
    statusElement.textContent =
      connectionLabels[connection.status] || connectionLabels.DISCONNECTED;
    errorElement.textContent = connection.lastError || "";
    errorElement.hidden = !connection.lastError;
    retryButton.hidden = !["DISCONNECTED", "CONFLICT"].includes(connection.status);
    socketElement.textContent = state.socketUrl || "";

    renderPromptQueue(Array.isArray(state.queue) ? state.queue : []);
    renderSentPrompts(Array.isArray(state.sentPrompts) ? state.sentPrompts : []);
    renderPendingResponses(
      Array.isArray(state.pendingResponses) ? state.pendingResponses : [],
    );
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
      message?.type === "devcockpit_connection_changed" ||
      message?.type === "devcockpit_sent_prompts_changed" ||
      message?.type === "devcockpit_response_changed"
    ) {
      void refresh();
    }
  });

  void refresh();
})();
