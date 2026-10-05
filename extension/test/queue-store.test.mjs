import assert from "node:assert/strict";
import test from "node:test";
import {
  createMemoryStorage,
  loadClassicScripts,
} from "./helpers.mjs";

const FIRST_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const SECOND_ID = "9fcd3422-3dbd-481f-a5b0-5915a1f7f5be";

async function loadQueue(storage) {
  const context = await loadClassicScripts(["src/queue-store.js"]);
  const { QueueStore } = context.DevCockpitCompanion.queue;
  return {
    context,
    store: new QueueStore(storage, {
      now: () => new Date("2026-10-01T16:00:00.000Z"),
    }),
  };
}

test("new prompt is persisted and restored after reload", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);

  const accepted = await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "DevCockpit:DEV:DC-012",
    text: "first",
  });
  assert.equal(accepted.kind, "accepted");

  const { store: reloaded } = await loadQueue(storage);
  const queue = await reloaded.list();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].delivery_id, FIRST_ID);
  assert.equal(queue[0].local_status, "QUEUED");
});

test("identical replay deduplicates without changing persisted content", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);
  const prompt = {
    deliveryId: FIRST_ID,
    session: "DevCockpit:DEV:DC-012",
    text: "same",
  };

  await store.acceptPrompt(prompt);
  const replay = await store.acceptPrompt(prompt);

  assert.equal(replay.kind, "duplicate");
  const queue = await store.list();
  assert.equal(queue.length, 1);
  assert.equal(queue[0].text, "same");
});

test("same delivery id with incompatible content is explicit conflict", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);

  await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "DevCockpit:DEV:DC-012",
    text: "original",
  });

  await assert.rejects(
    store.acceptPrompt({
      deliveryId: FIRST_ID,
      session: "DevCockpit:DEV:DC-012",
      text: "changed",
    }),
    (error) => error?.name === "DeliveryConflictError",
  );

  assert.equal((await store.list())[0].text, "original");
});

test("multiple sessions coexist independently", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);

  await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "DevCockpit:DEV:DC-012",
    text: "one",
  });
  await store.acceptPrompt({
    deliveryId: SECOND_ID,
    session: "RessourcePlanner:ARCH:502",
    text: "two",
  });

  const queue = await store.list();
  assert.deepEqual(
    queue.map((entry) => entry.session).sort(),
    ["DevCockpit:DEV:DC-012", "RessourcePlanner:ARCH:502"].sort(),
  );
});

test("send-requested state persists without becoming product completion", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);

  await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "DevCockpit:DEV:DC-012",
    text: "send",
  });
  await store.markSendRequested(FIRST_ID);

  const entry = (await store.list())[0];
  assert.equal(entry.local_status, "SEND_REQUESTED");
  assert.equal("status" in entry, false);
  assert.equal("work_item_status" in entry, false);
});

test("routing diagnostics do not erase SEND_REQUESTED uncertainty", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);
  await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "DevCockpit:DEV:DC-063A",
    text: "send",
    routing: null,
  });
  await store.markSendRequested(FIRST_ID);
  await store.setError(FIRST_ID, "routing_failed");

  const entry = (await store.list())[0];
  assert.equal(entry.local_status, "SEND_REQUESTED");
  assert.equal(entry.last_error, "routing_failed");
});


test("legacy routing can be repaired once with a real conversation at the same binding version", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);
  const legacy = {
    binding_version: 4,
    conversation_id: "local-chatgpt%3Alegacy",
    canonical_url: "https://chatgpt.com/c/local-chatgpt%3Alegacy",
  };
  await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "RessourcePlanner:DEV:594D",
    text: "fix CI",
    routing: legacy,
  });

  const repaired = await store.repairLegacyRouting(FIRST_ID, {
    binding_version: 4,
    conversation_id: "real-594d",
    canonical_url: "https://chatgpt.com/c/real-594d",
  });

  assert.equal(repaired.routing.conversation_id, "real-594d");
  await assert.rejects(
    store.repairLegacyRouting(FIRST_ID, {
      binding_version: 4,
      conversation_id: "other",
      canonical_url: "https://chatgpt.com/c/other",
    }),
    (error) => error?.message === "routing_repair_not_legacy",
  );
});

test("legacy routing repair rejects a different binding version", async () => {
  const storage = createMemoryStorage();
  const { store } = await loadQueue(storage);
  await store.acceptPrompt({
    deliveryId: FIRST_ID,
    session: "RessourcePlanner:DEV:594D",
    text: "fix CI",
    routing: {
      binding_version: 4,
      conversation_id: "local-chatgpt%3Alegacy",
      canonical_url: "https://chatgpt.com/c/local-chatgpt%3Alegacy",
    },
  });

  await assert.rejects(
    store.repairLegacyRouting(FIRST_ID, {
      binding_version: 5,
      conversation_id: "real-594d",
      canonical_url: "https://chatgpt.com/c/real-594d",
    }),
    (error) => error?.message === "routing_repair_invalid",
  );
});
