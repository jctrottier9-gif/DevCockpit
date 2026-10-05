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

  const MARKDOWN_ESCAPABLE_PUNCTUATION =
    "!\\\"#$%&'()*+,-./:;<=>?@[\\\\]^_\`{|}~";

  function normalizeRenderedPromptSource(value) {
    let text = normalizedText(value)
      .replace(/(?:&#x0*20;|&#0*32;|&nbsp;)/gi, " ")
      .replace(/\\\\\n/g, "\n");

    text = text.replace(/\\\\(.)/g, (match, character) =>
      MARKDOWN_ESCAPABLE_PUNCTUATION.includes(character) ? character : match,
    );
    return text;
  }

  function renderedPromptComparableText(value) {
    let text = normalizeRenderedPromptSource(value);
    text = text
      .replace(/^ {0,3}\x60{3}[^\n]*$/gm, "")
      .replace(/!\[([^\]]*)\]\([^\n)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^\n)]*\)/g, "$1")
      .replace(/\x60([^\x60\n]+)\x60/g, "$1")
      .replace(/^ {0,3}#{1,6}\s+/gm, "")
      .replace(/^ {0,3}>\s?/gm, "")
      .replace(/^ {0,3}(?:[-+*•◦]|\d+[.)])\s+/gm, "")
      .replace(/\*\*([^*\n]+)\*\*/g, "$1")
      .replace(/__([^_\n]+)__/g, "$1")
      .replace(/~~([^~\n]+)~~/g, "$1");
    return comparableText(text);
  }

  function lexicalPromptFingerprint(value) {
    const rendered = renderedPromptComparableText(value);
    const normalized = String(rendered ?? "")
      .normalize("NFKC")
      .toLocaleLowerCase("fr-CA");
    return normalized.match(/[\p{L}\p{N}]+/gu) || [];
  }

  function sameRenderedPromptText(renderedText, sourcePrompt) {
    const rendered = lexicalPromptFingerprint(renderedText);
    const source = lexicalPromptFingerprint(sourcePrompt);
    if (rendered.length !== source.length) return false;
    return rendered.every((token, index) => token === source[index]);
  }

  function elementsOverlap(left, right) {
    if (!left || !right) return false;
    return Boolean(left.contains?.(right) || right.contains?.(left));
  }

  function groupLogicalUserTurns(document) {
    const candidates = uniqueMatches(document, USER_MESSAGE_SELECTORS).filter((element) =>
      visibleElement(document, element),
    );
    const groups = [];
    for (const candidate of candidates) {
      const touching = [];
      for (let index = 0; index < groups.length; index += 1) {
        if (groups[index].some((member) => elementsOverlap(member, candidate))) {
          touching.push(index);
        }
      }
      if (touching.length === 0) {
        groups.push([candidate]);
        continue;
      }
      const merged = [candidate];
      for (let offset = touching.length - 1; offset >= 0; offset -= 1) {
        const index = touching[offset];
        merged.push(...groups[index]);
        groups.splice(index, 1);
      }
      groups.push([...new Set(merged)]);
    }
    return groups;
  }

  function authoredNodeText(node) {
    if (!node) return "";
    if (typeof node.cloneNode !== "function") {
      return normalizedText(node.textContent);
    }
    const clone = node.cloneNode(true);
    if (typeof clone.querySelectorAll === "function") {
      for (const control of clone.querySelectorAll(
        'button, [role="button"], [aria-hidden="true"]',
      )) {
        control.remove?.();
      }
      for (const block of clone.querySelectorAll("p, li, pre, br")) {
        block.append?.("\n");
      }
    }
    return normalizedText(clone.textContent);
  }

  function userTurnTextCandidates(element) {
    const values = new Set();
    const add = (value) => {
      const normalized = normalizedText(value);
      if (normalized) values.add(normalized);
    };

    add(elementText(element));
    add(authoredNodeText(element));

    if (typeof element?.querySelectorAll === "function") {
      const targets = [
        ...element.querySelectorAll(
          '[data-testid="collapsible-user-message-content"], [data-search-result-target], .whitespace-pre-wrap, .markdown',
        ),
      ];
      for (const target of targets) {
        const nested = targets.some(
          (other) =>
            other !== target &&
            other.contains?.(target),
        );
        if (nested) continue;
        add(elementText(target));
        add(authoredNodeText(target));
      }
    }
    return [...values];
  }

  function userTurnMatchesText(group, text) {
    return group.some((element) =>
      userTurnTextCandidates(element).some((candidate) =>
        sameRenderedPromptText(candidate, text),
      ),
    );
  }

  function promptMismatchDiagnostics(groups, text) {
    const expected = lexicalPromptFingerprint(text);
    let best = null;

    groups.forEach((group, turnIndex) => {
      for (const element of group) {
        for (const candidate of userTurnTextCandidates(element)) {
          const observed = lexicalPromptFingerprint(candidate);
          let prefix = 0;
          const limit = Math.min(expected.length, observed.length);
          while (prefix < limit && expected[prefix] === observed[prefix]) {
            prefix += 1;
          }
          const score = {
            turnIndex,
            prefix,
            observedLength: observed.length,
            expectedToken: expected[prefix] || "eof",
            observedToken: observed[prefix] || "eof",
          };
          if (
            best === null ||
            score.prefix > best.prefix ||
            (
              score.prefix === best.prefix &&
              Math.abs(score.observedLength - expected.length) <
                Math.abs(best.observedLength - expected.length)
            )
          ) {
            best = score;
          }
        }
      }
    });

    if (best === null) {
      return "best_turn=0,prefix=0,source_tokens=" + expected.length;
    }
    const safeToken = (value) =>
      String(value).slice(0, 24).replace(/[^\p{L}\p{N}_-]/gu, "_");
    return [
      "best_turn=" + (best.turnIndex + 1),
      "prefix=" + best.prefix,
      "source_tokens=" + expected.length,
      "observed_tokens=" + best.observedLength,
      "expected=" + safeToken(best.expectedToken),
      "observed=" + safeToken(best.observedToken),
    ].join(",");
  }

  function userTurnControlScopes(element) {
    const scopes = [element];
    if (typeof element?.closest === "function") {
      for (const selector of ['[data-testid^="conversation-turn-"]', "article"]) {
        const scope = element.closest(selector);
        if (scope && !scopes.includes(scope)) scopes.push(scope);
      }
    }
    return scopes;
  }

  function isShowMoreControl(control, scope) {
    if (!control) return false;
    const testId = control.getAttribute?.("data-testid") || "";
    const label = comparableText(
      control.getAttribute?.("aria-label") || elementText(control),
    ).toLocaleLowerCase("fr-CA");
    const showMore =
      label.includes("show more") ||
      label.includes("afficher plus") ||
      label.includes("voir plus");

    if (testId === "collapsible-user-message-toggle") {
      const root = control.closest?.(
        '[data-testid="collapsible-user-message-root"]',
      ) || scope;
      const checkbox = root?.querySelector?.(
        '[data-testid="collapsible-user-message-toggle-checkbox"]',
      );
      if (checkbox?.checked === true) return false;
      return showMore || label === "";
    }
    return showMore;
  }

  function expandCollapsedUserTurns(groups) {
    let clicked = false;
    const visited = new Set();
    const selector = [
      '[data-testid="collapsible-user-message-toggle"]',
      'button',
      '[role="button"]',
      'label[for]',
    ].join(", ");
    for (const group of groups) {
      for (const element of group) {
        for (const scope of userTurnControlScopes(element)) {
          if (!scope || visited.has(scope) || typeof scope.querySelectorAll !== "function") {
            continue;
          }
          visited.add(scope);
          for (const control of scope.querySelectorAll(selector)) {
            if (!isShowMoreControl(control, scope)) continue;
            control.click?.();
            clicked = true;
          }
        }
      }
    }
    return clicked;
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
        parts.length < 2 ||
        parts[parts.length - 2] !== "c" ||
        !parts[parts.length - 1]
      ) {
        return null;
      }
      return "https://chatgpt.com/c/" + parts[parts.length - 1];
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
            user_message_count: groupLogicalUserTurns(this.document).length,
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
          const userTurns = groupLogicalUserTurns(this.document);
          const matchingMessage = userTurns
            .slice(baseline.user_message_count)
            .some((group) => userTurnMatchesText(group, baseline.expected_text));
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

    async inspectPromptDelivery(text) {
      if (typeof text !== "string" || text.trim() === "") {
        return { ok: false, error: "prompt_empty" };
      }
      let rawUserMessages = uniqueMatches(
        this.document,
        USER_MESSAGE_SELECTORS,
      ).filter((element) => visibleElement(this.document, element));
      let userTurns = groupLogicalUserTurns(this.document);
      let matchingTurns = userTurns.filter((group) =>
        userTurnMatchesText(group, text),
      );

      if (matchingTurns.length === 0 && expandCollapsedUserTurns(userTurns)) {
        await this.sleep(100);
        rawUserMessages = uniqueMatches(
          this.document,
          USER_MESSAGE_SELECTORS,
        ).filter((element) => visibleElement(this.document, element));
        userTurns = groupLogicalUserTurns(this.document);
        matchingTurns = userTurns.filter((group) =>
          userTurnMatchesText(group, text),
        );
      }

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

      if (composerMatches && matchingTurns.length === 0) {
        return { ok: true, state: "NOT_SENT" };
      }
      if (!composerMatches && matchingTurns.length === 1) {
        return {
          ok: true,
          state: "SENT",
          conversationUrl: canonicalConversationUrl(this.location?.href) || null,
        };
      }
      return {
        ok: false,
        error:
          "delivery_evidence_ambiguous:" +
          [
            "composer=" + (composerMatches ? "1" : "0"),
            "raw=" + rawUserMessages.length,
            "logical=" + userTurns.length,
            "matching=" + matchingTurns.length,
            promptMismatchDiagnostics(userTurns, text),
          ].join(","),
      };
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
    normalizeRenderedPromptSource,
    renderedPromptComparableText,
    lexicalPromptFingerprint,
    sameRenderedPromptText,
    elementsOverlap,
    groupLogicalUserTurns,
    authoredNodeText,
    userTurnTextCandidates,
    userTurnMatchesText,
    promptMismatchDiagnostics,
    userTurnControlScopes,
    isShowMoreControl,
    expandCollapsedUserTurns,
    LOGIN_SELECTORS,
    USER_MESSAGE_SELECTORS,
    USER_MESSAGE_SELECTOR,
    ASSISTANT_RESPONSE_SELECTOR,
    ChatGptAdapterError,
    ChatGptPageAdapter,
  };
})();
