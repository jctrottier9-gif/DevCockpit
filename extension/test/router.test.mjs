import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

async function setup({
  initialTabs = [],
  createdTabInitialUrl = null,
  navigateCreatedTabTo = null,
  onSleep = null,
  pollDelaysMs = [0],
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
      if (onSleep) {
        await onSleep({ tabs, created });
        return;
      }
      if (navigateCreatedTabTo && created.length > 0) {
        const createdId = created.at(-1).id;
        const tab = tabs.find((candidate) => candidate.id === createdId);
        if (tab) tab.url = navigateCreatedTabTo;
      }
    },
    createdTabPollDelaysMs: pollDelaysMs,
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

  const project = conversationIdentity(
    "https://chatgpt.com/g/g-p-project/c/conv-a?model=auto",
  );
  assert.equal(project.conversationId, "conv-a");
  assert.equal(project.canonicalUrl, "https://chatgpt.com/c/conv-a");

  const workspace = conversationIdentity(
    "https://chatgpt.com/w/team/c/conv-b#anchor",
  );
  assert.equal(workspace.conversationId, "conv-b");
  assert.equal(workspace.canonicalUrl, "https://chatgpt.com/c/conv-b");
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

test("provisional session reuses its dedicated tab after ChatGPT project-shell navigation", async () => {
  const value = await setup();
  const first = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: null,
  });
  const tab = value.tabs.find((candidate) => candidate.id === first.tabId);
  tab.url =
    "https://chatgpt.com/g/g-p-69651c27f7b88191ac2e3eddaeeb7a39-application-planification/project";

  const second = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: null,
  });

  assert.equal(second.tabId, first.tabId);
  assert.equal(value.created.length, 1);
  const revalidated = await value.router.revalidateTarget({
    session: "RessourcePlanner:DEV:594D",
    routing: null,
    tabId: first.tabId,
  });
  assert.equal(revalidated.id, first.tabId);
});

test("provisional matcher accepts project shell but rejects an existing conversation", async () => {
  const { context } = await setup();
  const { isProvisionalChatGptUrl } = context.DevCockpitCompanion.routing;

  assert.equal(
    isProvisionalChatGptUrl(
      "https://chatgpt.com/g/g-p-project-slug/project",
    ),
    true,
  );
  assert.equal(
    isProvisionalChatGptUrl(
      "https://chatgpt.com/g/g-p-project-slug/c/conv-existing",
    ),
    false,
  );
});


test("bound revalidation waits through a project shell and returns the same linked conversation", async () => {
  let sleeps = 0;
  const value = await setup({
    initialTabs: [{ id: 7, url: "https://chatgpt.com/c/conv-a" }],
    pollDelaysMs: [0, 0],
    onSleep: async ({ tabs }) => {
      sleeps += 1;
      if (sleeps === 1) {
        tabs[0].url =
          "https://chatgpt.com/g/g-p-project-application-planification/project";
      } else {
        tabs[0].url =
          "https://chatgpt.com/g/g-p-project-application-planification/c/conv-a";
      }
    },
  });
  const target = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });
  value.tabs[0].url =
    "https://chatgpt.com/g/g-p-project-application-planification/project";

  const revalidated = await value.router.revalidateTarget({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
    tabId: target.tabId,
  });

  assert.equal(revalidated.id, 7);
  assert.equal(
    revalidated.url,
    "https://chatgpt.com/g/g-p-project-application-planification/c/conv-a",
  );
});

test("bound revalidation fails immediately when the same tab enters another conversation", async () => {
  let sleeps = 0;
  const value = await setup({
    initialTabs: [{ id: 7, url: "https://chatgpt.com/c/conv-a" }],
    pollDelaysMs: [0, 0],
    onSleep: async () => {
      sleeps += 1;
    },
  });
  const target = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });
  value.tabs[0].url = "https://chatgpt.com/c/other-conversation";

  await assert.rejects(
    () =>
      value.router.revalidateTarget({
        session: "RessourcePlanner:DEV:594D",
        routing: ROUTING,
        tabId: target.tabId,
      }),
    (error) => error?.code === "bound_target_changed",
  );
  assert.equal(sleeps, 0);
});

test("bound revalidation remains fail-closed when a transient project shell never resolves", async () => {
  let sleeps = 0;
  const value = await setup({
    initialTabs: [{ id: 7, url: "https://chatgpt.com/c/conv-a" }],
    pollDelaysMs: [0, 0],
    onSleep: async () => {
      sleeps += 1;
    },
  });
  const target = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });
  value.tabs[0].url =
    "https://chatgpt.com/g/g-p-project-application-planification/project";

  await assert.rejects(
    () =>
      value.router.revalidateTarget({
        session: "RessourcePlanner:DEV:594D",
        routing: ROUTING,
        tabId: target.tabId,
      }),
    (error) => error?.code === "bound_target_changed",
  );
  assert.equal(sleeps, 2);
});

test("bound tab creation tolerates a transient ChatGPT project shell before the linked conversation", async () => {
  let sleeps = 0;
  const value = await setup({
    createdTabInitialUrl: "about:blank",
    pollDelaysMs: [0, 0],
    onSleep: async ({ tabs, created }) => {
      sleeps += 1;
      const tab = tabs.find((candidate) => candidate.id === created.at(-1)?.id);
      if (!tab) return;
      tab.url =
        sleeps === 1
          ? "https://chatgpt.com/g/g-p-project-application-planification/project"
          : "https://chatgpt.com/g/g-p-project-application-planification/c/conv-a";
    },
  });

  const target = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });

  assert.equal(target.tabId, value.created[0].id);
  assert.equal(value.created.length, 1);
  assert.equal(
    target.url,
    "https://chatgpt.com/g/g-p-project-application-planification/c/conv-a",
  );
});


test("bound routing prefers cached tab on transient project shell instead of creating another tab", async () => {
  const value = await setup({
    initialTabs: [{ id: 7, url: "https://chatgpt.com/c/conv-a" }],
  });
  const first = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });
  assert.equal(first.tabId, 7);
  value.tabs[0].url =
    "https://chatgpt.com/g/g-p-project-application-planification/project";

  const second = await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });

  assert.equal(second.tabId, 7);
  assert.equal(value.created.length, 0);
  assert.match(second.url, /application-planification\/project$/);
});

test("bound routing fails closed on cached tab in another conversation without creating a new tab", async () => {
  const value = await setup({
    initialTabs: [{ id: 7, url: "https://chatgpt.com/c/conv-a" }],
  });
  await value.router.route({
    session: "RessourcePlanner:DEV:594D",
    routing: ROUTING,
  });
  value.tabs[0].url = "https://chatgpt.com/c/other-conversation";

  await assert.rejects(
    () =>
      value.router.route({
        session: "RessourcePlanner:DEV:594D",
        routing: ROUTING,
      }),
    (error) =>
      error?.code === "bound_target_changed" &&
      /expected=conv-a/.test(error.message) &&
      /observed=other-conversation/.test(error.message),
  );
  assert.equal(value.created.length, 0);
});
