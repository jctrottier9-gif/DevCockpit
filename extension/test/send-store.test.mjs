import assert from "node:assert/strict";
import test from "node:test";
import { createMemoryStorage, loadClassicScripts } from "./helpers.mjs";

const DELIVERY_ID = "8fcd3422-3dbd-481f-a5b0-5915a1f7f5be";
const SESSION = "DevCockpit:DEV:DC-063B";

async function setup() {
  const storage = createMemoryStorage();
  let counter = 0;
  const context = await loadClassicScripts(
    ["src/send-store.js"],
    {
      crypto: {
        randomUUID: () =>
          `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
      },
    },
  );
  const { ChatGptSendStore, SEND_STATE } =
    context.DevCockpitCompanion.sendStore;
  const store = new ChatGptSendStore(storage, {
    now: () => new Date("2026-10-04T15:00:00.000Z"),
    uuid: () =>
      `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
  });
  return { store, SEND_STATE };
}

test("SEND_ARMED and replay event persist before irreversible action", async () => {
  const { store, SEND_STATE } = await setup();
  await store.ensureQueued({ deliveryId: DELIVERY_ID, session: SESSION });
  await store.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: SEND_STATE.ROUTING,
    attempt: 1,
  });
  const armed = await store.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: SEND_STATE.SEND_ARMED,
    attempt: 1,
  });

  assert.equal((await store.get(DELIVERY_ID)).state, "SEND_ARMED");
  assert.equal(armed.event.state, "SEND_ARMED");
  assert.equal(
    (await store.pendingEvents()).at(-1).event_id,
    armed.event.event_id,
  );
});

test("restart converts SEND_ARMED to AMBIGUOUS and blocks resend", async () => {
  const { store, SEND_STATE } = await setup();
  await store.ensureQueued({ deliveryId: DELIVERY_ID, session: SESSION });
  await store.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: SEND_STATE.ROUTING,
    attempt: 1,
  });
  await store.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: SEND_STATE.SEND_ARMED,
    attempt: 1,
  });

  const events = await store.recoverInterruptedArmedSends();

  assert.equal(events.length, 1);
  assert.equal(events[0].state, "AMBIGUOUS");
  assert.equal((await store.get(DELIVERY_ID)).state, "AMBIGUOUS");
  await assert.rejects(
    store.transition({
      deliveryId: DELIVERY_ID,
      session: SESSION,
      state: SEND_STATE.ROUTING,
      attempt: 2,
    }),
    /terminal/,
  );
});

test("status outbox is removed only by exact backend event ACK", async () => {
  const { store, SEND_STATE } = await setup();
  await store.ensureQueued({ deliveryId: DELIVERY_ID, session: SESSION });
  const result = await store.transition({
    deliveryId: DELIVERY_ID,
    session: SESSION,
    state: SEND_STATE.ROUTING,
    attempt: 1,
  });

  assert.equal((await store.pendingEvents()).length, 1);
  assert.equal(await store.ackEvent("other-event"), false);
  assert.equal(await store.ackEvent(result.event.event_id), true);
  assert.equal((await store.pendingEvents()).length, 0);
});
