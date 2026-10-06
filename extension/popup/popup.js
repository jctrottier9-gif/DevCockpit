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

  function localStatusLabel(sendState, fallbackStatus) {
    const recovery = sendState?.recovery || null;
    if (
      ["INSPECTING", "RELOADING", "WAITING_CONTENT"].includes(recovery?.status)
    ) {
      return "Vérification automatique en cours";
    }
    if (recovery?.status === "CONFIRMED") {
      return "Envoi confirmé après resynchronisation";
    }
    if (recovery?.status === "VERIFIED_NOT_SENT") {
      return "Vérifié non envoyé";
    }
    if (recovery?.status === "INTERVENTION_REQUIRED") {
      return "Envoi ambigu — intervention requise";
    }

    const labels = {
      QUEUED: "En file",
      ROUTING: "Routage…",
      WAITING_READY: "ChatGPT occupé…",
      RETRYABLE_FAILURE: "Nouvelle tentative planifiée",
      SEND_ARMED: "Envoi armé — reprise bloquée",
      SENT_CONFIRMED: "Envoi confirmé",
      BLOCKED: "Action requise",
      AMBIGUOUS: "Envoi ambigu — intervention requise",
    };
    return labels[sendState?.state || fallbackStatus] || "En attente";
  }

  function bindingConflictMessage(conflict) {
    if (!conflict) return null;
    const expectedVersion = conflict.expected_binding_version ?? "inconnue";
    const observedVersion = conflict.observed_binding_version ?? "inconnue";
    return [
      "CHATGPT_SEND_BINDING_CONFLICT",
      "AgentSession=" + conflict.agent_session,
      "conversation attendue=" + (conflict.expected_conversation_id || "aucune"),
      "conversation observée=" + (conflict.observed_conversation_id || "non prouvée"),
      "binding_version attendue=" + expectedVersion,
      "binding_version observée=" + observedVersion,
    ].join(" · ");
  }

  function isArchitectureSession(session) {
    if (typeof session !== "string") return false;
    const parts = session.split(":");
    return parts.length === 3 && parts[1] === "ARCH";
  }

  function showEntryError(element, message) {
    element.textContent = message || "";
    element.hidden = !message;
  }

  function renderPromptQueue(queue, sendStates) {
    queueElement.replaceChildren();
    emptyElement.hidden = queue.length !== 0;
    const sendByDelivery = new Map(
      sendStates.map((entry) => [entry.delivery_id, entry]),
    );

    for (const entry of queue) {
      const fragment = promptTemplate.content.cloneNode(true);
      fragment.querySelector(".session").textContent = entry.session;
      const sendState = sendByDelivery.get(entry.delivery_id) || null;
      fragment.querySelector(".local-status").textContent =
        localStatusLabel(sendState, entry.local_status);
      fragment.querySelector(".preview").textContent = entry.text;
      fragment.querySelector(".full-text").textContent = entry.text;
      const entryError = fragment.querySelector(".entry-error");
      showEntryError(entryError, entry.last_error);

      const sendButton = fragment.querySelector(".send");
      const architecture = isArchitectureSession(entry.session);
      const ambiguous = sendState?.state === "AMBIGUOUS";
      const blocked = sendState?.state === "BLOCKED";
      const recoveryInProgress =
        ambiguous &&
        ["INSPECTING", "RELOADING", "WAITING_CONTENT"].includes(
          sendState?.recovery?.status,
        );
      const manualArchReady =
        architecture &&
        !ambiguous &&
        (!sendState || sendState.state === "QUEUED" || blocked);

      sendButton.hidden =
        recoveryInProgress || (!ambiguous && !blocked && !manualArchReady);
      if (ambiguous) {
        sendButton.textContent = "Vérifier l'envoi";
        sendButton.title = "Vérifie le DOM ChatGPT sans renvoyer automatiquement.";
      } else if (manualArchReady) {
        sendButton.textContent = blocked
          ? "Réessayer ASTRA dans l'onglet actif (Work)"
          : "Lancer ASTRA dans l'onglet actif (Work)";
        sendButton.title =
          "Ouvrez ou sélectionnez d'abord la conversation ChatGPT en Work mode. DevCockpit n'ouvre ni ne choisit automatiquement le mode pour une gate ARCH.";
      } else {
        sendButton.textContent = "Réessayer après correction";
        sendButton.title =
          "Disponible seulement après un échec certain avant SEND_ARMED.";
      }

      const conflictMessage = bindingConflictMessage(
        sendState?.recovery?.binding_conflict,
      );
      if (conflictMessage) {
        showEntryError(entryError, conflictMessage);
      } else if (recoveryInProgress) {
        showEntryError(
          entryError,
          "Vérification automatique en cours : inspection passive et, au maximum, un rechargement de l’onglet exact. Aucun renvoi n’est permis.",
        );
      } else if (
        blocked &&
        sendState?.recovery?.status === "VERIFIED_NOT_SENT"
      ) {
        showEntryError(
          entryError,
          "Vérifié non envoyé : aucune occurrence exacte du prompt n’a été trouvée après resynchronisation et le prompt est resté dans le composer.",
        );
      } else if (ambiguous) {
        showEntryError(
          entryError,
          "Envoi ambigu — intervention requise. Utilisez « Vérifier l'envoi » comme dernier recours; aucun renvoi automatique n’est permis.",
        );
      } else if (manualArchReady && !sendState?.error_code) {
        showEntryError(
          entryError,
          "Gate ASTRA manuelle : ouvrez/sélectionnez l’onglet ChatGPT en Work mode, puis lancez la gate ici. Les prompts ARCH ne sont jamais envoyés automatiquement.",
        );
      } else if (sendState?.error_code) {
        showEntryError(entryError, sendState.error_code);
      }

      sendButton.addEventListener("click", async () => {
        sendButton.disabled = true;
        const type = ambiguous
          ? "devcockpit_resolve_ambiguous_send"
          : manualArchReady
            ? "devcockpit_send_arch_manually"
            : "devcockpit_retry_chatgpt_send";
        const result = await browser.runtime.sendMessage({
          type,
          deliveryId: entry.delivery_id,
        });
        if (!result?.ok && result?.error) {
          showEntryError(entryError, result.error);
          sendButton.disabled = false;
          return;
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

  function renderSentPrompts(entries, sendStates) {
    sentElement.replaceChildren();
    sentEmptyElement.hidden = entries.length !== 0;
    const sendByDelivery = new Map(
      sendStates.map((entry) => [entry.delivery_id, entry]),
    );

    for (const entry of entries) {
      const fragment = sentTemplate.content.cloneNode(true);
      fragment.querySelector(".session").textContent = entry.session;
      const sendState = sendByDelivery.get(entry.delivery_id) || null;
      const sentAt = new Date(entry.sent_at).toLocaleString();
      fragment.querySelector(".sent-at").textContent =
        sendState?.recovery?.status === "CONFIRMED"
          ? "Envoi confirmé après resynchronisation · " + sentAt
          : sentAt;

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

    renderPromptQueue(
      Array.isArray(state.queue) ? state.queue : [],
      Array.isArray(state.sendStates) ? state.sendStates : [],
    );
    renderSentPrompts(
      Array.isArray(state.sentPrompts) ? state.sentPrompts : [],
      Array.isArray(state.sendStates) ? state.sendStates : [],
    );
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
      message?.type === "devcockpit_send_state_changed" ||
      message?.type === "devcockpit_sent_prompts_changed" ||
      message?.type === "devcockpit_response_changed"
    ) {
      void refresh();
    }
  });

  void refresh();
})();
