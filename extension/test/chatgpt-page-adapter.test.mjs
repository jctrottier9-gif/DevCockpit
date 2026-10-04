import assert from "node:assert/strict";
import test from "node:test";
import { loadClassicScripts } from "./helpers.mjs";

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = init.bubbles;
    this.data = init.data;
  }
}

class FakeElement {
  constructor({
    tagName = "DIV",
    attributes = {},
    disabled = false,
    text = "",
    visible = true,
  } = {}) {
    this.tagName = tagName;
    this.attributes = { ...attributes };
    this.disabled = disabled;
    this.visible = visible;
    this._text = text;
    this.value = "";
    this.focused = false;
    this.clicked = false;
    this.events = [];
    this.children = new Set();
  }
  get textContent() { return this._text; }
  set textContent(value) { this._text = value; }
  get innerText() { return this._text; }
  set innerText(value) { this._text = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }
  getClientRects() { return this.visible ? [{}] : []; }
  focus() {
    this.focused = true;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  dispatchEvent(event) { this.events.push(event); return true; }
  click() { this.clicked = true; }
  contains(other) { return this.children.has(other); }
}

class FakeDocument {
  constructor(selectorMap) {
    this.selectorMap = selectorMap;
    this.queryCount = 0;
    this.activeElement = null;
    this.selectedElement = null;
    const selection = {
      removeAllRanges: () => {
        this.selectedElement = null;
      },
      addRange: (range) => {
        this.selectedElement = range.element || null;
      },
    };
    this.defaultView = {
      InputEvent: FakeEvent,
      Event: FakeEvent,
      getSelection: () => selection,
    };
    for (const elements of selectorMap.values()) {
      for (const element of elements) element.ownerDocument = this;
    }
  }
  querySelectorAll(selector) {
    this.queryCount += 1;
    return this.selectorMap.get(selector) || [];
  }
  createRange() {
    const range = {
      element: null,
      selectNodeContents: (element) => {
        range.element = element;
      },
      collapse: () => {},
    };
    return range;
  }
  execCommand(command, _showUi, value) {
    const element = this.selectedElement || this.activeElement;
    if (!element) return false;
    if (command === "delete") {
      element.textContent = "";
      return true;
    }
    if (command === "insertText") {
      element.textContent = (element.textContent || "") + String(value ?? "");
      return true;
    }
    return false;
  }
}

async function adapterFor(selectorMap) {
  const context = await loadClassicScripts(
    ["src/chatgpt-page-adapter.js"],
    { InputEvent: FakeEvent, Event: FakeEvent },
  );
  const { ChatGptPageAdapter } = context.DevCockpitCompanion.chatgpt;
  const document = new FakeDocument(selectorMap);
  return {
    adapter: new ChatGptPageAdapter(document, { afterInput: async () => {} }),
    document,
  };
}

test("adapter prepares without click then confirms exact sent user message", async () => {
  const composer = new FakeElement({ attributes: { contenteditable: "true" } });
  const button = new FakeElement({ tagName: "BUTTON" });
  const userMessage = new FakeElement({ text: "Hello ChatGPT" });
  const selectorMap = new Map([
    ['#prompt-textarea', [composer]],
    ['[data-testid="prompt-textarea"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', []],
  ]);
  const { adapter } = await adapterFor(selectorMap);
  adapter.location = { href: "https://chatgpt.com/c/confirmed" };
  adapter.sleep = async () => {
    composer.textContent = "";
    composer.innerText = "";
    selectorMap.set('[data-message-author-role="user"]', [userMessage]);
  };

  const prepared = await adapter.preparePrompt("Hello ChatGPT");
  assert.equal(prepared.ok, true);
  assert.equal(button.clicked, false);

  const result = await adapter.commitPreparedPrompt(
    "Hello ChatGPT",
    prepared.baseline,
  );
  assert.equal(result.ok, true);
  assert.equal(result.conversationUrl, "https://chatgpt.com/c/confirmed");
  assert.equal(button.clicked, true);
});

test("adapter accepts plaintext-only canonical composer", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "plaintext-only" },
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-testid="prompt-textarea"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', []],
  ]));

  const prepared = await adapter.preparePrompt("Hello plaintext composer");

  assert.equal(prepared.ok, true);
  assert.equal(composer.textContent, "Hello plaintext composer");
  assert.equal(button.clicked, false);
});

test("canonical composer found by multiple selectors is deduplicated", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true" },
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-testid="prompt-textarea"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', []],
  ]));

  const prepared = await adapter.preparePrompt("Deduplicated composer");

  assert.equal(prepared.ok, true);
  assert.equal(button.clicked, false);
});

