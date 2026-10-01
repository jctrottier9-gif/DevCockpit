(() => {
  "use strict";

  const { ChatGptPageAdapter } = globalThis.DevCockpitCompanion.chatgpt;
  const adapter = new ChatGptPageAdapter(document);

  browser.runtime.onMessage.addListener((message) => {
    if (message?.type !== "devcockpit_send_prompt") {
      return undefined;
    }

    return Promise.resolve(adapter.sendPrompt(message.text));
  });
})();
