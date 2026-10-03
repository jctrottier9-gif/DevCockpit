import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

async function setup({ initialTabs = [] } = {}) {
  const storage = createMemoryStorage();
  const context = await loadClassicScripts([
    "src/routing-store.js",
    "src/router.js",
  ]);
  const { ConversationRoutingStore } = context.DevCockpitCompanion.routingStore;
  const { ConversationRouter } = context.DevCockpitCompanion.routing;
  const tabs = initialTabs.map((tab) => ({ ...tab }));
  let nextId = Math.max(0, ...tabs.map((tab) => tab.id || 0)) + 1;
  const created = [];
  const store = new ConversationRoutingStore(storage);
  const router = new ConversationRouter({
    routingStore: store,
    queryTabs: async () => tabs.map((tab) => ({ ...tab })),
    createTab: async ({ url, active }) => {
      const tab = { id: nextId++, url, active };
      tabs.push(tab);
      created.push({ ...tab });
      return { ...tab };
    },
  });
  return { storage, context, store, router, tabs, created };
}

const ROUTING = {
  binding_version: 4,
  conversation_id: "conv-a",
  canonical_url: "https://chatgpt.com/c/conv-a",
};

test("bound routing reuses only an exact conversation identity", async () => {
  const { router, created } = await setup({
    initialTabs: [
      { id: 8, url: "https://chatgpt.com/c/other", title: "conv-a" },
      { id: 7, url: "https://chat.openai.com/c/conv-a?model=auto", title: "anything" },
    ],
  });
  const target = await router.route({
    session: "DevCockpit:DEV:DC-063A",
    routing: ROUTING,
  });
  assert.equal(target.tabId, 7);
  assert.equal(created.length, 0);
});

test("bound routing reopens canonical URL when no exact tab is open", async () => {
  const { router, created } = await setup();
  const target = await router.route({
    session: "DevCockpit:DEV:DC-063A",
    routing: ROUTING,
  });
  assert.equal(target.kind, "BOUND");
  assert.equal(created.length, 1);
  assert.equal(created[0].url, "https://chatgpt.com/c/conv-a");
});

test("distinct unbound sessions receive distinct dedicated provisional tabs", async () => {
  const { router, created } = await setup();
  const first = await router.route({ session: "DevCockpit:DEV:A", routing: null });
  const second = await router.route({ session: "DevCockpit:DEV:B", routing: null });
  assert.notEqual(first.tabId, second.tabId);
  assert.equal(created.length, 2);
  assert.equal(created[0].url, "https://chatgpt.com/");
  assert.equal(created[1].url, "https://chatgpt.com/");
});

test("restart recovers a provisional session without mixing it with another tab", async () => {
  const first = await setup();
  const original = await first.router.route({
    session: "DevCockpit:DEV:A",
    routing: null,
  });

  const { ConversationRoutingStore } = first.context.DevCockpitCompanion.routingStore;
  const { ConversationRouter } = first.context.DevCockpitCompanion.routing;
  const restartedStore = new ConversationRoutingStore(first.storage);
  const restartedRouter = new ConversationRouter({
    routingStore: restartedStore,
    queryTabs: async () => first.tabs.map((tab) => ({ ...tab })),
    createTab: async ({ url, active }) => {
      const tab = { id: 99, url, active };
      first.tabs.push(tab);
      return tab;
    },
  });
  const recovered = await restartedRouter.route({
    session: "DevCockpit:DEV:A",
    routing: null,
  });
  assert.equal(recovered.tabId, original.tabId);
});

test("disappeared provisional tab is replaced for the same session", async () => {
  const value = await setup();
  const original = await value.router.route({
    session: "DevCockpit:DEV:A",
    routing: null,
  });
  value.tabs.splice(value.tabs.findIndex((tab) => tab.id === original.tabId), 1);
  const replacement = await value.router.route({
    session: "DevCockpit:DEV:A",
    routing: null,
  });
  assert.notEqual(replacement.tabId, original.tabId);
});

test("revalidation fails closed after concurrent navigation", async () => {
  const value = await setup({
    initialTabs: [{ id: 7, url: "https://chatgpt.com/c/conv-a" }],
  });
  const target = await value.router.route({
    session: "DevCockpit:DEV:DC-063A",
    routing: ROUTING,
  });
  value.tabs[0].url = "https://chatgpt.com/c/other";
  await assert.rejects(
    () => value.router.revalidateTarget({
      session: "DevCockpit:DEV:DC-063A",
      routing: ROUTING,
      tabId: target.tabId,
    }),
    (error) => error?.code === "bound_target_changed",
  );
  const cached = await value.store.get("DevCockpit:DEV:DC-063A");
  assert.equal(cached.kind, "INVALIDATED");
});

test("invalidated local route fails closed", async () => {
  const value = await setup();
  await value.store.markInvalidated("DevCockpit:DEV:A", "unsafe_target");
  await assert.rejects(
    () => value.router.route({ session: "DevCockpit:DEV:A", routing: null }),
    (error) => error?.code === "binding_invalidated",
  );
});
