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
  constructor({ tagName = "DIV", attributes = {}, disabled = false, text = "" } = {}) {
    this.tagName = tagName;
    this.attributes = { ...attributes };
    this.disabled = disabled;
    this.textContent = text;
    this.innerText = text;
    this.value = "";
    this.focused = false;
    this.clicked = false;
    this.events = [];
    this.children = new Set();
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  focus() { this.focused = true; }
  dispatchEvent(event) { this.events.push(event); return true; }
  click() { this.clicked = true; }
  contains(other) { return this.children.has(other); }
}

class FakeDocument {
  constructor(selectorMap) {
    this.selectorMap = selectorMap;
    this.defaultView = { InputEvent: FakeEvent, Event: FakeEvent };
    this.queryCount = 0;
  }
  querySelectorAll(selector) {
    this.queryCount += 1;
    return this.selectorMap.get(selector) || [];
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
    ['#prompt-textarea[contenteditable="true"]', [composer]],
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

test("missing or ambiguous composer fails closed", async () => {
  const button = new FakeElement({ tagName: "BUTTON" });
  let value = await adapterFor(new Map([['button[data-testid="send-button"]', [button]]]));
  let result = await value.adapter.preparePrompt("Hello");
  assert.equal(result.error, "composer_not_found");
  assert.equal(button.clicked, false);

  const first = new FakeElement({ attributes: { contenteditable: "true" } });
  const second = new FakeElement({ attributes: { contenteditable: "true" } });
  value = await adapterFor(new Map([
    ['#prompt-textarea[contenteditable="true"]', [first, second]],
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
    ['#prompt-textarea[contenteditable="true"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
  ]));

  const result = await adapter.preparePrompt("Hello");

  assert.equal(result.ok, false);
  assert.equal(result.error, "login_required");
  assert.equal(button.clicked, false);
});
