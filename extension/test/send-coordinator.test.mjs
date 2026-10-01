import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";

async function setup({ response = { ok: true }, tabUrl = "https://chatgpt.com/c/abc" } = {}) {
  const storage = createMemoryStorage();
  const context = await loadClassicScripts([
    "src/queue-store.js",
    "src/response-store.js",
    "src/send-coordinator.js",
  ]);
  const { QueueStore } = context.DevCockpitCompanion.queue;
  const { SentPromptStore } = context.DevCockpitCompanion.responses;
  const { PromptSendCoordinator } = context.DevCockpitCompanion.send;
  const store = new QueueStore(storage);
  const sentPromptStore = new SentPromptStore(storage, {
    now: () => new Date("2026-10-01T16:00:00.000Z"),
  });
  await store.acceptPrompt({
    deliveryId: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-030",
    text: "send me",
  });
  const sent = [];
  const coordinator = new PromptSendCoordinator({
    queueStore: store,
    sentPromptStore,
    getActiveTabs: async () => [{ id: 7, url: tabUrl }],
    sendToTab: async (tabId, message) => {
      sent.push({ tabId, message });
      return response;
    },
  });
  return { store, sentPromptStore, sent, coordinator };
}

test("successful send removes active queue and preserves sent context", async () => {
  const { store, sentPromptStore, sent, coordinator } = await setup();
  const result = await coordinator.send(DELIVERY_ID);
  assert.equal(result.ok, true);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].message.text, "send me");
  assert.equal((await store.list()).length, 0);
  const contexts = await sentPromptStore.list();
  assert.equal(contexts.length, 1);
  assert.equal(contexts[0].delivery_id, DELIVERY_ID);
  assert.equal(contexts[0].session, "DevCockpit:DEV:DC-030");
  assert.equal(contexts[0].tab_id, 7);
  assert.equal(contexts[0].conversation_url, "https://chatgpt.com/c/abc");
});

test("failed ChatGPT action keeps prompt and creates no sent context", async () => {
  const { store, sentPromptStore, coordinator } = await setup({
    response: { ok: false, error: "composer_not_found" },
  });
  const result = await coordinator.send(DELIVERY_ID);
  assert.equal(result.ok, false);
  const queue = await store.list();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].local_status, "QUEUED");
  assert.equal(queue[0].last_error, "composer_not_found");
  assert.equal((await sentPromptStore.list()).length, 0);
});

test("non-ChatGPT tab fails closed", async () => {
  const { store, sentPromptStore, sent, coordinator } = await setup({
    tabUrl: "https://example.com/",
  });
  const result = await coordinator.send(DELIVERY_ID);
  assert.equal(result.ok, false);
  assert.equal(sent.length, 0);
  assert.equal((await store.list()).length, 1);
  assert.equal((await sentPromptStore.list()).length, 0);
});