test("adapter accepts visible ProseMirror role textbox fallback", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const { adapter } = await adapterFor(new Map([
    ['.ProseMirror[contenteditable="true"][role="textbox"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', []],
  ]));

  const prepared = await adapter.preparePrompt("Fallback composer");

  assert.equal(prepared.ok, true);
  assert.equal(composer.textContent, "Fallback composer");
  assert.equal(button.clicked, false);
});

test("prepare replaces stale visible DOM through native editor commands", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "stale visible prompt",
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const { adapter, document } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', []],
  ]));
  const commands = [];
  const original = document.execCommand.bind(document);
  document.execCommand = (command, showUi, value) => {
    commands.push(command);
    return original(command, showUi, value);
  };

  const prepared = await adapter.preparePrompt("Fresh prompt");

  assert.equal(prepared.ok, true);
  assert.deepEqual(commands, ["delete", "insertText"]);
  assert.equal(composer.textContent, "Fresh prompt");
  assert.equal(button.clicked, false);
});

test("prompt inspection proves NOT_SENT only when composer owns exact prompt", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "Prompt A",
  });
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-message-author-role="user"]', []],
  ]));

  const result = adapter.inspectPromptDelivery("Prompt A");

  assert.equal(result.ok, true);
  assert.equal(result.state, "NOT_SENT");
});

test("prompt inspection proves SENT when one matching user message exists and composer changed", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "",
  });
  const user = new FakeElement({ text: "Prompt A" });
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-message-author-role="user"]', [user]],
  ]));
  adapter.location = { href: "https://chatgpt.com/c/abc-123" };

  const result = adapter.inspectPromptDelivery("Prompt A");

  assert.equal(result.ok, true);
  assert.equal(result.state, "SENT");
  assert.equal(result.conversationUrl, "https://chatgpt.com/c/abc-123");
});

test("rendered Markdown user text matches the complete source prompt", async () => {
  const context = await loadClassicScripts(
    ["src/chatgpt-page-adapter.js"],
    { InputEvent: FakeEvent, Event: FakeEvent },
  );
  const { sameRenderedPromptText } = context.DevCockpitCompanion.chatgpt;
  const source = [
    "Tu travailles sur le dépôt GitHub `tchi99/RessourcePlanner`.",
    "",
    "## Important",
    "- lis `AGENTS.md`;",
    "- PR : [#606](https://github.com/tchi99/RessourcePlanner/pull/606)",
    "",
    "```text",
    "COCKPIT_PIPELINE_V3",
    "```",
  ].join("\n");
  const rendered = [
    "Tu travailles sur le dépôt GitHub tchi99/RessourcePlanner.",
    "",
    "Important",
    "• lis AGENTS.md;",
    "• PR : #606",
    "",
    "COCKPIT_PIPELINE_V3",
  ].join("\n");

  assert.equal(sameRenderedPromptText(rendered, source), true);
  assert.equal(
    sameRenderedPromptText(
      rendered.replace("COCKPIT_PIPELINE_V3", "COCKPIT_PIPELINE"),
      source,
    ),
    false,
  );
});

test("flattened 594C-style prompt matches by exact lexical fingerprint", async () => {
  const context = await loadClassicScripts(
    ["src/chatgpt-page-adapter.js"],
    { InputEvent: FakeEvent, Event: FakeEvent },
  );
  const { sameRenderedPromptText, lexicalPromptFingerprint } =
    context.DevCockpitCompanion.chatgpt;

  const source = [
    "Tu travailles sur le dépôt GitHub `tchi99/RessourcePlanner`.",
    "",
    "Prends en charge la tranche 594C — commandes audit concurrence et cycle de vie.",
    "",
    "Contexte canonique :",
    "- Project : RessourcePlanner",
    "- WorkItem : 594C",
    "- Parent : #594",
    "- Roadmap maître : #55",
    "- Statut canonique : READY",
    "",
    "Travaille sur le main actuel et synchronise-toi avec le vrai main avant de commencer.",
  ].join("\n");

  const flattened = [
    "Tu travailles sur le dépôt GitHub tchi99/RessourcePlanner.",
    "Prends en charge la tranche 594C — commandes audit concurrence et cycle de vie.",
    "Contexte canonique : - Project : RessourcePlanner - WorkItem : 594C - Parent : #594",
    "- Roadmap maître : #55 - Statut canonique : READY",
    "Travaille sur le main actuel et synchronise-toi avec le vrai main avant de commencer.",
  ].join(" ");

  assert.equal(sameRenderedPromptText(flattened, source), true);
  assert.deepEqual(
    Array.from(lexicalPromptFingerprint(flattened)),
    Array.from(lexicalPromptFingerprint(source)),
  );
  assert.equal(
    sameRenderedPromptText(
      flattened.replace("Statut canonique : READY", "Statut canonique : DONE"),
      source,
    ),
    false,
  );
});

