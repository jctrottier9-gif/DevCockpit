(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});

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

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  class PromptSendCoordinator {
    constructor({ queueStore, getActiveTabs, sendToTab }) {
      this.queueStore = queueStore;
      this.getActiveTabs = getActiveTabs;
      this.sendToTab = sendToTab;
    }

    async send(deliveryId) {
      let entry = null;
      let chatGptActionReportedSuccess = false;

      try {
        entry = await this.queueStore.markSendRequested(deliveryId);

        const tabs = await this.getActiveTabs();
        if (!Array.isArray(tabs) || tabs.length !== 1) {
          throw new Error("Aucun onglet actif unique");
        }

        const tab = tabs[0];
        if (!Number.isInteger(tab.id) || !isSupportedChatGptUrl(tab.url)) {
          throw new Error("Ouvrez la conversation ChatGPT cible dans l'onglet actif");
        }

        const response = await this.sendToTab(tab.id, {
          type: "devcockpit_send_prompt",
          text: entry.text,
        });

        if (!response || response.ok !== true) {
          throw new Error(response?.error || "L'adapter ChatGPT a refusé l'envoi");
        }
        chatGptActionReportedSuccess = true;

        await this.queueStore.remove(deliveryId);
        return { ok: true };
      } catch (error) {
        const message = errorText(error);

        if (entry && !chatGptActionReportedSuccess) {
          try {
            await this.queueStore.markQueued(deliveryId, message);
          } catch {
            // Preserve the first actionable error; storage failures remain visible
            // through the SEND_REQUESTED state when the prior write succeeded.
          }
        }

        if (chatGptActionReportedSuccess) {
          return {
            ok: false,
            error:
              "L'action Envoyer a été déclenchée dans ChatGPT, mais le nettoyage local a échoué. Vérifiez la conversation avant de réessayer.",
          };
        }

        return { ok: false, error: message };
      }
    }
  }

  namespace.send = {
    isSupportedChatGptUrl,
    PromptSendCoordinator,
  };
})();
