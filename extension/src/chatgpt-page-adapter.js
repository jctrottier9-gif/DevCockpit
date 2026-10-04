(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});

  const COMPOSER_SELECTORS = Object.freeze([
    '#prompt-textarea[contenteditable="true"]',
    'textarea#prompt-textarea',
    '[data-testid="prompt-textarea"][contenteditable="true"]',
  ]);
  const SEND_BUTTON_SELECTOR = 'button[data-testid="send-button"]';
  const USER_MESSAGE_SELECTOR = '[data-message-author-role="user"]';
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
      for (const element of document.querySelectorAll(selector)) matches.add(element);
    }
    return [...matches];
  }

  function normalizedText(value) {
    return String(value ?? "").replace(/\r\n/g, "\n").trim();
  }

  function elementText(element) {
    if (!element) return "";
    if (element.tagName === "TEXTAREA") return normalizedText(element.value);
    const raw =
      typeof element.innerText === "string" ? element.innerText : element.textContent;
    return normalizedText(raw);
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
        ? Object.getOwnPropertyDescriptor(view.HTMLTextAreaElement.prototype, "value")
        : null;
      if (descriptor?.set) descriptor.set.call(composer, text);
      else composer.value = text;
      dispatchInput(document, composer, text);
      if (composer.value !== text) throw new ChatGptAdapterError("composer_insert_failed");
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

  function exactComposer(document) {
    const composers = uniqueMatches(document, COMPOSER_SELECTORS);
    if (composers.length === 0) throw new ChatGptAdapterError("composer_not_found");
    if (composers.length !== 1) throw new ChatGptAdapterError("composer_ambiguous");
    return composers[0];
  }

  function exactEnabledSendButton(document) {
    const buttons = uniqueMatches(document, [SEND_BUTTON_SELECTOR]);
    if (buttons.length === 0) throw new ChatGptAdapterError("send_button_not_found");
    if (buttons.length !== 1) throw new ChatGptAdapterError("send_button_ambiguous");
    const button = buttons[0];
    if (button.disabled === true || button.getAttribute?.("aria-disabled") === "true") {
      throw new ChatGptAdapterError("send_button_disabled");
    }
    return button;
  }

  function canonicalConversationUrl(rawUrl) {
    if (typeof rawUrl !== "string") return null;
    try {
      const parsed = new URL(rawUrl);
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (
        !["chatgpt.com", "chat.openai.com"].includes(parsed.hostname.toLowerCase()) ||
        parts.length !== 2 ||
        parts[0] !== "c" ||
        !parts[1]
      ) {
        return null;
      }
      return "https://chatgpt.com/c/" + parts[1];
    } catch {
      return null;
    }
  }

  function extractAssistantText(element) {
    const text = elementText(element);
    if (!text) throw new ChatGptAdapterError("assistant_response_empty");
    return text;
  }

  function assertNoNestedAssistantMatches(matches) {
    for (let index = 0; index < matches.length; index += 1) {
      const current = matches[index];
      if (typeof current.contains !== "function") continue;
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
      {
        afterInput = () => new Promise((resolve) => setTimeout(resolve, 0)),
        sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
        confirmationPollMs = 200,
        confirmationPollCount = 25,
        location = globalThis.location,
      } = {},
    ) {
      this.document = document;
      this.afterInput = afterInput;
      this.sleep = sleep;
      this.confirmationPollMs = confirmationPollMs;
      this.confirmationPollCount = confirmationPollCount;
      this.location = location;
    }

    async preparePrompt(text) {
      if (typeof text !== "string" || text.trim() === "") {
        return { ok: false, error: "prompt_empty" };
      }
      try {
        const composer = exactComposer(this.document);
        insertIntoComposer(this.document, composer, text);
        await this.afterInput();
        exactEnabledSendButton(this.document);
        return {
          ok: true,
          baseline: {
            user_message_count: uniqueMatches(this.document, [USER_MESSAGE_SELECTOR]).length,
            expected_text: normalizedText(text),
          },
        };
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

    async commitPreparedPrompt(text, baseline) {
      let clicked = false;
      try {
        if (
          !baseline ||
          !Number.isInteger(baseline.user_message_count) ||
          baseline.user_message_count < 0 ||
          baseline.expected_text !== normalizedText(text)
        ) {
          throw new ChatGptAdapterError("send_baseline_invalid");
        }
        const composer = exactComposer(this.document);
        if (elementText(composer) !== normalizedText(text)) {
          throw new ChatGptAdapterError("composer_changed_before_send");
        }
        exactEnabledSendButton(this.document).click();
        clicked = true;

        for (let index = 0; index < this.confirmationPollCount; index += 1) {
          await this.sleep(this.confirmationPollMs);
          const messages = uniqueMatches(this.document, [USER_MESSAGE_SELECTOR]);
          const matchingMessage = messages
            .slice(baseline.user_message_count)
            .some((element) => elementText(element) === baseline.expected_text);
          let composerChanged = true;
          try {
            composerChanged =
              elementText(exactComposer(this.document)) !== baseline.expected_text;
          } catch (error) {
            if (
              error instanceof ChatGptAdapterError &&
              error.code !== "composer_not_found"
            ) {
              throw error;
            }
          }
          if (matchingMessage && composerChanged) {
            return {
              ok: true,
              conversationUrl: canonicalConversationUrl(this.location?.href) || null,
            };
          }
        }
        throw new ChatGptAdapterError("send_confirmation_timeout");
      } catch (error) {
        return {
          ok: false,
          error:
            error instanceof ChatGptAdapterError
              ? error.code
              : "chatgpt_adapter_failed",
          ambiguous: clicked,
        };
      }
    }

    async sendPrompt(text) {
      const prepared = await this.preparePrompt(text);
      if (!prepared.ok) return prepared;
      return this.commitPreparedPrompt(text, prepared.baseline);
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
    USER_MESSAGE_SELECTOR,
    ASSISTANT_RESPONSE_SELECTOR,
    ChatGptAdapterError,
    ChatGptPageAdapter,
  };
})();
