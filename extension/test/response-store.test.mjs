import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const RESPONSE_ID = "10cd3422-3dbd-481f-a5b0-5915a1f7f5be";

async function stores(storage) {
  const context = await loadClassicScripts(["src/response-store.js"]);
  const { SentPromptStore, PendingResponseStore } = context.DevCockpitCompanion.responses;
  const options = { now: () => new Date("2026-10-01T18:00:00.000Z") };
  return {
    sent: new SentPromptStore(storage, options),
    pending: new PendingResponseStore(storage, options),
  };
}

test("sent prompt context survives extension reload", async () => {
  const storage = createMemoryStorage();
  const { sent } = await stores(storage);
  await sent.recordSent({
    deliveryId: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-030",
    tabId: 7,
    conversationUrl: "https://chatgpt.com/c/abc",
  });
  const { sent: reloaded } = await stores(storage);
  const entries = await reloaded.list();
  assert.equal(entries.length, 1);
  assert.equal(entries[0].delivery_id, DELIVERY_ID);
  assert.equal(entries[0].session, "DevCockpit:DEV:DC-030");
  assert.equal(entries[0].sent_at, "2026-10-01T18:00:00.000Z");
});

test("multiple sent contexts remain independent", async () => {
  const storage = createMemoryStorage();
  const { sent } = await stores(storage);
  await sent.recordSent({ deliveryId: DELIVERY_ID, session: "DevCockpit:DEV:DC-030" });
  await sent.recordSent({
    deliveryId: "9fcd3422-3dbd-481f-a5b0-5915a1f7f5be",
    session: "DevCockpit:DEV:DC-031",
  });
  assert.equal((await sent.list()).length, 2);
});

test("pending response keeps stable response_id until ACK cleanup", async () => {
  const storage = createMemoryStorage();
  const { pending } = await stores(storage);
  const value = {
    responseId: RESPONSE_ID,
    deliveryId: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-030",
    text: "selected response",
  };
  const first = await pending.add(value);
  const replay = await pending.add(value);
  assert.equal(first.entry.response_id, RESPONSE_ID);
  assert.equal(replay.kind, "duplicate");
  assert.equal((await pending.list()).length, 1);
  await pending.markError(RESPONSE_ID, "session_mismatch");
  assert.equal((await pending.list())[0].last_error, "session_mismatch");
  assert.equal(await pending.remove(RESPONSE_ID), true);
  assert.equal((await pending.list()).length, 0);
});

test("pending response collision never overwrites original content", async () => {
  const storage = createMemoryStorage();
  const { pending } = await stores(storage);
  await pending.add({
    responseId: RESPONSE_ID,
    deliveryId: DELIVERY_ID,
    session: "DevCockpit:DEV:DC-030",
    text: "original",
  });
  await assert.rejects(
    pending.add({
      responseId: RESPONSE_ID,
      deliveryId: DELIVERY_ID,
      session: "DevCockpit:DEV:DC-030",
      text: "changed",
    }),
    (error) => error?.name === "ResponseStorageConflictError",
  );
  assert.equal((await pending.list())[0].text, "original");
});
