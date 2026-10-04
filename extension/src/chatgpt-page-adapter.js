(() => {
  "use strict";

  const namespace = (globalThis.DevCockpitCompanion ||= {});

  const COMPOSER_SELECTORS = Object.freeze([
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'form[data-chatgpt-composer] [contenteditable="true"][role="textbox"]',
    'form[data-type="unified-composer"] [contenteditable="true"][role="textbox"]',
    'form[data-type="unified-composer"] [data-lexical-editor="true"][role="textbox"]',
    '.composer-parent .ProseMirror[contenteditable="true"]',
    '.ProseMirror[contenteditable="true"][role="textbox"]',
    '[contenteditable="true"][data-lexical-editor="true"][role="textbox"]',
    'textarea[name="prompt-textarea"]',
    'textarea[aria-label="Chat with ChatGPT"]',
  ]);
  const SEND_BUTTON_SELECTORS = Object.freeze([
    'button[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[data-testid*="composer-send"]',
    'button[type="submit"][aria-label*="Send" i]',
    'button[type="submit"]',
  ]);
  const SEND_BUTTON_SELECTOR = SEND_BUTTON_SELECTORS[0];
  const LOGIN_SELECTORS = Object.freeze([
    'button[data-testid="login-button"]',
    'a[href="/auth/login"]',
  ]);
  const USER_MESSAGE_SELECTORS = Object.freeze([
    '[data-message-author-role="user"]',
    '[data-user-message-bubble]',
    '[data-chatgpt-search-unit-key$=":user"]',
  ]);
  const USER_MESSAGE_SELECTOR = USER_MESSAGE_SELECTORS[0];
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

  function visibleElement(document, element) {
    if (!element || typeof element.getAttribute !== "function") return false;
    if (
      element.hidden === true ||
      element.getAttribute("aria-hidden") === "true" ||
      element.hasAttribute?.("inert") ||
      element.hasAttribute?.("disabled")
    ) {
      return false;
    }
    if (typeof element.getClientRects === "function" && element.getClientRects().length === 0) {
      return false;
    }
    const view = document.defaultView || globalThis;
    if (typeof view.getComputedStyle === "function") {
      const style = view.getComputedStyle(element);
      if (style?.display === "none" || style?.visibility === "hidden") return false;
    }
    return true;
  }

  function normalizedText(value) {
    return String(value ?? "").replace(/\r\n/g, "\n").trim();
  }

  function comparableText(value) {
    return normalizedText(value).replace(/\s+/g, " ");
  }

  function sameText(left, right) {
    return comparableText(left) === comparableText(right);
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

  function editableMode(element) {
    if (!element || typeof element.getAttribute !== "function") return null;
    const value = element.getAttribute("contenteditable");
    return value === "true" || value === "plaintext-only" ? value : null;
  }

  function selectComposerContents(document, composer) {
    const view = document.defaultView || globalThis;
    const selection = view.getSelection?.();
    if (!selection || typeof document.createRange !== "function") {
      throw new ChatGptAdapterError("composer_selection_unavailable");
    }
    const range = document.createRange();
    range.selectNodeContents(composer);
    selection.removeAllRanges();
    selection.addRange(range);
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
    if (editableMode(composer)) {
      if (typeof document.execCommand !== "function") {
        throw new ChatGptAdapterError("composer_native_edit_unavailable");
      }
      selectComposerContents(document, composer);
      document.execCommand("delete", false);
      const inserted = document.execCommand("insertText", false, text);
      if (!inserted) throw new ChatGptAdapterError("composer_insert_failed");
      dispatchInput(document, composer, text);
      return;
    }
    throw new ChatGptAdapterError("composer_not_supported");
  }

  function composerDiagnostics(document) {
    const counts = [
      ["id", "#prompt-textarea"],
      ["testid", '[data-testid="prompt-textarea"]'],
      ["prose", ".ProseMirror"],
      ["role", '[role="textbox"]'],
      ["lexical", '[data-lexical-editor="true"]'],
      ["form", 'form[data-chatgpt-composer], form[data-type="unified-composer"]'],
    ];
    return counts
      .map(([label, selector]) => {
        try {
          return label + "=" + document.querySelectorAll(selector).length;
        } catch {
          return label + "=?";
        }
      })
      .join(",");
  }

  function exactComposer(document) {
    const composers = uniqueMatches(document, COMPOSER_SELECTORS).filter((element) =>
      visibleElement(document, element),
    );
    if (composers.length === 0) {
      throw new ChatGptAdapterError(
        "composer_not_found:" + composerDiagnostics(document),
      );
    }
    if (composers.length !== 1) throw new ChatGptAdapterError("composer_ambiguous");
    return composers[0];
  }

  function exactEnabledSendButton(document, composer) {
    const scope =
      typeof composer?.closest === "function"
        ? composer.closest("form") || document
        : document;
    const buttons = uniqueMatches(scope, SEND_BUTTON_SELECTORS).filter((element) =>
      visibleElement(document, element),
    );
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
        if (uniqueMatches(this.document, LOGIN_SELECTORS).length > 0) {
          throw new ChatGptAdapterError("login_required");
        }
        const composer = exactComposer(this.document);
        insertIntoComposer(this.document, composer, text);
        await this.afterInput();
        if (!sameText(elementText(composer), text)) {
          throw new ChatGptAdapterError("composer_insert_failed");
        }
        exactEnabledSendButton(this.document, composer);
        return {
          ok: true,
          baseline: {
            user_message_count: uniqueMatches(this.document, USER_MESSAGE_SELECTORS).length,
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
        if (!sameText(elementText(composer), text)) {
          throw new ChatGptAdapterError("composer_changed_before_send");
        }
        exactEnabledSendButton(this.document, composer).click();
        clicked = true;

        for (let index = 0; index < this.confirmationPollCount; index += 1) {
          await this.sleep(this.confirmationPollMs);
          const messages = uniqueMatches(this.document, USER_MESSAGE_SELECTORS);
          const matchingMessage = messages
            .slice(baseline.user_message_count)
            .some((element) => sameText(elementText(element), baseline.expected_text));
          let composerChanged = true;
          try {
            composerChanged =
              !sameText(elementText(exactComposer(this.document)), baseline.expected_text);
          } catch (error) {
            if (
              error instanceof ChatGptAdapterError &&
              !error.code.startsWith("composer_not_found")
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

    inspectPromptDelivery(text) {
      if (typeof text !== "string" || text.trim() === "") {
        return { ok: false, error: "prompt_empty" };
      }
      const matchingMessages = uniqueMatches(
        this.document,
        USER_MESSAGE_SELECTORS,
      ).filter((element) => sameText(elementText(element), text));

      let composerMatches = false;
      try {
        composerMatches = sameText(elementText(exactComposer(this.document)), text);
      } catch (error) {
        if (
          !(error instanceof ChatGptAdapterError) ||
          !error.code.startsWith("composer_not_found")
        ) {
          return {
            ok: false,
            error:
              error instanceof ChatGptAdapterError
                ? error.code
                : "chatgpt_adapter_failed",
          };
        }
      }

      if (composerMatches && matchingMessages.length === 0) {
        return { ok: true, state: "NOT_SENT" };
      }
      if (!composerMatches && matchingMessages.length === 1) {
        return {
          ok: true,
          state: "SENT",
          conversationUrl: canonicalConversationUrl(this.location?.href) || null,
        };
      }
      return { ok: false, error: "delivery_evidence_ambiguous" };
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
    SEND_BUTTON_SELECTORS,
    SEND_BUTTON_SELECTOR,
    visibleElement,
    editableMode,
    comparableText,
    sameText,
    LOGIN_SELECTORS,
    USER_MESSAGE_SELECTORS,
    USER_MESSAGE_SELECTOR,
    ASSISTANT_RESPONSE_SELECTOR,
    ChatGptAdapterError,
    ChatGptPageAdapter,
  };
})();