test("lexical prompt comparison remains full-content and order sensitive", async () => {
  const context = await loadClassicScripts(
    ["src/chatgpt-page-adapter.js"],
    { InputEvent: FakeEvent, Event: FakeEvent },
  );
  const { sameRenderedPromptText } = context.DevCockpitCompanion.chatgpt;

  assert.equal(
    sameRenderedPromptText(
      "Alpha beta gamma delta",
      "Alpha beta gamma delta",
    ),
    true,
  );
  assert.equal(
    sameRenderedPromptText(
      "Alpha beta gamma",
      "Alpha beta gamma delta",
    ),
    false,
  );
  assert.equal(
    sameRenderedPromptText(
      "Alpha gamma beta delta",
      "Alpha beta gamma delta",
    ),
    false,
  );
});

test("prompt inspection proves SENT for a Markdown-rendered logical user turn", async () => {
  const source = [
    "Tu travailles sur le dépôt GitHub `tchi99/RessourcePlanner`.",
    "",
    "- consulte `AGENTS.md`;",
    "- roadmap **#55**.",
  ].join("\n");
  const rendered = [
    "Tu travailles sur le dépôt GitHub tchi99/RessourcePlanner.",
    "",
    "• consulte AGENTS.md;",
    "• roadmap #55.",
  ].join("\n");
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "",
  });
  const outer = new FakeElement({ text: rendered });
  const inner = new FakeElement({ text: rendered });
  outer.children.add(inner);
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-message-author-role="user"]', [outer]],
    ['[data-user-message-bubble]', [inner]],
  ]));
  adapter.location = { href: "https://chatgpt.com/c/abc-123" };

  const result = adapter.inspectPromptDelivery(source);

  assert.equal(result.ok, true);
  assert.equal(result.state, "SENT");
  assert.equal(result.conversationUrl, "https://chatgpt.com/c/abc-123");
});

test("nested DOM candidates for one user turn count as one SENT proof", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "",
  });
  const outer = new FakeElement({ text: "Prompt A" });
  const inner = new FakeElement({ text: "Prompt A" });
  outer.children.add(inner);
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-message-author-role="user"]', [outer]],
    ['[data-user-message-bubble]', [inner]],
  ]));
  adapter.location = { href: "https://chatgpt.com/c/abc-123" };

  const result = adapter.inspectPromptDelivery("Prompt A");

  assert.equal(result.ok, true);
  assert.equal(result.state, "SENT");
});

test("two distinct matching user turns remain ambiguous", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "",
  });
  const first = new FakeElement({ text: "Prompt A" });
  const second = new FakeElement({ text: "Prompt A" });
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-message-author-role="user"]', [first, second]],
  ]));

  const result = adapter.inspectPromptDelivery("Prompt A");

  assert.equal(result.ok, false);
  assert.match(result.error, /logical=2/);
  assert.match(result.error, /matching=2/);
});

test("post-send confirmation uses logical user-turn count, not nested DOM count", async () => {
  const composer = new FakeElement({ attributes: { contenteditable: "true" } });
  const button = new FakeElement({ tagName: "BUTTON" });
  const priorOuter = new FakeElement({ text: "Earlier" });
  const priorInner = new FakeElement({ text: "Earlier" });
  priorOuter.children.add(priorInner);
  const selectorMap = new Map([
    ['#prompt-textarea', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', [priorOuter]],
    ['[data-user-message-bubble]', [priorInner]],
  ]);
  const { adapter } = await adapterFor(selectorMap);
  const prepared = await adapter.preparePrompt("Prompt A");
  assert.equal(prepared.ok, true);
  assert.equal(prepared.baseline.user_message_count, 1);

  const sentOuter = new FakeElement({ text: "Prompt A" });
  const sentInner = new FakeElement({ text: "Prompt A" });
  sentOuter.children.add(sentInner);
  adapter.location = { href: "https://chatgpt.com/c/abc-123" };
  adapter.sleep = async () => {
    composer.textContent = "";
    selectorMap.set('[data-message-author-role="user"]', [priorOuter, sentOuter]);
    selectorMap.set('[data-user-message-bubble]', [priorInner, sentInner]);
  };

  const result = await adapter.commitPreparedPrompt("Prompt A", prepared.baseline);

  assert.equal(result.ok, true);
  assert.equal(button.clicked, true);
});

test("prompt inspection stays ambiguous when composer and user turn both match", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
    text: "Prompt A",
  });
  const user = new FakeElement({ text: "Prompt A" });
  const { adapter } = await adapterFor(new Map([
    ['#prompt-textarea', [composer]],
    ['[data-message-author-role="user"]', [user]],
  ]));

  const result = adapter.inspectPromptDelivery("Prompt A");

  assert.equal(result.ok, false);
  assert.match(result.error, /^delivery_evidence_ambiguous:/);
});

