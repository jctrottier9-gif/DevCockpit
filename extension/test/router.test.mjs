import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

async function setup({
  initialTabs = [],
  createdTabInitialUrl = null,
  navigateCreatedTabTo = null,
} = {}) {
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
      const tab = {
        id: nextId++,
        url: createdTabInitialUrl ?? url,
        requestedUrl: url,
        active,
      };
      tabs.push(tab);
      created.push({ ...tab });
      return { ...tab };
    },
    sleep: async () => {
      if (navigateCreatedTabTo && created.length > 0) {
        const createdId = created.at(-1).id;
        const tab = tabs.find((candidate) => candidate.id === createdId);
        if (tab) tab.url = navigateCreatedTabTo;
      }
    },
    createdTabPollDelaysMs: [0],
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

test("provisional routing tolerates transient about:blank after tab creation", async () => {
  const { router, created } = await setup({
    createdTabInitialUrl: "about:blank",
    navigateCreatedTabTo: "https://chatgpt.com/",
  });
  const target = await router.route({
    session: "DevCockpit:DEV:TRANSIENT",
    routing: null,
  });
  assert.equal(created[0].requestedUrl, "https://chatgpt.com/");
  assert.equal(target.url, "https://chatgpt.com/");
});

test("bound routing tolerates transient about:blank after tab creation", async () => {
  const { router, created } = await setup({
    createdTabInitialUrl: "about:blank",
    navigateCreatedTabTo: "https://chatgpt.com/c/conv-a",
  });
  const target = await router.route({
    session: "DevCockpit:DEV:BOUND-TRANSIENT",
    routing: ROUTING,
  });
  assert.equal(created[0].requestedUrl, "https://chatgpt.com/c/conv-a");
  assert.equal(target.url, "https://chatgpt.com/c/conv-a");
});

test("created tab reaching an incompatible ChatGPT target still fails closed", async () => {
  const { router } = await setup({
    createdTabInitialUrl: "about:blank",
    navigateCreatedTabTo: "https://chatgpt.com/c/other",
  });
  await assert.rejects(
    () => router.route({
      session: "DevCockpit:DEV:BOUND-MISMATCH",
      routing: ROUTING,
    }),
    (error) => error?.code === "created_tab_target_mismatch",
  );
});

test("created tab that never reaches ChatGPT fails with bounded navigation timeout", async () => {
  const { router } = await setup({
    createdTabInitialUrl: "about:blank",
  });
  await assert.rejects(
    () => router.route({
      session: "DevCockpit:DEV:TIMEOUT",
      routing: null,
    }),
    (error) => error?.code === "created_tab_navigation_timeout",
  );
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
  assert.equal(cached.kind, "BOUND");
});

test("invalidated local route fails closed", async () => {
  const value = await setup();
  await value.store.markInvalidated("DevCockpit:DEV:A", "unsafe_target");
  await assert.rejects(
    () => value.router.route({ session: "DevCockpit:DEV:A", routing: null }),
    (error) => error?.code === "binding_invalidated",
  );
});

test("provisional revalidation fails closed after navigation into an existing conversation", async () => {
  const value = await setup();
  const target = await value.router.route({
    session: "DevCockpit:DEV:PROVISIONAL",
    routing: null,
  });
  const tab = value.tabs.find((candidate) => candidate.id === target.tabId);
  tab.url = "https://chatgpt.com/c/unrelated";
  await assert.rejects(
    () => value.router.revalidateTarget({
      session: "DevCockpit:DEV:PROVISIONAL",
      routing: null,
      tabId: target.tabId,
    }),
    (error) => error?.code === "provisional_target_changed",
  );
});


test("conversation identity accepts nested project and workspace routes", async () => {
  const { context } = await setup();
  const { conversationIdentity } = context.DevCockpitCompanion.routing;

  assert.deepEqual(
    conversationIdentity("https://chatgpt.com/g/g-p-project/c/conv-a?model=auto"),
    {
      conversationId: "conv-a",
      canonicalUrl: "https://chatgpt.com/c/conv-a",
    },
  );
  assert.deepEqual(
    conversationIdentity("https://chatgpt.com/w/team/c/conv-b#anchor"),
    {
      conversationId: "conv-b",
      canonicalUrl: "https://chatgpt.com/c/conv-b",
    },
  );
  assert.equal(
    conversationIdentity("https://chatgpt.com/g/g-p-project/c/conv-a/extra"),
    null,
  );
});

test("bound routing recognizes nested project URL for the same conversation id", async () => {
  const { router, created } = await setup({
    initialTabs: [
      {
        id: 12,
        url: "https://chatgpt.com/g/g-p-project/c/conv-a?model=auto",
      },
    ],
  });

  const target = await router.route({
    session: "DevCockpit:DEV:NESTED",
    routing: ROUTING,
  });

  assert.equal(target.tabId, 12);
  assert.equal(created.length, 0);
});
