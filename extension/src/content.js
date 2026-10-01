(() => {
  "use strict";

  const { ChatGptPageAdapter } = globalThis.DevCockpitCompanion.chatgpt;
  const adapter = new ChatGptPageAdapter(document);

  browser.runtime.onMessage.addListener((message) => {
    if (message?.type === "devcockpit_send_prompt") {
      return Promise.resolve(adapter.sendPrompt(message.text));
    }
    if (message?.type === "devcockpit_list_chatgpt_responses") {
      return Promise.resolve(adapter.listAssistantResponses());
    }
    return undefined;
  });
})();
