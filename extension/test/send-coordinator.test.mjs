import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const SESSION = "DevCockpit:DEV:DC-063B";

async function setup({
  commitResponse = {
    ok: true,
    conversationUrl: "https://chatgpt.com/c/new-conversation",
  },
  prepareResponse = {
    ok: true,
    baseline: { user_message_count: 0, expected_text: "send me" },
  },
  routeError = null,
  prepareErrors = [],
  retryDelaysMs = [],
} = {}) {
  const storage = createMemoryStorage();
  let counter = 0;
  const context = await loadClassicScripts(
    [
      "src/queue-store.js",
      "src/response-store.js",
      "src/send-store.js",
      "src/send-coordinator.js",
    ],
    {
      crypto: {
        randomUUID: () =>
          `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
      },
    },
  );
  const { QueueStore } = context.DevCockpitCompanion.queue;
  const { SentPromptStore } = context.DevCockpitCompanion.responses;
  const { ChatGptSendStore } = context.DevCockpitCompanion.sendStore;
  const { PromptSendCoordinator } = context.DevCockpitCompanion.send;

  const queueStore = new QueueStore(storage);
  const sentPromptStore = new SentPromptStore(storage);
  const sendStore = new ChatGptSendStore(storage, {
    uuid: () =>
      `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
  });
  await queueStore.acceptPrompt({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    text: "send me",
    routing: null,
  });
  await sendStore.ensureQueued({ deliveryId: DELIVERY_ID, session: SESSION });

  const calls = [];
  const transitions = [];
  const remainingPrepareErrors = [...prepareErrors];
  const originalTransition = sendStore.transition.bind(sendStore);
  sendStore.transition = async (command) => {
    const result = await originalTransition(command);
    transitions.push(result.entry.state);
    return result;
  };

  const coordinator = new PromptSendCoordinator({
    queueStore,
    sentPromptStore,
    sendStore,
    router: {
      async route() {
        if (routeError) throw new Error(routeError);
        return { kind: "PROVISIONAL", tabId: 7, url: "https://chatgpt.com/" };
      },
      async revalidateTarget() {
        return { id: 7, url: "https://chatgpt.com/" };
      },
    },
    sendToTab: async (_tabId, message) => {
      calls.push(message.type);
      if (message.type === "devcockpit_prepare_prompt") {
        if (remainingPrepareErrors.length > 0) {
          throw new Error(remainingPrepareErrors.shift());
        }
        return prepareResponse;
      }
      return commitResponse;
    },
    sleep: async () => {},
    retryDelaysMs,
  });

  return {
    coordinator,
    sendStore,
    queueStore,
    sentPromptStore,
    calls,
    transitions,
  };
}

test("automatic send persists SEND_ARMED before exactly one DOM commit", async () => {
  const { coordinator, sendStore, queueStore, calls, transitions } = await setup();

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.ok, true);
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_commit_prepared_prompt",
  ]);
  assert.ok(
    transitions.indexOf("SEND_ARMED") <
      transitions.indexOf("SENT_CONFIRMED"),
  );
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
  assert.equal((await queueStore.list()).length, 0);
});

test("post-barrier uncertainty becomes AMBIGUOUS and never auto-resends", async () => {
  const { coordinator, sendStore, calls } = await setup({
    commitResponse: {
      ok: false,
      error: "send_confirmation_timeout",
      ambiguous: true,
    },
  });

  const first = await coordinator.enqueue(DELIVERY_ID);
  const second = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(first.state, "AMBIGUOUS");
  assert.equal(second.state, "AMBIGUOUS");
  assert.equal(
    calls.filter((item) => item === "devcockpit_commit_prepared_prompt").length,
    1,
  );
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "AMBIGUOUS");
});

test("certain pre-barrier readiness failure is BLOCKED without DOM click", async () => {
  const { coordinator, sendStore, calls } = await setup({
    prepareResponse: { ok: false, error: "send_button_disabled" },
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.deepEqual(calls, ["devcockpit_prepare_prompt"]);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});

test("created-tab navigation timeout remains BLOCKED before SEND_ARMED", async () => {
  const { coordinator, sendStore, calls, transitions } = await setup({
    routeError: "created_tab_navigation_timeout",
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "created_tab_navigation_timeout");
  assert.deepEqual(calls, []);
  assert.equal(transitions.includes("SEND_ARMED"), false);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});

test("transient missing content script retries before SEND_ARMED and then succeeds", async () => {
  const { coordinator, sendStore, calls, transitions } = await setup({
    prepareErrors: [
      "Could not establish connection. Receiving end does not exist.",
    ],
    retryDelaysMs: [0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "SENT_CONFIRMED");
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_prepare_prompt",
    "devcockpit_commit_prepared_prompt",
  ]);
  assert.ok(transitions.includes("WAITING_READY"));
  assert.ok(
    transitions.indexOf("WAITING_READY") <
      transitions.indexOf("SEND_ARMED"),
  );
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "SENT_CONFIRMED");
});

test("missing content script exhausts bounded retries while still pre-SEND_ARMED", async () => {
  const { coordinator, sendStore, calls, transitions } = await setup({
    prepareErrors: [
      "Could not establish connection. Receiving end does not exist.",
      "Could not establish connection. Receiving end does not exist.",
    ],
    retryDelaysMs: [0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "content_script_unavailable");
  assert.deepEqual(calls, [
    "devcockpit_prepare_prompt",
    "devcockpit_prepare_prompt",
  ]);
  assert.equal(transitions.includes("SEND_ARMED"), false);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});

test("unrelated tab messaging failure remains non-retryable", async () => {
  const { coordinator, sendStore, calls, transitions } = await setup({
    prepareErrors: ["Unexpected extension messaging failure"],
    retryDelaysMs: [0, 0],
  });

  const result = await coordinator.enqueue(DELIVERY_ID);

  assert.equal(result.state, "BLOCKED");
  assert.equal(result.error, "Unexpected extension messaging failure");
  assert.deepEqual(calls, ["devcockpit_prepare_prompt"]);
  assert.equal(transitions.includes("WAITING_READY"), false);
  assert.equal(transitions.includes("SEND_ARMED"), false);
  assert.equal((await sendStore.get(DELIVERY_ID)).state, "BLOCKED");
});
