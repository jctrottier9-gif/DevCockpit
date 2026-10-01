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
  constructor({ tagName = "DIV", attributes = {}, disabled = false } = {}) {
    this.tagName = tagName;
    this.attributes = { ...attributes };
    this.disabled = disabled;
    this.textContent = "";
    this.value = "";
    this.focused = false;
    this.clicked = false;
    this.events = [];
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  focus() {
    this.focused = true;
  }

  dispatchEvent(event) {
    this.events.push(event);
    return true;
  }

  click() {
    this.clicked = true;
  }
}

class FakeDocument {
  constructor(selectorMap) {
    this.selectorMap = selectorMap;
    this.defaultView = {
      InputEvent: FakeEvent,
      Event: FakeEvent,
    };
  }

  querySelectorAll(selector) {
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
  return new ChatGptPageAdapter(document, { afterInput: async () => {} });
}

test("adapter inserts prompt and clicks only the exact send button", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true" },
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const map = new Map([
    ['#prompt-textarea[contenteditable="true"]', [composer]],
    ['button[data-testid="send-button"]', [button]],
  ]);
  const adapter = await adapterFor(map);

  const result = await adapter.sendPrompt("Hello ChatGPT");

  assert.equal(result.ok, true);
  assert.equal(composer.textContent, "Hello ChatGPT");
  assert.equal(composer.focused, true);
  assert.equal(composer.events.some((event) => event.type === "input"), true);
  assert.equal(button.clicked, true);
});

test("missing composer fails closed without clicking", async () => {
  const button = new FakeElement({ tagName: "BUTTON" });
  const adapter = await adapterFor(
    new Map([['button[data-testid="send-button"]', [button]]]),
  );

  const result = await adapter.sendPrompt("Hello");

  assert.equal(result.ok, false);
  assert.equal(result.error, "composer_not_found");
  assert.equal(button.clicked, false);
});

test("missing send button preserves fail-closed behavior", async () => {
  const composer = new FakeElement({
    attributes: { contenteditable: "true" },
  });
  const adapter = await adapterFor(
    new Map([['#prompt-textarea[contenteditable="true"]', [composer]]]),
  );

  const result = await adapter.sendPrompt("Hello");

  assert.equal(result.ok, false);
  assert.equal(result.error, "send_button_not_found");
});

test("ambiguous composer fails closed", async () => {
  const first = new FakeElement({
    attributes: { contenteditable: "true" },
  });
  const second = new FakeElement({
    attributes: { contenteditable: "true" },
  });
  const button = new FakeElement({ tagName: "BUTTON" });
  const adapter = await adapterFor(
    new Map([
      ['#prompt-textarea[contenteditable="true"]', [first, second]],
      ['button[data-testid="send-button"]', [button]],
    ]),
  );

  const result = await adapter.sendPrompt("Hello");

  assert.equal(result.ok, false);
  assert.equal(result.error, "composer_ambiguous");
  assert.equal(button.clicked, false);
});
