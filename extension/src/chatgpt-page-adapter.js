(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});

  const COMPOSER_SELECTORS = Object.freeze([
    '#prompt-textarea[contenteditable="true"]',
    'textarea#prompt-textarea',
    '[data-testid="prompt-textarea"][contenteditable="true"]',
  ]);
  const SEND_BUTTON_SELECTOR = 'button[data-testid="send-button"]';
  const ASSISTANT_RESPONSE_SELECTOR = '[data-message-author-role="assistant"]';

  class ChatGptAdapterError extends Error {
    constructor(code) {
      super(code);
      this.name = "ChatGptAdapterError";
      this.code = code;
    }
  }

  function uniqueMatches(document, selectors) {
    const matches = new Set();
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        matches.add(element);
      }
    }
    return [...matches];
  }

  function dispatchInput(document, element, text) {
    const view = document.defaultView || globalThis;
    const InputEventConstructor = view.InputEvent || view.Event;
    element.dispatchEvent(
      new InputEventConstructor("input", {
        bubbles: true,
        inputType: "insertText",
        data: text,
      }),
    );
  }

  function insertIntoComposer(document, composer, text) {
    composer.focus();

    if (composer.tagName === "TEXTAREA") {
      const view = document.defaultView || globalThis;
      const descriptor = view.HTMLTextAreaElement
        ? Object.getOwnPropertyDescriptor(
            view.HTMLTextAreaElement.prototype,
            "value",
          )
        : null;
      if (descriptor?.set) {
        descriptor.set.call(composer, text);
      } else {
        composer.value = text;
      }
      dispatchInput(document, composer, text);
      if (composer.value !== text) {
        throw new ChatGptAdapterError("composer_insert_failed");
      }
      return;
    }

    if (composer.getAttribute("contenteditable") === "true") {
      composer.textContent = text;
      dispatchInput(document, composer, text);
      if (composer.textContent !== text) {
        throw new ChatGptAdapterError("composer_insert_failed");
      }
      return;
    }

    throw new ChatGptAdapterError("composer_not_supported");
  }

  function extractAssistantText(element) {
    const raw =
      typeof element.innerText === "string" ? element.innerText : element.textContent;
    if (typeof raw !== "string" || raw.trim() === "") {
      throw new ChatGptAdapterError("assistant_response_empty");
    }
    return raw.replace(/\r\n/g, "\n").trim();
  }

  function assertNoNestedAssistantMatches(matches) {
    for (let index = 0; index < matches.length; index += 1) {
      const current = matches[index];
      if (typeof current.contains !== "function") {
        continue;
      }
      for (let otherIndex = index + 1; otherIndex < matches.length; otherIndex += 1) {
        const other = matches[otherIndex];
        if (current.contains(other) || other.contains?.(current)) {
          throw new ChatGptAdapterError("assistant_response_dom_ambiguous");
        }
      }
    }
  }

  class ChatGptPageAdapter {
    constructor(
      document,
      { afterInput = () => new Promise((resolve) => setTimeout(resolve, 0)) } = {},
    ) {
      this.document = document;
      this.afterInput = afterInput;
    }

    async sendPrompt(text) {
      if (typeof text !== "string" || text.trim() === "") {
        return { ok: false, error: "Prompt vide" };
      }

      try {
        const composers = uniqueMatches(this.document, COMPOSER_SELECTORS);
        if (composers.length === 0) {
          throw new ChatGptAdapterError("composer_not_found");
        }
        if (composers.length !== 1) {
          throw new ChatGptAdapterError("composer_ambiguous");
        }

        insertIntoComposer(this.document, composers[0], text);
        await this.afterInput();

        const sendButtons = uniqueMatches(this.document, [SEND_BUTTON_SELECTOR]);
        if (sendButtons.length === 0) {
          throw new ChatGptAdapterError("send_button_not_found");
        }
        if (sendButtons.length !== 1) {
          throw new ChatGptAdapterError("send_button_ambiguous");
        }

        const sendButton = sendButtons[0];
        if (
          sendButton.disabled === true ||
          sendButton.getAttribute?.("aria-disabled") === "true"
        ) {
          throw new ChatGptAdapterError("send_button_disabled");
        }

        sendButton.click();
        return { ok: true };
      } catch (error) {
        return {
          ok: false,
          error:
            error instanceof ChatGptAdapterError
              ? error.code
              : "chatgpt_adapter_failed",
        };
      }
    }

    listAssistantResponses() {
      try {
        const matches = uniqueMatches(this.document, [ASSISTANT_RESPONSE_SELECTOR]);
        if (matches.length === 0) {
          throw new ChatGptAdapterError("assistant_response_not_found");
        }
        assertNoNestedAssistantMatches(matches);

        const candidates = matches.map((element, index) => {
          const text = extractAssistantText(element);
          return {
            candidateId: "assistant-" + (index + 1),
            text,
            preview: text.length > 240 ? text.slice(0, 240) + "…" : text,
          };
        });
        return { ok: true, candidates };
      } catch (error) {
        return {
          ok: false,
          error:
            error instanceof ChatGptAdapterError
              ? error.code
              : "chatgpt_adapter_failed",
        };
      }
    }
  }

  namespace.chatgpt = {
    COMPOSER_SELECTORS,
    SEND_BUTTON_SELECTOR,
    ASSISTANT_RESPONSE_SELECTOR,
    ChatGptAdapterError,
    ChatGptPageAdapter,
  };
})();
