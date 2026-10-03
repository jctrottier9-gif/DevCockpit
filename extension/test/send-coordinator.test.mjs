import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const ROUTING = {
  binding_version: 4,
  conversation_id: "abc",
  canonical_url: "https://chatgpt.com/c/abc",
};

async function setup({
  response = { ok: true },
  revalidateError = null,
  routing = ROUTING,
} = {}) {
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
    now: () => new Date("2026-10-03T16:00:00.000Z"),
  });
  await store.acceptPrompt({
    deliveryId: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-063A",
    text: "send me",
    routing,
  });
  const sent = [];
  const routed = [];
  const router = {
    async route(target) {
      routed.push({ phase: "route", ...target });
      return {
        kind: routing === null ? "PROVISIONAL" : "BOUND",
        tabId: 7,
        url: routing?.canonical_url || "https://chatgpt.com/",
      };
    },
    async revalidateTarget(target) {
      routed.push({ phase: "revalidate", ...target });
      if (revalidateError) throw new Error(revalidateError);
      return {
        id: 7,
        url: routing?.canonical_url || "https://chatgpt.com/",
      };
    },
  };
  const coordinator = new PromptSendCoordinator({
    queueStore: store,
    sentPromptStore,
    router,
    sendToTab: async (tabId, message) => {
      sent.push({ tabId, message });
      return response;
    },
  });
  return { store, sentPromptStore, sent, routed, coordinator };
}

test("manual send routes and revalidates exact target before DOM action", async () => {
  const { store, sentPromptStore, sent, routed, coordinator } = await setup();
  assert.equal(sent.length, 0);

  const result = await coordinator.send(DELIVERY_ID);
  assert.equal(result.ok, true);
  assert.deepEqual(routed.map((entry) => entry.phase), ["route", "revalidate"]);
  assert.equal(routed[1].routing.conversation_id, "abc");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].tabId, 7);
  assert.equal(sent[0].message.text, "send me");
  assert.equal((await store.list()).length, 0);

  const contexts = await sentPromptStore.list();
  assert.equal(contexts.length, 1);
  assert.equal(contexts[0].delivery_id, DELIVERY_ID);
  assert.equal(contexts[0].session, "DevCockpit:DEV:DC-063A");
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

test("concurrent navigation detected by revalidation fails before DOM send", async () => {
  const { store, sentPromptStore, sent, coordinator } = await setup({
    revalidateError: "bound_target_changed",
  });
  const result = await coordinator.send(DELIVERY_ID);
  assert.equal(result.ok, false);
  assert.match(result.error, /bound_target_changed/);
  assert.equal(sent.length, 0);
  assert.equal((await store.list())[0].local_status, "QUEUED");
  assert.equal((await sentPromptStore.list()).length, 0);
});

test("provisional manual send preserves dedicated tab identity", async () => {
  const { routed, sent, coordinator } = await setup({ routing: null });
  const result = await coordinator.send(DELIVERY_ID);
  assert.equal(result.ok, true);
  assert.equal(routed[0].routing, null);
  assert.equal(routed[1].routing, null);
  assert.equal(sent[0].tabId, 7);
});
