(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});

  function errorText(error) {
    return error instanceof Error ? error.message : String(error);
  }

  class PromptSendCoordinator {
    constructor({ queueStore, sentPromptStore, router, sendToTab }) {
      this.queueStore = queueStore;
      this.sentPromptStore = sentPromptStore;
      this.router = router;
      this.sendToTab = sendToTab;
    }

    async send(deliveryId) {
      let entry = null;
      let chatGptActionReportedSuccess = false;

      try {
        entry = await this.queueStore.markSendRequested(deliveryId);
        const target = await this.router.route({
          session: entry.session,
          routing: entry.routing,
        });
        const tab = await this.router.revalidateTarget({
          session: entry.session,
          routing: entry.routing,
          tabId: target.tabId,
        });

        const response = await this.sendToTab(tab.id, {
          type: "devcockpit_send_prompt",
          text: entry.text,
        });
        if (!response || response.ok !== true) {
          throw new Error(response?.error || "L'adapter ChatGPT a refusé l'envoi");
        }
        chatGptActionReportedSuccess = true;

        await this.sentPromptStore.recordSent({
          deliveryId,
          session: entry.session,
          tabId: tab.id,
          conversationUrl: tab.url || target.url || null,
        });
        await this.queueStore.remove(deliveryId);
        return { ok: true };
      } catch (error) {
        const message = errorText(error);

        if (entry && !chatGptActionReportedSuccess) {
          try {
            await this.queueStore.markQueued(deliveryId, message);
          } catch {
            // Keep the first actionable failure visible.
          }
        }

        if (chatGptActionReportedSuccess) {
          return {
            ok: false,
            error:
              "L'action Envoyer a été déclenchée dans ChatGPT, mais le contexte envoyé n'a pas pu être conservé localement. Vérifiez la conversation avant de réessayer.",
          };
        }
        return { ok: false, error: message };
      }
    }
  }

  namespace.send = {
    PromptSendCoordinator,
  };
})();