test("hidden textarea fallback is ignored in favor of visible editor", async () => {
  const hiddenTextarea = new FakeElement({
    tagName: "TEXTAREA",
    attributes: { name: "prompt-textarea" },
    visible: false,
  });
  const composer = new FakeElement({
    attributes: { contenteditable: "true", role: "textbox" },
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const { adapter } = await adapterFor(new Map([
    ['textarea[name="prompt-textarea"]', [hiddenTextarea]],
    ['.ProseMirror[contenteditable="true"][role="textbox"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
    ['[data-message-author-role="user"]', []],
  ]));

  const prepared = await adapter.preparePrompt("Visible only");

  assert.equal(prepared.ok, true);
  assert.equal(hiddenTextarea.value, "");
  assert.equal(composer.textContent, "Visible only");
});

test("missing or ambiguous composer fails closed", async () => {
  const button = new FakeElement({ tagName: "BUTTON" });
  let value = await adapterFor(new Map([['button[data-testid="send-button"]', [button]]]));
  let result = await value.adapter.preparePrompt("Hello");
  assert.match(result.error, /^composer_not_found:/);
  assert.equal(button.clicked, false);

  const first = new FakeElement({ attributes: { contenteditable: "true" } });
  const second = new FakeElement({ attributes: { contenteditable: "true" } });
  value = await adapterFor(new Map([
    ['#prompt-textarea', [first]],
    ['[data-testid="prompt-textarea"]', [second]],
    ['button[data-testid="send-button"]', [button]],
  ]));
  result = await value.adapter.preparePrompt("Hello");
  assert.equal(result.error, "composer_ambiguous");
  assert.equal(button.clicked, false);
});

test("assistant DOM is inspected only by explicit one-shot call", async () => {
  const first = new FakeElement({ text: "First answer\n\n- item" });
  const second = new FakeElement({ text: "Second answer\ncode block" });
  const { adapter, document } = await adapterFor(new Map([
    ['[data-message-author-role="assistant"]', [first, second]],
  ]));
  assert.equal(document.queryCount, 0);
  const result = adapter.listAssistantResponses();
  assert.equal(result.ok, true);
  assert.equal(document.queryCount, 1);
  assert.equal(result.candidates.length, 2);
  assert.equal(result.candidates[0].text, "First answer\n\n- item");
  assert.equal(result.candidates[1].text, "Second answer\ncode block");
});

test("no, blank or ambiguous assistant response fails closed", async () => {
  let value = await adapterFor(new Map());
  let result = value.adapter.listAssistantResponses();
  assert.equal(result.error, "assistant_response_not_found");

  value = await adapterFor(new Map([
    ['[data-message-author-role="assistant"]', [new FakeElement({ text: "   " })]],
  ]));
  result = value.adapter.listAssistantResponses();
  assert.equal(result.error, "assistant_response_empty");

  const outer = new FakeElement({ text: "outer" });
  const inner = new FakeElement({ text: "inner" });
  outer.children.add(inner);
  value = await adapterFor(new Map([
    ['[data-message-author-role="assistant"]', [outer, inner]],
  ]));
  result = value.adapter.listAssistantResponses();
  assert.equal(result.error, "assistant_response_dom_ambiguous");
});


test("logged-out ChatGPT page is BLOCKED before any send click", async () => {
  const login = new FakeElement({ tagName: "BUTTON" });
  const composer = new FakeElement({ attributes: { contenteditable: "true" } });
  const button = new FakeElement({ tagName: "BUTTON" });
  const { adapter } = await adapterFor(new Map([
    ['button[data-testid="login-button"]', [login]],
    ['#prompt-textarea', [composer]],
    ['button[data-testid="send-button"]', [button]],
  ]));

  const result = await adapter.preparePrompt("Hello");

  assert.equal(result.ok, false);
  assert.equal(result.error, "login_required");
  assert.equal(button.clicked, false);
});
